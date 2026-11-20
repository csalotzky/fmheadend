import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fmheadendd.diagnostics import RecentLogs
from fmheadendd.sources.diagnostic_events import DiagnosticEvents, accept, audio_recovered, redact
from fmheadendd.sources.worker_recovery import WorkerRecovery
from fmheadendd.tests.test_audio_worker import adapter


def test_bounded_repetition_summary_redaction():
    now=[10.0]; events=DiagnosticEvents(lambda:now[0])
    events.error('http_404','Not Found https://user:password@host/path?token=secret')
    first=events.poll(); assert first['count']==1 and first['message']=='Not Found https://host/path'
    from fmheadendd.sources.audio_ipc import envelope, pack
    assert pack(envelope(14, 'a'*32, 1, 'diagnostic', first))
    for _ in range(10000):events.error('http_404','Not Found https://user:password@host/path?token=secret')
    assert events.poll() is None
    now[0]=310; assert events.poll()['count']==10000
    for i in range(100):events.error('media_error',str(i))
    assert len(events.entries)<=8


@pytest.mark.parametrize('mutate',[
    lambda e:e.update(message='x'*513),lambda e:e.update(count=True),
    lambda e:e.update(category='bad\nlog'),lambda e:e.update(first=float('nan')),
    lambda e:e.update(last=float('inf')),lambda e:e.update(extra=1),
])
def test_invalid_events_do_not_fail_worker_or_log(mutate):
    event=dict(category='http_404',message='Not Found',count=1,first=0,last=0);mutate(event)
    with RecentLogs() as logs:accept(SimpleNamespace(),event);assert not logs.snapshot()


def test_parent_recovery_once_and_stale_summary(caplog):
    caplog.set_level('INFO', logger='fmheadendd.sources.events')
    p=SimpleNamespace(source_id=14,generation='generation',_has_pcm=False)
    now=time.monotonic()
    with RecentLogs() as logs:
        accept(p,dict(category='http_503',message='https://u:p@host/a?key=secret',count=1,first=now,last=now))
        audio_recovered(p);audio_recovered(p)
        rows=logs.snapshot();assert len(rows)==2
        assert rows[0]['severity']=='INFO' and 'audio_recovered' in rows[0]['message']
        assert 'secret' not in str(rows) and 'u:p' not in str(rows)
        p._diagnostic_next=0;p._has_pcm=True;p._last_pcm=now+1
        accept(p,dict(category='http_503',message='old error',count=10,first=now,last=now))
        assert p._diagnostic_outage_since is None


def test_timeout_reason_and_finite_cooldown_logged():
    old=SimpleNamespace(generation='g',recovery_exhausted=False,source=SimpleNamespace(type='HTTP'),
        recovery_observation=lambda:(100,0,'CONNECTING',None,False),_fail=Mock(),stop=Mock())
    m=SimpleNamespace(clock=lambda:100,pipelines={14:old},consumers={14:{0}},statuses={},restart_errors={})
    recovery=WorkerRecovery(m)
    with RecentLogs() as logs:
        recovery.service()
        assert 14 in recovery.pending and recovery.attempts[14]==1
        assert any('Worker PCM progress timeout' in r['message'] and r['severity']=='ERROR' for r in logs.snapshot())
        old.recovery_observation=lambda:(100,0,'ERROR','Worker PCM progress timeout',True)
        recovery.service()
        assert recovery.pending[14][2]==101
        assert not old.recovery_exhausted
        assert any('category=worker_cooldown' in r['message'] for r in logs.snapshot())
        assert not any('restart_budget_exhausted' in r['message'] for r in logs.snapshot())


def test_eight_idle_reporters_no_extra_periodic_events():
    from fmheadendd.sources.worker_reporting import WorkerReporter
    now=[0.0];sent=[];reporters=[]
    for _ in range(8):
        p=SimpleNamespace(diagnostic_events=DiagnosticEvents(lambda:now[0]),status_revision=0,
            fatal_error=None,snapshot=lambda:{},pcm_snapshot=lambda:{'staging':{}},_telemetry_request=(False,50))
        reporters.append(WorkerReporter(p,None,None,lambda k,v:sent.append((k,v)),lambda:now[0]))
    for i in range(1001):
        now[0]=i/10
        for reporter in reporters:reporter.poll()
    assert sum(k=='heartbeat' for k,v in sent)==808
    assert sum(k=='status' for k,v in sent)==8
    assert not any(k=='diagnostic' for k,v in sent)


def test_reap_exception_is_visible_without_changing_wait_sequence(adapter):
    adapter.process=Mock();adapter.process.wait.side_effect=[RuntimeError('https://u:p@host/a?secret=yes'),0]
    with RecentLogs() as logs:
        adapter._reap_child([],None)
        assert adapter.exit_code==0
        adapter.process.terminate.assert_called_once()
        rows=logs.snapshot()
        assert len(rows)==1 and rows[0]['severity']=='ERROR'
        assert 'category=reap_failure' in rows[0]['message'] and 'secret' not in str(rows)
