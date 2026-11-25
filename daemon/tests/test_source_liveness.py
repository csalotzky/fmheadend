import threading,copy
import logging
import time
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from fmheadendd.sources.native_health import NativeHealth,accept
from fmheadendd.sources.source_liveness import evaluate,request_progress
from fmheadendd.tests.test_audio_worker import adapter
from fmheadendd.diagnostics import RecentLogs
from fmheadendd.sources.worker_recovery import WorkerRecovery


def probe(health,ident=1):return dict(id=ident,attempt=health.attempt,owner=health.generation,mode='progress')


def test_ack_is_not_completion_duplicate_and_backoff_not_interrupted():
    now=[100.];h=NativeHealth(lambda:now[0]);h.generation='g';h.begin_attempt();h.checkpoint('backoff',130,True)
    value=probe(h);assert h.receive_probe(value);first=h.probe
    now[0]=110;assert h.receive_probe(value) and h.probe is first
    assert h.probe[4]==0 and h.probe[5]=='pending'
    assert not h.receive_probe(probe(h,2))
    now[0]=130;h.begin_attempt()
    assert h.probe[4]==130 and h.probe[5]=='advanced_attempt'
    h.invalidate_probe();h.checkpoint(force=True);assert h.probe[5]=='cancelled'


def test_generation_attempt_and_malformed_request_fencing():
    h=NativeHealth();h.generation='g';h.begin_attempt()
    for change in ({'owner':'old'},{'attempt':2},{'id':True},{'mode':'rebuild'},{'extra':1}):
        value=probe(h);value.update(change);assert not h.receive_probe(value)
    assert h.probe is None
    value=probe(h);value['attempt']=0
    assert h.receive_probe(value) and h.probe[5]=='stale_attempt' and h.probe[4]==0


def setup(adapter,now):
    h=NativeHealth(lambda:now[0]);h.generation=adapter.generation;h.begin_attempt()
    adapter.source=SimpleNamespace(type='HTTP');adapter.native_health=h.snapshot()
    adapter.recovery_observation=lambda:(now[0],now[0]-100,'CONNECTING',None,False)
    return h


def test_ack_without_completion_and_native_deadline_not_rearmed(adapter):
    now=[100.];h=setup(adapter,now)
    with h.operation('source','teardown'):
        now[0]=111;adapter.native_health=h.snapshot()
        assert evaluate(adapter,111)['decision']=='await_operation_progress'
        request=adapter._source_probe;adapter._source_probe_sent=111
        assert h.receive_probe(request)
        now[0]=117;accept(adapter,h.snapshot(),117)
        assert evaluate(adapter,117)['decision']=='replace_candidate_native_unqualified'
        assert adapter._source_probe is request
        assert not adapter._stop.is_set() and not adapter.fatal_error


def test_source_checkpoint_request_completion_and_stop_fencing(adapter):
    now=[100.];h=setup(adapter,now);now[0]=106
    assert evaluate(adapter,106)['decision']=='request_source_progress'
    req=adapter._source_probe;assert request_progress(adapter,h.attempt)==req['id']
    adapter._source_probe_sent=106;h.receive_probe(req)
    now[0]=112;accept(adapter,h.snapshot(),112)
    assert evaluate(adapter,112)['decision']=='replace_candidate_source_unqualified'
    h.checkpoint(force=True);accept(adapter,h.snapshot(),112)
    assert adapter._source_probe_report[5]=='checkpoint'
    assert evaluate(adapter,114)['decision']=='continue_media_wait'
    adapter.stop();assert adapter._source_probe is None
    # Old generation report cannot complete a replacement generation's request.
    replacement=SimpleNamespace(generation='other',source_id=adapter.source_id)
    accept(replacement,h.snapshot(),114);assert not hasattr(replacement,'native_health')


@pytest.mark.parametrize('kind',['HTTP','LOCAL','PODCAST','ALSA','UDP','RTP'])
def test_missing_pcm_is_not_replace_decision(adapter,kind):
    now=[100.];h=setup(adapter,now);adapter.source=SimpleNamespace(type=kind)
    assert evaluate(adapter,100)['decision']=='continue_media_wait'
    assert not hasattr(adapter,'_source_probe')


