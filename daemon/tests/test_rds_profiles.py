"""Owned RDS content, effective projections and target-local hot application."""
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSourceConfig, ModulatorConfig, RdsProfileConfig
from fmheadendd.models.configuration import RtOverride
from fmheadendd.rds.radiotext import RadioTextService


def source_config(**kw):
    return InputSourceConfig(name='Station', type='HTTP', config={'uri': 'https://example.org/live'}, **kw)


def test_ownership_roundtrip_removal_and_delete(tmp_path):
    path = tmp_path/'owned.db'
    db = Database(path)
    source = db.save('sources', source_config())
    assert source.rds_profile_id is None and source.rds_profile is None
    source = db.save('sources', source.model_copy(update={'rds_profile': RdsProfileConfig(rds_pi=123)}), source.id)
    pid = source.rds_profile_id
    other = db.save('sources', source_config(rds_profile=RdsProfileConfig(rds_pi=123)))
    assert other.rds_profile_id != pid
    db.close()
    db = Database(path)
    assert db.get('sources', source.id) == source
    source = db.save('sources', source.model_copy(update={'rds_profile': None}), source.id)
    assert source.rds_profile_id is None
    assert db.connection.execute('SELECT id FROM rds_profiles WHERE id=?', (pid,)).fetchone() is None
    db.delete('sources', other.id)
    assert db.connection.execute('SELECT count(*) FROM rds_profiles').fetchone()[0] == 8
    db.close()


def test_effective_resolution_preserves_override_and_rejects_invalid_follow(tmp_path):
    db = Database(tmp_path/'resolve.db')
    s = db.save('sources', source_config(rds_profile=RdsProfileConfig(rds_pi=123, rds_rt='Source')))
    initial = db.get('modulators', 0)
    followed = db.save('modulators', initial.model_copy(update={'input_source_id': s.id, 'rds_follow_source': True}), 0)
    assert followed.rds_pi == 123 and not followed.rds_enabled
    assert followed.rds_override_profile == initial.rds_override_profile
    with pytest.raises(DomainError, match='TX0') as exc:
        db.save('sources', s.model_copy(update={'rds_profile': None}), s.id)
    assert exc.value.error == 'RDS_PROFILE_FOLLOWED'
    with pytest.raises(DomainError):
        db.delete('sources', s.id)
    custom = db.save('modulators', followed.model_copy(update={'rds_follow_source': False}), 0)
    assert custom.rds_pi == 0xFFF0 and custom.rds_override_profile == initial.rds_override_profile
    db.save('sources', s.model_copy(update={'rds_profile': None}), s.id)
    with pytest.raises(DomainError, match='no RDS profile'):
        db.save('modulators', custom.model_copy(update={'rds_follow_source': True}), 0)
    assert not db.get('modulators', 0).rds_follow_source
    with pytest.raises(ValueError):
        ModulatorConfig(audio_mode='ANALOG', input_source_id=s.id, rds_follow_source=True)
    db.close()


@pytest.fixture
def live(tmp_path):
    app = create_app(Settings(tmp_path/'hot.db'), clock=lambda: 0.)
    with TestClient(app) as api:
        source = api.post('/api/v1/sources', json=source_config(rds_profile=RdsProfileConfig(rds_pi=321)).model_dump(mode='json')).json()
        for tx in (0, 2, 5, 7):
            response = api.patch(f'/api/v1/modulators/{tx}', json={
                'input_source_id': source['id'], 'frequency_khz': 98100,
                'rds_enabled': True, 'rds_follow_source': tx != 7})
            assert response.status_code == 200, response.text
            assert api.post(f'/api/v1/modulators/{tx}/start').status_code == 200
        services = app.state.services
        app.state.control.executor.submit(services.modulators.rds.tick).result()
        for method in ('set_tx_enabled', 'set_tx_config', 'clear_rds'):
            setattr(services.rp, method, Mock(wraps=getattr(services.rp, method)))
        yield api, app, source, services


def test_source_hot_profile_targets_followers_only_and_preserves_audio(live):
    api, app, source, services = live
    pipeline = services.sources.pipelines[source['id']]
    pipeline.stop = Mock(wraps=pipeline.stop)
    profile = source['rds_profile'] | {'rds_pi': 432, 'rds_rt': 'Changed'}
    response = api.patch(f"/api/v1/sources/{source['id']}", json={'rds_profile': profile})
    assert response.status_code == 200 and response.json()['applied']
    assert services.sources.pipelines[source['id']] is pipeline
    pipeline.stop.assert_not_called()
    services.rp.set_tx_enabled.assert_not_called()
    services.rp.set_tx_config.assert_not_called()
    assert {c.args[0] for c in services.rp.clear_rds.call_args_list} == {0, 2, 5}
    for tx in (0, 2, 5):
        assert api.get(f'/api/v1/modulators/{tx}').json()['rds_pi'] == 432
    assert api.get('/api/v1/modulators/7').json()['rds_pi'] == 0xFFF7
    denied = api.patch(f"/api/v1/sources/{source['id']}", json={'rds_profile': None})
    assert denied.status_code == 409 and denied.json()['error'] == 'RDS_PROFILE_FOLLOWED'
    assert all(f'TX{tx}' in denied.json()['message'] for tx in (0, 2, 5))


