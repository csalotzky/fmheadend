"""Routing and conservative network-idle supervision without native sockets."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources import process_pipeline as backend
from fmheadendd.sources.worker_recovery import WorkerRecovery, RecoveryPolicy
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
def test_network_idle_is_bounded_but_status_timeout_is_not_exempt(kind,age,status_age,restart):
    old=Mock(source=source(kind),recovery_exhausted=False,expected_media_wait=False)
    old.recovery_observation.return_value=(100-status_age,100-age,'RUNNING',None,False)
    m=SimpleNamespace(pipelines={1:old},consumers={1:{0}},clock=lambda:100)
    recovery=WorkerRecovery(m)
    recovery.request=Mock()
    recovery.service()
    assert recovery.request.called is restart
    assert RecoveryPolicy().network_idle_timeout==90
