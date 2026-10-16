"""Physical inventory, narrow persistence and lifecycle regression checks."""
import json
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.rp2354.client import MockRp2354Client

P = '/api/v1'


def stored(db):
    return [dict(r) for r in db.connection.execute('SELECT * FROM modulators ORDER BY id')]


def test_enabled_only_transaction_preserves_raw_data_and_profiles(tmp_path):
    db = Database(tmp_path / 'config.db')
    assert len(stored(db)) == 8
    with db.connection:
        for row in stored(db):
            data = json.loads(row['data']) | {'enabled': True, 'unknown_optional': {'future': [1, None, 'x']}}
            db.connection.execute('UPDATE modulators SET data=? WHERE id=?', (json.dumps(data), row['id']))
    before = stored(db)
    profiles = list(db.connection.execute('SELECT * FROM rds_profiles'))
    statements = []
    db.connection.set_trace_callback(statements.append)
    db.disable_absent_modulators(4)
    assert sum(s.startswith('BEGIN') for s in statements) == 1
    assert sum(s.startswith('COMMIT') for s in statements) == 1
    after = stored(db)
    for original, result in zip(before, after):
        old_data, new_data = json.loads(original.pop('data')), json.loads(result.pop('data'))
        assert original == result
        assert new_data == (old_data if original['id'] < 4 else old_data | {'enabled': False})
    changes = db.connection.total_changes
    db.disable_absent_modulators(4)
    db.disable_absent_modulators(8)
    assert db.connection.total_changes == changes
    assert list(db.connection.execute('SELECT * FROM rds_profiles')) == profiles
    db.close()


def controller(count):
    rp = MockRp2354Client()
    rp.physical_count = count
    get_info = rp.get_info
    rp.get_info = lambda: get_info() | {'modulator_count': rp.physical_count}
    rp.calls = []
    for name in ('set_tx_enabled', 'set_tx_config', 'clear_rds', 'push_rds'):
        original = getattr(rp, name)
        def call(channel, *args, _original=original, _name=name):
            assert 0 <= channel < rp.physical_count
            rp.calls.append((_name, channel, *args))
            return _original(channel, *args)
        setattr(rp, name, call)
    return rp


@pytest.mark.parametrize('count', [1, 4, 8])
def test_inventory_api_rds_and_global_controls(tmp_path, count):
    path = tmp_path / 'config.db'
    db = Database(path)
    for channel in range(8):
        config = db.get('modulators', channel).model_copy(update={
            'enabled': True, 'frequency_khz': 98100, 'audio_mode': 'ANALOG', 'rds_enabled': True})
        db.save('modulators', config, channel)
    db.close()
    rp = controller(count)
    app = create_app(Settings(path), rp=rp)
    with TestClient(app) as api:
        assert [c['id'] for c in api.get(P + '/modulators').json()] == list(range(count))
        assert [c['id'] for c in api.get(P + '/status').json()['modulators']] == list(range(count))
        s = app.state.services
        def refill():
            s.rds.next_poll = 0
            s.rds.tick()
        app.state.control.executor.submit(refill).result()
        assert any(c[0] == 'push_rds' for c in rp.calls)
        before = Database(path)
        saved = list(before.connection.iterdump())
        before.close()
        absent = count
        for method, suffix, body in [('get', '', None), ('patch', '', {'frequency_khz': 99500}),
                ('post', '/start', {}), ('post', '/stop', {}),
                ('post', '/save-and-resume', {'frequency_khz': 99500}),
                ('get', '/rds/rt', None), ('put', '/rds/rt', {'text': 'Absent'}), ('delete', '/rds/rt', None)]:
            kwargs = {} if body is None else {'json': body}
            assert getattr(api, method)(P + f'/modulators/{absent}' + suffix, **kwargs).status_code == 404
        after = Database(path)
        assert list(after.connection.iterdump()) == saved
        after.close()
        response = api.post(P + '/modulators/stop-all', json={})
        assert response.status_code == 200
        assert [r['id'] for r in response.json()] == list(range(count))
        assert api.get(P + '/status').json()['system']['global_stopped']
        assert not any(rp.enabled[:count])
        assert api.post(P + '/modulators/start-all', json={}).status_code == 200
        assert all(rp.enabled[:count])
    assert all(c[1] < count for c in rp.calls)


