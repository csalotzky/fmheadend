import copy,threading
from types import SimpleNamespace
import pytest
from fmheadendd.sources.native_health import NativeHealth,accept
from fmheadendd.sources.source_liveness import healthy_http_retry_exemption as exempt
from fmheadendd.tests.test_worker_cooldown import Lab
from fmheadendd.tests.test_audio_worker import adapter

def setup():
    lab=Lab();p=lab.current;p._lock=threading.RLock();p._stop=threading.Event();p._done=threading.Event()
    p.reconnect=True;p.source_id=1;p._pcm_error=None
    h=NativeHealth(lambda:lab.now);h.generation=p.generation;h.begin_attempt()
    h.http_retry=(1,1000.,1030.)
    return lab,p,h

def report(lab,p,h):accept(p,h.snapshot(),lab.now)

def test_expiry_million_heartbeats_and_original_pcm_clock():
    lab,p,h=setup();report(lab,p,h);expiry=p._http_retry_evidence[2]
    # Full accepted reports, not merely a boolean/helper loop.
    for _ in range(1000000):report(lab,p,h)
    assert expiry==1120 and p._http_retry_evidence[2]==expiry
    for now,expected in ((1119.999,True),(1120,False),(1120.001,False)):
        lab.now=now;report(lab,p,h);assert exempt(p,now)==expected
    assert p.pcm_at==1000

def test_supervisor_transitions_and_72_hours():
    lab,p,h=setup();p.pcm_at=0
    for i in range(8641):
        lab.now=1000+i*30;h.begin_attempt();h.http_retry=(h.attempt,lab.now,lab.now+30)
        for phase in ('backoff','attempt','service'):
            h.checkpoint(phase,lab.now+30 if phase=='backoff' else 0,True);report(lab,p,h)
            p.state='ERROR' if phase=='service' else 'CONNECTING';lab.r.service()
            assert lab.current is p and not lab.r.attempts
    lab.now=h.http_retry[2]+90;report(lab,p,h);lab.r.service()
    assert lab.r.attempts[1]==1 and p.stopped

@pytest.mark.parametrize('change',[
 lambda s:s.update(version=2),lambda s:s.update(generation='old'),
 lambda s:s.update(http_retry=(True,1000,1030)),lambda s:s.update(http_retry=(1,float('nan'),1030)),
 lambda s:s.update(http_retry=(1,1001,1031)),lambda s:s.update(http_retry=(1,1000,1031)),
 lambda s:s.update(http_retry=(1,1000,1029)),lambda s:s.update(http_retry=(0,1000,1030)),
 lambda s:s.update(overflow=[1,0,0]),lambda s:s.update(progress=(1000,'finished',0)),
 lambda s:s.pop('http_retry'),
])
def test_invalid_report_cannot_authorize(change):
    lab,p,h=setup();report(lab,p,h);v=copy.deepcopy(h.snapshot());change(v)
    accept(p,v,lab.now);assert not exempt(p,lab.now)

def test_immutable_consumed_and_new_marker():
    lab,p,h=setup();report(lab,p,h)
    h.http_retry=(1,1000,1010);report(lab,p,h);assert not exempt(p,1000)
    h.http_retry=(1,1000,1030);report(lab,p,h);assert exempt(p,1000)
    p._has_pcm=True;p._http_pcm_produced_at=999;assert exempt(p,1000)
    p._http_pcm_produced_at=1001;assert not exempt(p,1000)
    report(lab,p,h);assert not exempt(p,1000)
    lab.now=1002;h.begin_attempt();h.http_retry=(2,1002,1004);report(lab,p,h)
    assert exempt(p,1002)
    p.direct_runtime='lab';p._last_pcm=1003;assert not exempt(p,1002)

@pytest.mark.parametrize('kind',['LOCAL','PODCAST','ALSA','UDP','RTP'])
def test_other_sources_unchanged(kind):
    lab,p,h=setup();p.source=SimpleNamespace(type=kind);report(lab,p,h);assert not exempt(p,1000)

@pytest.mark.parametrize('kind',['LOCAL','PODCAST','ALSA','UDP','RTP'])
def test_other_sources_skip_http_evidence_and_lock(kind):
    class NonHttp:
        source = SimpleNamespace(type=kind)

        @property
        def _http_retry_evidence(self):
            pytest.fail('Non-HTTP source must not inspect HTTP evidence')

        @property
        def _lock(self):
            pytest.fail('Non-HTTP source must not acquire the HTTP report lock')

    assert not exempt(NonHttp(), 1000)


