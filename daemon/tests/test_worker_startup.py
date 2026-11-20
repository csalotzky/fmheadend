"""STARTUP/STEADY fencing with a controllable clock and no child processes."""
from types import SimpleNamespace
import threading
import pytest

from fmheadendd.errors import SourcePending
from fmheadendd.sources import process_pipeline as backend, worker_reporting
from fmheadendd.sources.audio_ipc import envelope, ProtocolError
from fmheadendd.sources.worker_recovery import WorkerRecovery, RecoveryPolicy
from fmheadendd.tests.test_audio_worker import adapter


@pytest.fixture
def startup(adapter, monkeypatch):
    now = [1000.]
    clock = lambda: now[0]
    fake_time = SimpleNamespace(monotonic=clock, monotonic_ns=lambda: int(clock()*1e9))
    monkeypatch.setattr(backend, 'time', fake_time)
    monkeypatch.setattr(worker_reporting, 'time', fake_time)
    class NoThread:
        def __init__(self, **kw): pass
        def start(self): pass
    monkeypatch.setattr(backend, 'threading', SimpleNamespace(Thread=NoThread,
        Event=threading.Event, Lock=threading.Lock))
    p = adapter
    p._last_status = p._last_pcm = clock()
    with pytest.raises(SourcePending): p.start()
    try: yield p, now, clock
    finally:
        p._stop.set()
        with backend._owners_lock: backend._owners.discard(p)


def status(p, sequence=1, **changes):
    payload = dict(status={'state':'CONNECTING'}, staging={}, fatal_error=None)
    payload.update(changes)
    return envelope(p.source_id,p.generation,sequence,'status',payload)


def beat(p, sequence=1, payload=None):
    return envelope(p.source_id,p.generation,sequence,'heartbeat',{} if payload is None else payload)


@pytest.mark.parametrize('kind',['status','heartbeat'])
def test_initial_report_just_before_deadline_establishes_runtime(startup, kind):
    p, now, clock = startup; deadline = p._startup_deadline
    now[0] = deadline-.001
    p._accept('control', status(p) if kind=='status' else beat(p))
    first = p._initial_liveness_at
    assert first == now[0] and p._startup_deadline == deadline
    now[0] += 5
    assert p.status_timeout_reason(clock,5) is None
    now[0] += .001
    assert p.status_timeout_reason(clock,5) == 'Worker status timeout'
    p._accept('control',beat(p,2))
    assert p._initial_liveness_at == first and p._startup_deadline == deadline


@pytest.mark.parametrize('offset',[0,.001])
@pytest.mark.parametrize('kind',['status','heartbeat'])
def test_at_or_after_deadline_report_cannot_revive_startup(startup, offset, kind):
    p, now, clock = startup; now[0] = p._startup_deadline+offset
    p._accept('control',status(p) if kind=='status' else beat(p))
    assert p._initial_liveness_at is None
    assert p.status_timeout_reason(clock,5) == 'Worker startup timeout'
    p._accept('control',beat(p,2))
    assert p._initial_liveness_at is None


def test_repeated_accepted_prepared_and_start_do_not_extend(startup):
    p,now,clock=startup;deadline=p._startup_deadline
    for sequence in range(1,101):
        now[0] += .1
        p._accept('control',envelope(p.source_id,p.generation,sequence,'accepted',{'request':sequence}))
        with pytest.raises(SourcePending): p.start()
        assert p._startup_deadline == deadline and p._initial_liveness_at is None
    p._accept('control',envelope(p.source_id,p.generation,101,'prepared',{'request':1}))
    now[0]=deadline
    assert p.status_timeout_reason(clock,5) == 'Worker startup timeout'


@pytest.mark.parametrize('message',[
    lambda p:status(p,staging={'queued_bytes':-1}),
    lambda p:status(p,fatal_error=42),
    lambda p:beat(p,payload={'unexpected':1}),
])
def test_malformed_liveness_never_establishes_handshake(startup,message):
    p,now,clock=startup
    with pytest.raises((ValueError,ProtocolError)): p._accept('control',message(p))
    assert p._initial_liveness_at is None
    now[0]=p._startup_deadline
    assert p.status_timeout_reason(clock,5) == 'Worker startup timeout'


def test_malformed_optional_health_cannot_establish_handshake(startup):
    p,now,clock=startup
    p._accept('control',beat(p,payload={'health':{'version':999}}))
    assert p._initial_liveness_at is None
    now[0]=p._startup_deadline
    assert p.status_timeout_reason(clock,5) == 'Worker startup timeout'


@pytest.mark.parametrize('retire',['stop','fatal','expired'])
def test_late_handshake_cannot_reverse_retirement(startup, retire):
    p,now,clock=startup
    if retire=='stop': p._stop.set()
    elif retire=='fatal': p._fail('actual startup failure')
    else:
        now[0]=p._startup_deadline
        assert p.status_timeout_reason(clock,5)=='Worker startup timeout'
    p._accept('control',beat(p))
    assert p._initial_liveness_at is None


def test_stale_generation_and_new_owner(startup):
    p,now,clock=startup
    p._accept('control',envelope(p.source_id,'f'*32,1,'heartbeat',{}))
    assert p.stale_messages==1 and p._initial_liveness_at is None
    now[0] += 8
    replacement=p.fresh()
    assert replacement.generation!=p.generation and replacement._startup_deadline is None
    assert replacement._initial_liveness_at is None
    assert p.status_timeout_reason(clock,5) is None


def test_real_supervisor_startup_crash_fatal_and_cooldown(startup):
    p,now,clock=startup
    m=SimpleNamespace(clock=clock,pipelines={1:p},consumers={1:{0,1}},statuses={},restart_errors={})
    r=WorkerRecovery(m);requests=[]
    r.request=lambda sid,**kw:requests.append((sid,kw))
    now[0] += 6
    r.service(); assert not requests
    now[0]=p._startup_deadline
    r.service()
    assert requests==[(1,{'automatic':True})] and p.fatal_error=='Worker startup timeout'
    assert m.consumers[1]=={0,1}


@pytest.mark.parametrize('fault',['crash','fatal'])
def test_failure_priority_during_startup(startup,fault):
    p,now,clock=startup
    if fault=='crash':p._done.set()
    else:p._fail('fatal before first status')
    m=SimpleNamespace(clock=clock,pipelines={1:p},consumers={1:{0}},statuses={},restart_errors={})
    r=WorkerRecovery(m);requests=[];r.request=lambda *a,**kw:requests.append(kw)
    r.service();assert requests==[{'automatic':True}]
    assert p.fatal_error==('Worker exited' if fault=='crash' else 'fatal before first status')


@pytest.mark.parametrize('offset',[-.001,0,.001])
def test_valid_fatal_status_has_priority_without_late_handshake(startup,offset):
    p,now,clock=startup;now[0]=p._startup_deadline+offset
    p._accept('control',status(p,fatal_error='genuine pipeline failure'))
    assert p.fatal_error=='genuine pipeline failure'
    if offset>=0:assert p._initial_liveness_at is None
    m=SimpleNamespace(clock=clock,pipelines={1:p},consumers={1:{0}},statuses={},restart_errors={})
    r=WorkerRecovery(m);requests=[];r.request=lambda *a,**kw:requests.append(kw)
    r.service();assert requests==[{'automatic':True}]
    assert p.fatal_error=='genuine pipeline failure'