def test_unknown_and_stale_never_disable_saved_channels(tmp_path):
    path = tmp_path / 'config.db'
    db = Database(path)
    db.save('modulators', db.get('modulators', 0).model_copy(update={
        'enabled': True, 'frequency_khz': 98100, 'audio_mode': 'ANALOG'}), 0)
    original = stored(db)
    db.close()
    rp = controller(4)
    rp.available = False
    now = [0.0]
    app = create_app(Settings(path), rp=rp, clock=lambda: now[0])
    with TestClient(app) as api:
        assert api.get(P + '/modulators').json() == []
        assert api.get(P + '/status').json()['modulators'] == []
        for action in ('start', 'stop'):
            assert api.post(P + f'/modulators/0/{action}', json={}).status_code == 503
        assert api.get(P + '/modulators/0').status_code == 503
        assert api.post(P + '/modulators/stop-all', json={}).status_code == 200
        assert api.get(P + '/status').json()['system']['global_stopped']
        db = Database(path)
        assert stored(db) == original
        db.close()
        rp.available = True
        now[0] = 1
        app.state.control.executor.submit(app.state.services.status.refresh).result()
        rp.available = False
        app.state.control.executor.submit(app.state.services.status.refresh).result()
        assert len(api.get(P + '/modulators').json()) == 4
        assert api.get(P + '/system/info').json()['controller_capability_stale']
        db = Database(path)
        assert stored(db) == original
        db.close()


def test_removed_consumer_cleanup_retries_without_rds_or_command_replay(tmp_path):
    rp = controller(8)
    app = create_app(Settings(tmp_path / 'cleanup.db'), rp=rp)
    with TestClient(app):
        s = app.state.services
        def check():
            s.modulators.active_sources[6] = 999
            s.sources.release = Mock(side_effect=[DependencyUnavailable('teardown pending'), None])
            rp.physical_count = 4
            with pytest.raises(DependencyUnavailable, match='cleanup pending'):
                s.recovery._record_info(rp.get_info())
            assert not s.recovery.availability_ready
            assert s.modulators.active_sources[6] == 999
            s.rds.tick()  # Must not prevent the subsequent recovery observation.
            s.recovery.service()
            s.recovery.observe()
            assert s.recovery.availability_ready
            assert 6 not in s.modulators.active_sources
            assert s.sources.release.call_count == 2
        app.state.control.executor.submit(check).result()


def test_capacity_change_cancels_removed_work_and_preserves_settings(tmp_path):
    path = tmp_path / 'config.db'
    db = Database(path)
    config = db.get('modulators', 6).model_copy(update={
        'enabled': True, 'frequency_khz': 99900, 'audio_mode': 'ANALOG', 'rds_enabled': True})
    db.save('modulators', config, 6)
    expected = db.get('modulators', 6).model_dump() | {'enabled': False}
    db.close()
    rp = controller(8)
    app = create_app(Settings(path), rp=rp)
    with TestClient(app) as api:
        s = app.state.services
        def change(count):
            rp.physical_count = count
            s.recovery._record_info(rp.get_info())
            for _ in range(4):
                s.status.refresh()
        def shrink():
            rp.calls.clear()
            s.recovery.pending[6] = 'config'
            s.recovery.audio_pending[6] = 0
            s.rds.channels[6].needs_clear = True
            s.rt.overrides[6] = ('old', None)
            s.mux.attach(6, 123)
            change(4)
            s.recovery.service()
            s.rds.tick()
            assert 6 not in s.recovery.pending and 6 not in s.recovery.audio_pending
            assert 6 not in s.rt.overrides and 6 not in s.mux.producers
            assert not s.rds.channels[6].needs_clear
        app.state.control.executor.submit(shrink).result()
        assert all(c[1] < 4 for c in rp.calls)
        assert api.get(P + '/modulators/6').status_code == 404
        app.state.control.executor.submit(lambda: change(8)).result()
        result = api.get(P + '/modulators/6')
        assert result.status_code == 200 and result.json()['enabled'] is False
        assert not rp.enabled[6]
        db = Database(path)
        assert db.get('modulators', 6).model_dump() == expected
        db.close()