@pytest.mark.parametrize('failure',['fatal','done','status','stop','pcm'])
def test_failure_priority(failure):
    lab,p,h=setup();report(lab,p,h)
    if failure=='fatal':p.fatal_error='fault';p._fatal_error='fault'
    elif failure=='done':p._done.set()
    elif failure=='stop':p._stop.set()
    elif failure=='pcm':p._pcm_error='transport'
    else:lab.now=1006
    assert not exempt(p,lab.now)

def test_retention_snapshot_and_invalidation():
    lab,p,h=setup();marker=h.http_retry;h.begin_attempt();h.checkpoint(force=True)
    assert h.snapshot()['http_retry']==marker
    h.invalidate_probe();assert h.snapshot()['http_retry'] is None


def test_pcm1_uses_production_time_not_receipt(adapter,monkeypatch):
    from fmheadendd.tests.test_audio_worker import pcm_message,PCM
    import time
    p=adapter;now=time.monotonic();h=NativeHealth(lambda:now);h.generation=p.generation;h.begin_attempt()
    h.http_retry=(1,now-.01,now+.99);accept(p,h.snapshot(),now)
    p._accept('pcm',pcm_message(p,produced_ns=int((now-.02)*1e9)),PCM)
    assert exempt(p,now)
    p._accept('pcm',pcm_message(p,sequence=2,produced_ns=time.monotonic_ns()),PCM)
    assert not exempt(p,time.monotonic())


def test_direct_writer_failure_cannot_be_masked(tmp_path,monkeypatch):
    import sys,time
    from fmheadendd.sources.process_pipeline import ProcessPipeline
    from fmheadendd.sources.worker_reporting import accept_report
    from fmheadendd.sources.audio_ipc import ProtocolError
    from fmheadendd.tests.test_audio_worker import source
    monkeypatch.setattr(sys,'platform','linux');p=ProcessPipeline(source(),direct_runtime=str(tmp_path))
    p.set_output_lease(1,1);now=time.monotonic();h=NativeHealth(lambda:now);h.generation=p.generation;h.begin_attempt();h.http_retry=(1,now,now+1)
    data=dict(blocks=0,bytes=0,produced_ns=0,writer_at=now,health=h.snapshot())
    accept_report(p,'heartbeat',data);assert exempt(p,time.monotonic())
    p._direct_writer_required_since=now-10
    with pytest.raises(ProtocolError):accept_report(p,'heartbeat',dict(data,writer_at=now-10))
    assert not exempt(p,now)


def test_stale_generation_and_stop_cannot_reactivate(adapter):
    from fmheadendd.sources.audio_ipc import envelope
    import time
    p=adapter;now=time.monotonic();h=NativeHealth(lambda:now);h.generation=p.generation;h.begin_attempt();h.http_retry=(1,now,now+1)
    p._accept('control',envelope(p.source_id,p.generation,1,'heartbeat',{'health':h.snapshot()}))
    assert exempt(p,time.monotonic())
    evidence=p._http_retry_evidence
    p._accept('control',envelope(p.source_id,'f'*32,2,'heartbeat',{'health':h.snapshot()}))
    assert p._http_retry_evidence==evidence and p.stale_messages==1
    p.stop()
    p._accept('control',envelope(p.source_id,p.generation,3,'heartbeat',{'health':h.snapshot()}))
    assert not exempt(p,now)


def test_report_freshness_separate_from_ipc_and_legacy_versions():
    lab,p,h=setup();report(lab,p,h);assert exempt(p,1000)
    assert not exempt(p,1005.001)
    for version in (1,2):
        value=h.snapshot();value.pop('http_retry');value['version']=version
        if version==1:value.pop('probe')
        assert accept(p,value,1000)
        assert not exempt(p,1000)


def test_snapshot_while_source_publishes_new_attempts():
    h=NativeHealth();h.generation='owner';start=threading.Event()
    def publish():
        start.wait()
        for _ in range(20000):
            h.begin_attempt();at=h.clock();h.http_retry=(h.attempt,at,at+1)
    thread=threading.Thread(target=publish);thread.start();start.set()
    try:
        for _ in range(20000):
            value=h.snapshot();marker=value['http_retry']
            assert marker is None or marker[0]<=value['attempt']
    finally:thread.join(5)
    assert not thread.is_alive()