def test_72_hour_logical_http_retries_have_no_replace_candidate(adapter):
    now=[100.];h=setup(adapter,now)
    for _ in range(8640):
        h.begin_attempt();h.checkpoint('backoff',now[0]+30,True);adapter.native_health=h.snapshot()
        adapter._liveness_next=0
        assert evaluate(adapter,now[0])['decision']=='continue_retry'
        now[0]+=30
    assert not hasattr(adapter,'_source_probe') and not adapter.fatal_error


def test_writer_stall_vs_disconnected_endpoint(adapter):
    now=[100.];h=setup(adapter,now);adapter._direct_intents={0:(1,1)}
    adapter.direct_progress={'writer_at':100,'connections':{'0':False}}
    assert evaluate(adapter,100)['decision']=='continue_delivery_retry'
    assert evaluate(adapter,106)['decision']=='replace_candidate_writer'


def test_previous_attempt_ack_cannot_spend_new_response_deadline(adapter):
    now=[100.];h=setup(adapter,now)
    request_progress(adapter,h.attempt);old=adapter._source_probe
    adapter._source_probe_sent=100
    h.begin_attempt();h.receive_probe(old);accept(adapter,h.snapshot(),100)
    assert adapter._source_probe_report[5]=='stale_attempt'
    now[0]=111;adapter.native_health=h.snapshot()
    assert evaluate(adapter,111)['decision']=='request_source_progress'
    assert adapter._source_probe['id']!=old['id']
    assert adapter._source_probe['attempt']==h.attempt


def test_backoff_probe_does_not_spend_post_backoff_response_allowance(adapter):
    now=[100.];h=setup(adapter,now);h.checkpoint('backoff',130,True)
    request_progress(adapter,h.attempt);adapter._source_probe_sent=100
    h.receive_probe(adapter._source_probe);accept(adapter,h.snapshot(),100)
    now[0]=130;assert evaluate(adapter,130)['decision']=='continue_retry'
    now[0]=136;assert evaluate(adapter,136)['decision']=='request_source_progress'
    now[0]=142;assert evaluate(adapter,142)['decision']=='replace_candidate_source_unqualified'


def test_delayed_initial_writer_grant_uses_existing_required_since_grace(adapter):
    now=[100.];h=setup(adapter,now);adapter._direct_intents={0:(1,1)}
    adapter.direct_progress={'writer_at':90,'connections':{'0':False}}
    adapter._direct_writer_required_since=100
    assert evaluate(adapter,100)['decision']=='continue_delivery_retry'
    assert evaluate(adapter,106)['decision']=='replace_candidate_writer'


def test_ipc_failure_and_worker_exit_are_separate(adapter):
    now=[100.];setup(adapter,now)
    adapter.recovery_observation=lambda:(90,100,'RUNNING',None,False)
    assert evaluate(adapter,100)['decision']=='replace_candidate_ipc'
    adapter.recovery_observation=lambda:(101,100,'ERROR','exit -9',True)
    assert evaluate(adapter,102)['decision']=='replace_candidate_worker_exit'


def test_healthy_shadow_observations_never_log(adapter, caplog):
    caplog.set_level(logging.DEBUG)
    now = [100.]
    h = setup(adapter, now)
    adapter._has_pcm = True
    adapter.recovery_observation = lambda: (now[0], now[0], 'RUNNING', None, False)
    with RecentLogs() as logs:
        for tick in (100, 101, 102, 401, 1000, 10000):
            now[0] = tick
            h.checkpoint(force=True)
            adapter.native_health = h.snapshot()
            value = evaluate(adapter, tick)
            assert value['decision'] == 'continue_playback'
            assert adapter.shadow_decision is value and value['observed_at'] == tick
        assert logs.snapshot() == []
    assert caplog.records == []


