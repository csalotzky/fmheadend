"""Clock-driven policy tests: no hardware, real waiting or skipped failures."""
from itertools import count
from types import SimpleNamespace
from threading import Lock
import pytest
from fmheadendd.errors import DependencyUnavailable, SourcePending
from fmheadendd.diagnostics import RecentLogs
from fmheadendd.sources.worker_recovery import WorkerRecovery, COOLDOWNS


class Lab:
    def __init__(self):
        self.now=1000.;self.ids=count();self.created=[];self.fail_build=0;self.fail_start=0
        self.m=SimpleNamespace(clock=lambda:self.now,pipelines={},consumers={1:{0,1}},statuses={},restart_errors={},_registry_lock=Lock())
        self.m._publish_pipeline=lambda sid,p:self.m.pipelines.update({sid:p})
        self.m._new_pipeline=lambda source,**kw:self.new(source)
        self.r=WorkerRecovery(self.m);self.m.pipelines[1]=self.new()
    def new(self,source=None):
        if self.fail_build:self.fail_build-=1;raise DependencyUnavailable('temporary missing dependency')
        p=Adapter(self,source);self.created.append(p);return p
    @property
    def current(self):return self.m.pipelines[1]
    def failure(self):
        self.current._fail('controlled worker failure');self.r.service();self.r.service()
        return self.r.pending[1][2]-self.now
    def expire(self):
        self.now=self.r.pending[1][2];self.r.service()


class Adapter:
    def __init__(self,lab,source=None):
        self.lab=lab;self.generation=str(next(lab.ids));self.source=source or SimpleNamespace(type='HTTP',config={})
        self.output=object();self.fatal_error=None;self.stopped=False;self.reaped=True;self.confirm_reap=True
        self.started=False;self._has_pcm=False;self.pcm_at=lab.now;self.state='CONNECTING';self.direct_runtime=None
    def recovery_observation(self):return self.lab.now,self.pcm_at,self.state,self.fatal_error,self.stopped and self.reaped
    def _fail(self,reason):self.fatal_error=reason
    def stop(self):
        self.stopped=True
        if not self.reaped or not self.confirm_reap:raise DependencyUnavailable('reap pending')
    def fresh(self):return self.lab.new(self.source)
    def start(self):
        self.started=True
        if self.lab.fail_start:self.lab.fail_start-=1;raise DependencyUnavailable('spawn failed')


def test_exact_sequence_twenty_failures_and_no_window(caplog):
    caplog.set_level('INFO');lab=Lab();delays=[]
    with RecentLogs() as logs:
        for i in range(20):
            old=lab.current;delays.append(lab.failure());due=lab.r.pending[1][2]
            lab.now=due-.001;lab.r.service();assert lab.current is old
            lab.now=due;lab.r.service();assert lab.current is not old and lab.current.started
            assert old.stopped and old.reaped and lab.m.consumers[1]=={0,1}
        assert delays==list(COOLDOWNS)+[120]*13
        assert lab.r.attempts[1]==20
        assert any('category=worker_cooldown' in row['message'] for row in logs.snapshot())
        assert not any('exhaust' in row['message'] for row in logs.snapshot())


def test_72_hours_never_exhausts():
    lab=Lab();end=lab.now+72*3600;n=0
    while lab.now<end:assert 1<=lab.failure()<=120;lab.expire();n+=1
    assert n>2000 and lab.current.started and lab.r.attempts[1]==n


def test_replacement_build_and_start_failures_keep_retrying():
    lab=Lab();lab.fail_build=3;lab.failure()
    for _ in range(3):
        lab.expire();assert 1 in lab.r.pending
        lab.r.service();assert lab.r.pending[1][2]>lab.now
    lab.fail_start=1;lab.expire();failed=lab.current;assert failed.started and failed.stopped
    lab.r.service();lab.expire();assert lab.current is not failed and lab.current.started


def test_actual_recovery_logs_remain_visible(caplog):
    caplog.set_level('INFO')
    lab = Lab()
    old = lab.current
    old.state = 'RUNNING'
    old.pcm_at = lab.now - 16
    with RecentLogs() as logs:
        lab.r.service()
        assert old.stopped and 1 in lab.r.pending
        lab.r.service()
        lab.fail_build = 1
        lab.expire()
        lab.r.service()
        lab.expire()
        assert lab.current is not old and lab.current.started
        messages = [row['message'] for row in logs.snapshot()]
        for category in ('worker_recovery', 'worker_cooldown', 'replacement_failed', 'worker_replacement'):
            assert any(f'category={category} ' in message for message in messages)
    assert 'Worker PCM progress timeout' in caplog.text


