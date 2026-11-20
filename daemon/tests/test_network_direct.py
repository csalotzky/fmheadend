"""One UDP/RTP opt-in; existing receiver configuration and supervision remain."""
import sys,time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fmheadendd.errors import DependencyUnavailable
from fmheadendd.sources.direct_pcm import DirectHttpFactory
from fmheadendd.sources.process_pipeline import ProcessPipeline,ExperimentalAudioFactory
from fmheadendd.sources.audio_ipc import envelope,ProtocolError
from fmheadendd.sources.worker_recovery import WorkerRecovery,RecoveryPolicy
from fmheadendd.tests.test_network_worker import source


@pytest.mark.parametrize('kind',['UDP','RTP'])
def test_shared_direct_opt_in_and_exact_config(monkeypatch,tmp_path,kind):
    monkeypatch.setattr(sys,'platform','linux');s=source(kind)
    with pytest.raises(DependencyUnavailable):DirectHttpFactory(tmp_path,local=True,podcast=True,alsa=True)(s)
    p=DirectHttpFactory(tmp_path,network=True)(s)
    assert p.direct_runtime==str(tmp_path) and p._create['source']['config']==s.config
    assert p._catalog is None and p._podcast_service is None
    fresh=p.fresh();assert fresh.output is p.output and fresh.generation!=p.generation and fresh.direct_runtime==p.direct_runtime
    assert ProcessPipeline(s).direct_runtime is None
    p._accept('control',envelope(p.source_id,'f'*32,1,'heartbeat',{}))
    assert p.stale_messages==1
    with pytest.raises(ProtocolError,match='forbidden'):p._accept('pcm',envelope(p.source_id,p.generation,1,'pcm',{}),bytes(4))


@pytest.mark.parametrize('enabled',[False,True])
def test_private_network_selection_and_defaults(monkeypatch,enabled):
    import uvicorn
    from fmheadendd import app
    monkeypatch.setattr(sys,'platform','linux')
    for var in ('HTTP','LOCAL','PODCAST','ALSA'):monkeypatch.delenv(f'FMHEADEND_DIRECT_{var}_PROTOTYPE',raising=False)
    monkeypatch.setenv('FMHEADEND_DIRECT_NETWORK_PROTOTYPE','1' if enabled else '0')
    monkeypatch.setattr(sys,'argv',['fmheadendd','--experimental-audio-worker','--mock-controller'])
    create=Mock();monkeypatch.setattr(app,'create_app',create);monkeypatch.setattr(uvicorn,'run',Mock())
    app.main();factory=create.call_args.kwargs['pipeline_factory']
    if enabled:assert isinstance(factory,DirectHttpFactory) and factory.network and not factory.alsa
    else:assert isinstance(factory,ExperimentalAudioFactory)


@pytest.mark.parametrize('kind',['UDP','RTP'])
@pytest.mark.parametrize('age,status_age,restart',[(89,0,False),(91,0,True),(1,6,True)])
def test_direct_network_keeps_production_idle_and_status_deadlines(monkeypatch,tmp_path,kind,age,status_age,restart):
    monkeypatch.setattr(sys,'platform','linux')
    p=DirectHttpFactory(tmp_path,network=True)(source(kind))
    p._accept('control', envelope(p.source_id, p.generation, 1, 'heartbeat',
        {'blocks':0, 'bytes':0, 'produced_ns':0, 'writer_at':time.monotonic()}))
    now=time.monotonic()
    p._last_pcm=now-age;p._last_status=now-status_age;p._status['state']='RUNNING'
    m=SimpleNamespace(pipelines={p.source_id:p},consumers={p.source_id:{0}},clock=lambda:now)
    recovery=WorkerRecovery(m);recovery.request=Mock();recovery.service()
    assert recovery.request.called is restart
    assert RecoveryPolicy().network_idle_timeout==90 and RecoveryPolicy().status_timeout==5
