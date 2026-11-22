"""Accepted audio preparation is distinct from persistence and RF completion."""
from unittest.mock import Mock
import pytest
from fmheadendd.errors import SourcePending, SourceStartupFailed
from fmheadendd.tests.test_modulator_edit_policy import running


@pytest.mark.parametrize('change_source',[False,True])
@pytest.mark.parametrize('outcome',['ready','failure','exhausted','stop','stop-all'])
def test_saved_restart_pending_then_authoritative_outcome(running,change_source,outcome):
    api,s,sid=running
    p='/api/v1/modulators/0'
    if change_source:
        sid=api.post('/api/v1/sources',json=dict(name='New',type='HTTP',config={'uri':'http://example.org/new'})).json()['id']
    acquire=s.sources.acquire
    blocked=[True]
    def pending(source,tx):
        if blocked[0]:raise SourcePending('HTTP source cleanup/replacement pending')
        if outcome in ('failure','exhausted'):
            raise SourceStartupFailed('Audio worker restart budget exhausted' if outcome=='exhausted' else 'Decoder failed')
        return acquire(source,tx)
    s.sources.acquire=pending
    s.rp.set_tx_enabled=Mock(wraps=s.rp.set_tx_enabled)
    result=api.post(p+'/save-and-resume',json=dict(input_source_id=sid,rf_attenuation_db=9)).json()
    assert result['saved'] and result['restart_pending'] and not result['restarted']
    assert result['failed_stage'] is None
    assert result['configuration']['input_source_id']==sid
    assert not s.rp.enabled[0]
    row=s.modulators.statuses[0]
    assert row.start_pending and row.source_state=='CONNECTING' and row.source_last_error is None
    status = api.get('/api/v1/status').json()['modulators'][0]
    assert status['intent_revision'] == result['intent_revision'] and status['intent_action'] == 'SAVE'
    assert status['desired_on'] and status['start_pending'] and status['start_error'] is None
    assert 0 in s.recovery.audio_pending
    if outcome in ('stop','stop-all'):
        api.post(p+'/stop' if outcome=='stop' else '/api/v1/modulators/stop-all')
    blocked[0]=False
    s.recovery.clock=lambda:100
    api.app.state.control.executor.submit(s.recovery.service).result()
    api.app.state.control.executor.submit(s.status.refresh).result()
    status = api.get('/api/v1/status').json()['modulators'][0]
    assert not row.start_pending
    if outcome=='ready':
        assert s.rp.enabled[0] and row.tx_state=='ON'
        assert row.source_last_error is None
        assert status['intent_revision'] == result['intent_revision'] and status['desired_on']
    else:
        assert not s.rp.enabled[0]
        if outcome in ('failure','exhausted'):
            assert row.source_state=='ERROR' and row.source_last_error
            assert status['intent_revision'] == result['intent_revision'] and not status['desired_on']
            assert status['start_error'] == row.source_last_error
        else:
            assert 0 not in s.recovery.audio_pending
            assert status['intent_revision'] > result['intent_revision'] and status['intent_action'] == 'STOP'
            assert not status['desired_on'] and status['start_error'] is None


def test_manual_cleanup_snapshot_and_generation_remain_owned(running):
    api,s,sid=running
    old=s.sources.pipelines[sid]
    s.sources.worker_recovery.pending[sid]=(old,None,100,False)
    try:
        with pytest.raises(SourcePending):s.sources.acquire(sid,0)
        assert api.app.state.control.executor.submit(lambda:s.sources.status(sid).state).result()=='CONNECTING'
        assert s.sources.pipelines[sid] is old
    finally:s.sources.worker_recovery.pending.pop(sid)