def test_stability_requires_progress_observation_and_full_300_seconds(caplog):
    caplog.set_level('INFO');lab=Lab();lab.r.attempts[1]=6;p=lab.current;p._has_pcm=True;p.state='RUNNING'
    lab.r.service();lab.now+=299;p.pcm_at=lab.now;lab.r.service()
    assert lab.r.attempts[1]==6  # Unobserved 299 seconds do NOT qualify.
    for i in range(299):lab.now+=1;p.pcm_at=lab.now;lab.r.service()
    assert lab.r.attempts[1]==6
    with RecentLogs() as logs:
        lab.now+=1;p.pcm_at=lab.now;lab.r.service()
        assert 1 not in lab.r.attempts
        assert 'worker_failure_counter_reset' in logs.snapshot()[0]['message']
    assert lab.failure()==1


def test_one_pcm_heartbeat_or_running_state_cannot_reset():
    lab=Lab();lab.r.attempts[1]=7;p=lab.current;p._has_pcm=True;p.state='RUNNING'
    lab.r.service()
    for _ in range(301):lab.now+=1;lab.r._stable(1,p,p.recovery_observation(),lab.now)
    assert lab.r.attempts[1]==7


@pytest.mark.parametrize('fault',['state','writer','connection','poll_gap'])
def test_stability_interruption_restarts_window(fault):
    lab=Lab();p=lab.current;p._has_pcm=True;p.state='RUNNING';lab.r.attempts[1]=4
    p.direct_runtime='lab';p._direct_intents={0:(1,1)};p.direct_progress={'writer_at':lab.now,'connections':{'0':True}}
    for _ in range(290):
        lab.now+=1;p.pcm_at=lab.now;p.direct_progress['writer_at']=lab.now;lab.r.service()
    if fault=='state':p.state='BUFFERING'
    if fault=='writer':p.direct_progress['writer_at']-=5
    if fault=='connection':p.direct_progress['connections']['0']=False
    if fault=='poll_gap':lab.now+=3;p.pcm_at=lab.now;p.direct_progress['writer_at']=lab.now
    lab.r.service()
    p.state='RUNNING';p.direct_progress['connections']['0']=True
    for _ in range(20):lab.now+=1;p.pcm_at=lab.now;p.direct_progress['writer_at']=lab.now;lab.r.service()
    assert lab.r.attempts[1]==4


def test_stop_or_disabled_last_consumer_cancels_cooldown():
    lab=Lab();lab.failure();old=lab.current
    lab.m.consumers[1].clear();lab.r.request(1);lab.r.service();lab.now+=10000;lab.r.service()
    assert not lab.m.pipelines and not lab.r.pending and not lab.r.attempts
    assert old.stopped


def test_config_supersedes_cooldown_and_late_reap_uses_latest():
    lab=Lab();lab.failure();old=lab.current;old.reaped=False
    one=SimpleNamespace(type='HTTP',config={'uri':'one'});two=SimpleNamespace(type='HTTP',config={'uri':'two'})
    lab.r.request(1,one);lab.r.request(1,two);lab.now+=1000;lab.r.service();assert lab.current is old
    old.reaped=True;lab.r.service();assert lab.current.source is two and lab.current.started
    lab.r.service();assert len(lab.created)==2


def test_reap_confirmation_blocks_start_and_cooldown_starts_after_reap():
    lab=Lab();old=lab.current;old.reaped=False
    old._fail('failed');lab.r.service();lab.now+=1000;lab.r.service();assert lab.r.pending[1][2] is None
    old.reaped=True;old.confirm_reap=False;lab.r.service();assert lab.current is old
    old.confirm_reap=True;lab.r.service();assert lab.r.pending[1][2]==lab.now+1
    lab.expire();assert lab.current is not old


def test_shared_consumer_detach_does_not_cancel_other_consumer():
    lab=Lab();lab.failure();lab.m.consumers[1].remove(0);lab.expire()
    assert lab.current.started and lab.m.consumers[1]=={1}


def test_stop_arriving_during_replacement_start_keeps_new_cleanup_owned():
    lab=Lab();lab.failure();construct=lab.new
    def fresh(source=None):
        p=construct(source)
        def start():
            p.started=True;lab.m.consumers[1].clear();lab.r.request(1)
        p.start=start
        return p
    lab.new=fresh;lab.expire()
    assert lab.r.pending[1][0] is lab.current
    lab.r.service();lab.now+=1000;lab.r.service()
    assert not lab.m.pipelines and not lab.r.pending and len(lab.created)==2


def test_http_retry_and_shadow_overdue_alone_do_not_count_failures():
    lab=Lab();p=lab.current
    p.native_health={'slots':[['overdue shadow observation']]}
    for _ in range(89):lab.now+=1;lab.r.service()
    assert not lab.r.attempts and not lab.r.pending
    lab.now+=2;lab.r.service()
    assert lab.r.attempts[1]==1  # Existing HTTP PCM timeout is explicitly deferred to Phase 4.