def test_suspicious_shadow_logs_only_material_changes(adapter, caplog):
    caplog.set_level(logging.DEBUG)
    now = [100.]
    h = setup(adapter, now)
    with h.operation('source', 'teardown'):
        adapter.native_health = h.snapshot()
        with RecentLogs() as logs:
            now[0] = 111
            assert evaluate(adapter, 111)['decision'] == 'await_operation_progress'
            assert len(logs.snapshot()) == 1
            request = adapter._source_probe
            for tick in (112, 113, 115):
                now[0] = tick
                assert evaluate(adapter, tick)['decision'] == 'await_operation_progress'
            assert len(logs.snapshot()) == 1
            # Escalation remains visible inside the former five-minute limiter.
            for tick in (117, 118, 418, 1000):
                now[0] = tick
                assert evaluate(adapter, tick)['decision'] == 'replace_candidate_native_unqualified'
            assert len(logs.snapshot()) == 2
            assert adapter._source_probe is request
    # A distinct operation is useful even when the decision enum is unchanged.
    now[0] = 1001
    with h.operation('source', 'teardown'):
        adapter.native_health = h.snapshot()
        now[0] = 1018
        assert evaluate(adapter, 1018)['decision'] == 'replace_candidate_native_unqualified'
    assert len(caplog.records) == 3
    assert all(record.levelno == logging.INFO for record in caplog.records)
    assert all('observation_only=true' in record.message for record in caplog.records)
    assert not adapter._stop.is_set() and not adapter.fatal_error


@pytest.mark.parametrize('condition', ['exit', 'fault', 'ipc', 'writer'])
def test_substantiated_shadow_fault_warns_once(adapter, caplog, condition):
    caplog.set_level(logging.DEBUG)
    now = [100.]
    setup(adapter, now)
    adapter.recovery_observation = lambda: (
        90 if condition == 'ipc' else now[0], now[0], 'RUNNING',
        'worker fault' if condition == 'fault' else None, condition == 'exit')
    if condition == 'writer':
        adapter._direct_intents = {0: (1, 1)}
        adapter.direct_progress = {'writer_at': 90}
    for tick in (100, 101, 401, 1000):
        now[0] = tick
        assert evaluate(adapter, tick)['decision'].startswith('replace_candidate_')
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert 'observation_only=true' in caplog.records[0].message


def test_suspicion_after_healthy_playback_is_a_new_episode(adapter, caplog):
    caplog.set_level(logging.INFO)
    now = [100.]
    h = setup(adapter, now)
    adapter._has_pcm = True
    adapter.recovery_observation = lambda: (now[0], now[0], 'RUNNING', None, False)
    adapter._direct_intents = {0: (1, 1)}
    adapter.direct_progress = {'writer_at': 90}
    assert evaluate(adapter, 100)['decision'] == 'replace_candidate_writer'
    now[0] = 101
    h.checkpoint(force=True)
    adapter.native_health = h.snapshot()
    adapter.direct_progress['writer_at'] = 101
    assert evaluate(adapter, 101)['decision'] == 'continue_playback'
    now[0] = 107
    assert evaluate(adapter, 107)['decision'] == 'replace_candidate_writer'
    assert len(caplog.records) == 2


def test_shadow_candidate_does_not_trigger_worker_recovery(adapter, caplog):
    caplog.set_level(logging.INFO)
    now = [time.monotonic() - 100]
    h = setup(adapter, now)
    adapter._has_pcm = True
    adapter.recovery_observation = lambda: (now[0], now[0], 'RUNNING', None, False)
    adapter._fail = Mock()
    adapter.stop = Mock()
    manager = SimpleNamespace(clock=lambda: now[0], pipelines={adapter.source_id: adapter},
                              consumers={adapter.source_id: {0}})
    recovery = WorkerRecovery(manager)
    with h.operation('source', 'teardown'):
        now[0] = time.monotonic()
        adapter.native_health = h.snapshot()
        recovery.service()
    assert adapter.shadow_decision['decision'] == 'replace_candidate_native_unqualified'
    assert recovery.pending == {} and recovery.attempts == {}
    assert manager.pipelines[adapter.source_id] is adapter
    adapter._fail.assert_not_called()
    adapter.stop.assert_not_called()
    assert not adapter._stop.is_set()
    assert len(caplog.records) == 1
    assert 'observation_only=true' in caplog.records[0].message
