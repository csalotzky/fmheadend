"""Routing and conservative network-idle supervision without native sockets."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources import process_pipeline as backend
from fmheadendd.sources.worker_recovery import WorkerRecovery, RecoveryPolicy
from fmheadendd.sources.audio_ipc import ProtocolError, envelope
from fmheadendd.tests.test_audio_worker import pcm_message, PCM


def source(kind):
    config = dict(host='127.0.0.1', port=45678)
    config.update(dict(format='S16LE', sample_rate=44100, channels=1) if kind=='UDP'
        else dict(codec='PCM', clock_rate=44100, channels=1, payload_type=96))
    return InputSourceConfig(name=kind, type=kind, config=config)


@pytest.mark.parametrize('kind', ['UDP','RTP'])
def test_routing_config_and_stale_pcm(monkeypatch, kind):
    monkeypatch.setattr(backend.sys,'platform','linux')
    s=source(kind)
    p=backend.ExperimentalAudioFactory()(s)
    assert isinstance(p,backend.ProcessPipeline) and p._create['source']['config']==s.config
    assert not isinstance(backend.ExperimentalHttpFactory()(s),backend.ProcessPipeline)
    q=p.fresh()
    assert q.output is p.output and q.generation!=p.generation
    q.output.attach(0)
    q._accept('pcm',pcm_message(p),PCM)
    assert q.stale_messages==1 and q.output.read(0) is None
    message=pcm_message(q)
    q._accept('pcm',message,bytes(len(PCM)))
    assert q.output.read(0)==bytes(len(PCM))


@pytest.mark.parametrize('kind,change', [('UDP',{'format':'MP3'}),('UDP',{'channels':0}),
    ('RTP',{'codec':'VORBIS'}),('RTP',{'payload_type':128})])
def test_unsupported_config_rejected(kind,change):
    from pydantic import ValidationError
    s=source(kind)
    with pytest.raises(ValidationError):
        InputSourceConfig(name=kind,type=kind,config=dict(s.config,**change))


@pytest.mark.parametrize('kind',['UDP','RTP'])
@pytest.mark.parametrize('age,status_age,restart',[(20,0,False),(89,0,False),(91,0,True),(1,6,True)])
def test_network_without_idle_evidence_uses_existing_deadlines(kind,age,status_age,restart):
    old=Mock(source=source(kind),recovery_exhausted=False,expected_media_wait=False)
    old.recovery_observation.return_value=(100-status_age,100-age,'RUNNING',None,False)
    m=SimpleNamespace(pipelines={1:old},consumers={1:{0}},clock=lambda:100)
    recovery=WorkerRecovery(m)
    recovery.request=Mock()
    recovery.service()
    assert recovery.request.called is restart
    assert RecoveryPolicy().network_idle_timeout==90


@pytest.mark.parametrize('kind', ['UDP', 'RTP'])
def test_network_idle_observations_keep_generation_alive_but_cannot_be_replayed(kind):
    now = 1000.
    old = SimpleNamespace(source=source(kind), expected_media_wait=False, _fail=Mock())
    manager = SimpleNamespace(pipelines={1:old}, consumers={1:{0}}, clock=lambda:now)
    recovery = WorkerRecovery(manager)
    recovery.request = Mock()
    for _ in range(72*3600//2):
        now += 2
        old.network_idle_at = now-1
        old.recovery_observation = lambda: (now, 1000., 'CONNECTING', None, False)
        recovery.service()
    recovery.request.assert_not_called()
    # Fresh control traffic with a frozen native timestamp does not give immunity.
    now = old.network_idle_at+90
    recovery.service()
    recovery.request.assert_not_called()
    now += .001
    recovery.service()
    old._fail.assert_called_once_with('Worker PCM progress timeout')


@pytest.mark.parametrize('fatal,done,status_age,reason', [
    ('Direct writer stalled', False, 0, 'Direct writer stalled'),
    (None, True, 0, 'Worker exited'),
    (None, False, 6, 'Worker status timeout'),
])
def test_network_idle_evidence_does_not_mask_other_failures(fatal,done,status_age,reason):
    old = Mock(source=source('UDP'), expected_media_wait=False, network_idle_at=999.)
    old.recovery_observation.return_value = (1000-status_age, 1., 'CONNECTING', fatal, done)
    manager = SimpleNamespace(pipelines={1:old}, consumers={1:{0}}, clock=lambda:1000.)
    recovery = WorkerRecovery(manager)
    recovery.request = Mock()
    recovery.service()
    old._fail.assert_called_once_with(reason)


def test_network_idle_report_arriving_during_service_is_not_discarded():
    values = iter((1000., 1000.01))
    old = Mock(source=source('RTP'), expected_media_wait=False, network_idle_at=1000.005)
    old.recovery_observation.return_value = (1000., 1., 'CONNECTING', None, False)
    manager = SimpleNamespace(pipelines={1:old}, consumers={1:{0}}, clock=lambda:next(values))
    recovery = WorkerRecovery(manager)
    recovery.request = Mock()
    recovery.service()
    recovery.request.assert_not_called()


@pytest.mark.parametrize('kind', ['UDP', 'RTP', 'HTTP'])
def test_network_idle_report_is_bounded_and_generation_fenced(monkeypatch, kind):
    from fmheadendd.tests.test_audio_worker import source as http_source
    import time
    monkeypatch.setattr(backend.sys, 'platform', 'linux')
    old = backend.ProcessPipeline(http_source() if kind == 'HTTP' else source(kind))
    now = time.monotonic()
    def report(sequence, observed, generation=old.generation):
        old._accept('control', envelope(old.source_id,generation,sequence,'heartbeat',
                                       {'network_idle_at':observed}))
    report(1, now, 'f'*32)
    assert not old.network_idle_at
    for i, bad in enumerate((True, -1, float('nan'), float('inf'), now+60), 2):
        with pytest.raises(ProtocolError, match='network idle'):
            report(i,bad)
        assert old._initial_liveness_at is None
    if kind == 'HTTP':
        with pytest.raises(ProtocolError, match='network idle'):
            report(8,now)
        return
    pcm_at = old._last_pcm
    report(8,now)
    report(9,now-1)
    assert old.network_idle_at == now and old._last_pcm == pcm_at
    old.stop()
    report(10,time.monotonic())
    assert old.network_idle_at == now and not old.fresh().network_idle_at