def test_mixed_source_save_uses_existing_audio_lifecycle_atomically(live):
    api, app, source, services = live
    before = api.get(f"/api/v1/sources/{source['id']}").json()
    old = services.sources.pipelines[source['id']]
    profile = before['rds_profile'] | {'rds_pi': 987}
    bad = api.patch(f"/api/v1/sources/{source['id']}", json={'rds_profile': profile, 'config': {'uri': 'invalid'}})
    assert bad.status_code == 422
    assert api.get(f"/api/v1/sources/{source['id']}").json() == before
    good = api.patch(f"/api/v1/sources/{source['id']}", json={'rds_profile': profile, 'input_gain_db': -3})
    assert good.status_code == 200 and good.json()['applied']
    assert services.sources.pipelines[source['id']] is not old
    assert services.sources.consumers[source['id']] == {0, 2, 5, 7}
    services.rp.set_tx_enabled.assert_not_called()
    services.rp.set_tx_config.assert_not_called()


def test_active_override_and_selection_changes_are_hot_but_enable_is_hardware(live):
    api, app, source, services = live
    url = '/api/v1/modulators/7'
    before = api.get(url).json()
    override = before['rds_override_profile'] | {'rds_rt': 'Override', 'rds_pi': 234}
    result = api.post(url+'/save-and-resume', json={'rds_override_profile': override}).json()
    assert result['saved'] and not result['restarted']
    assert result['configuration']['rds_pi'] == 234
    assert api.patch(url, json={'rds_follow_source': True}).json()['rds_pi'] == 321
    custom = api.patch(url, json={'rds_follow_source': False}).json()
    assert custom['rds_override_profile'] == override and custom['rds_rt'] == 'Override'
    services.rp.set_tx_enabled.assert_not_called()
    services.rp.set_tx_config.assert_not_called()
    assert api.patch(url, json={'rds_enabled': False}).status_code == 409
    result = api.post(url+'/save-and-resume', json={'rds_enabled': False}).json()
    assert result['saved'] and result['restarted']
    assert services.rp.set_tx_config.called and services.rp.set_tx_enabled.called


def test_failed_persistence_rolls_back_owned_profile(tmp_path):
    db = Database(tmp_path/'atomic.db')
    s = db.save('sources', source_config(rds_profile=RdsProfileConfig(rds_pi=1)))
    before = [tuple(r) for r in db.connection.execute('SELECT * FROM rds_profiles')]
    db.connection.execute("CREATE TEMP TRIGGER fail_source BEFORE UPDATE ON sources BEGIN SELECT RAISE(ABORT, 'failed'); END")
    with pytest.raises(DomainError):
        db.save('sources', s.model_copy(update={'rds_profile': RdsProfileConfig(rds_pi=2)}), s.id)
    assert [tuple(r) for r in db.connection.execute('SELECT * FROM rds_profiles')] == before
    db.close()


def test_shared_profile_still_has_per_tx_rt_override_and_metadata_policy(tmp_path):
    from types import SimpleNamespace
    db = Database(tmp_path/'rt.db')
    source = db.save('sources', source_config(rds_profile=RdsProfileConfig(rds_rt='Fallback', rds_metadata_to_rt=True)))
    for tx in (0, 1):
        db.save('modulators', db.get('modulators', tx).model_copy(update={
            'input_source_id': source.id, 'rds_follow_source': True}), tx)
    metadata = SimpleNamespace(stream_title='Artist - Title')
    rt = RadioTextService(db, SimpleNamespace(status=lambda _: metadata))
    assert rt.effective(0).text == rt.effective(1).text == 'Artist - Title'
    rt.set(0, RtOverride(text='Override'))
    assert rt.effective(0).text == 'Override' and rt.effective(1).text == 'Artist - Title'
    metadata.stream_title = None
    assert rt.effective(1).text == 'Fallback'
    db.close()


def test_rds_only_save_does_not_retry_failed_audio_apply(live):
    api, app, source, services = live
    services.sources.restart_errors[source['id']] = 'Previous audio failure'
    pipeline = services.sources.pipelines[source['id']]
    pipeline.stop = Mock(wraps=pipeline.stop)
    response = api.patch(f"/api/v1/sources/{source['id']}", json={
        'rds_profile': source['rds_profile'] | {'rds_pi': 456}})
    assert response.status_code == 200
    pipeline.stop.assert_not_called()
    assert services.sources.restart_errors[source['id']] == 'Previous audio failure'


def test_corrupt_owned_profile_relationship_is_rejected_without_rewriting(tmp_path):
    path = tmp_path/'invalid.db'
    db = Database(path)
    source = db.save('sources', source_config())
    db.connection.execute('UPDATE sources SET rds_profile_id=1 WHERE id=?', (source.id,))
    db.connection.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='ownership'):
        Database(path)
    assert path.read_bytes() == before
