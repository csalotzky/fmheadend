"""Publication inputs reuse the ordinary radiotext selection semantics."""
from unittest.mock import patch

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSourceConfig, ModulatorConfig, RdsProfileConfig, RtOverride, RtStatus
from fmheadendd.rds.radiotext import RadioTextService
from fmheadendd.sources.manager import SourceManager


@pytest.fixture
def selection():
    db = Database(':memory:')
    sources = SourceManager(db)
    now = [0.0]
    rt = RadioTextService(db, sources, lambda: now[0])
    source = db.save('sources', InputSourceConfig(name='Station', type='HTTP',
        config={'uri': 'https://example.org/live'},
        rds_profile=RdsProfileConfig(rds_rt='Source fallback', rds_metadata_to_rt=True)))
    db.save('modulators', ModulatorConfig(input_source_id=source.id,
        rds_rt='TX fallback', rds_metadata_to_rt=True), 0)
    yield db, sources, rt, source.id, now
    db.close()


@pytest.mark.parametrize('inputs', ['none', 'config', 'statuses', 'both'])
@pytest.mark.parametrize('follow,title,expected,origin', [
    (True, None, 'Source fallback', 'fallback'),
    (False, None, 'TX fallback', 'fallback'),
    (True, 'Artist - Track', 'Artist - Track', 'metadata'),
    (False, 'x' * 80, 'x' * 64, 'metadata'),
    (True, '', 'Source fallback', 'fallback'),
])
def test_resolved_and_lookup_paths_agree(selection, inputs, follow, title, expected, origin):
    db, sources, rt, sid, _ = selection
    config = db.save('modulators', db.get('modulators', 0).model_copy(
        update={'rds_follow_source': follow}), 0)
    observation = sources.status(sid).model_copy(update={'stream_title': title}, deep=True)
    sources.statuses[sid] = observation
    kwargs = {}
    if inputs in ('config', 'both'):
        kwargs['config'] = config
    if inputs in ('statuses', 'both'):
        kwargs['source_statuses'] = {sid: observation}
    before = config.model_dump(), observation.model_dump()
    with patch.object(db, 'get', wraps=db.get) as get, patch.object(sources, 'status', wraps=sources.status) as status:
        assert rt.effective(0, **kwargs) == RtStatus(text=expected, origin=origin)
        assert status.call_count == (0 if 'source_statuses' in kwargs else 1)
        modulator_reads = [c for c in get.call_args_list if c.args == ('modulators', 0)]
        assert len(modulator_reads) == (0 if 'config' in kwargs else 1)
        if inputs == 'both':
            get.assert_not_called()
    # Selection also applies to disabled TXes and idle sources; inputs are not mutated.
    assert not config.enabled and observation.state == 'IDLE'
    assert before == (config.model_dump(), observation.model_dump())


@pytest.mark.parametrize('preloaded', [False, True])
def test_runtime_override_ttl_empty_text_and_clear(selection, preloaded):
    db, sources, rt, sid, now = selection
    observation = sources.status(sid)
    observation.stream_title = 'Current metadata'
    kwargs = dict(config=db.get('modulators', 0), source_statuses={sid: observation}) if preloaded else {}
    rt.set(0, RtOverride(text='Runtime override', ttl_s=1))
    now[0] = .25
    assert rt.effective(0, **kwargs) == RtStatus(text='Runtime override', origin='override', remaining_ttl_s=.75)
    now[0] = 1.0  # Exact expiry boundary, not just after it.
    assert rt.effective(0, **kwargs) == RtStatus(text='Current metadata', origin='metadata')
    assert not rt.overrides
    rt.set(0, RtOverride(text=''))
    now[0] = 100
    with patch.object(sources, 'status', side_effect=AssertionError('override must not read metadata')):
        assert rt.effective(0, **kwargs) == RtStatus(text='', origin='override')
    rt.clear(0)
    assert rt.effective(0, **kwargs).text == 'Current metadata'


@pytest.mark.parametrize('state', ['IDLE', 'ERROR'])
def test_empty_observation_uses_fallback_without_resampling(selection, state):
    db, sources, rt, sid, _ = selection
    config = db.get('modulators', 0)
    observation = sources.status(sid).model_copy(update={'state': state, 'stream_title': None})
    with patch.object(sources, 'status', side_effect=AssertionError('must use collected observation')):
        assert rt.effective(0, config=config, source_statuses={sid: observation}).text == 'TX fallback'


@pytest.mark.parametrize('preloaded', [False, True])
def test_missing_observation_retains_source_lookup_error(selection, preloaded):
    db, sources, rt, sid, _ = selection
    kwargs = dict(config=db.get('modulators', 0), source_statuses={}) if preloaded else {}
    failure = DomainError(404, 'NOT_FOUND', 'Source no longer exists')
    with patch.object(sources, 'status', side_effect=failure) as status:
        with pytest.raises(DomainError) as error:
            rt.effective(0, **kwargs)
        assert error.value is failure
        status.assert_called_once_with(sid)
    with pytest.raises(DomainError) as error:
        rt.effective(999, source_statuses={})
    assert error.value.status == 404


@pytest.mark.parametrize('assigned,metadata', [(False, True), (True, False)])
def test_no_source_or_metadata_disabled_does_not_read_observations(selection, assigned, metadata):
    db, sources, rt, sid, _ = selection
    config = db.save('modulators', ModulatorConfig(input_source_id=sid if assigned else None,
        rds_metadata_to_rt=metadata, rds_rt='TX fallback'), 0)
    with patch.object(sources, 'status', side_effect=AssertionError('metadata is not requested')):
        assert rt.effective(0).text == rt.effective(0, config=config, source_statuses={}).text == 'TX fallback'


def test_invalid_follow_source_still_fails_during_configuration_resolution(selection):
    db, _, rt, sid, _ = selection
    config = db.get('modulators', 0).model_copy(update={'rds_follow_source': True})
    db.save('modulators', config, 0)
    # Simulate corrupt persisted input in this isolated in-memory database.
    db.connection.execute('UPDATE sources SET rds_profile_id=NULL WHERE id=?', (sid,))
    for read in (lambda: rt.effective(0), lambda: db.list('modulators')):
        with pytest.raises(DomainError) as error:
            read()
        assert error.value.error == 'INVALID_RDS_FOLLOW'
