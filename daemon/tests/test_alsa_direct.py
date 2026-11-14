"""Private ALSA opt-in; exact device configuration and existing progress fences."""
import sys,time
from unittest.mock import Mock

import pytest

from fmheadendd.errors import DependencyUnavailable
from fmheadendd.sources.direct_pcm import DirectHttpFactory
from fmheadendd.sources.process_pipeline import ProcessPipeline,ExperimentalAudioFactory
from fmheadendd.sources.audio_ipc import envelope,ProtocolError
from fmheadendd.sources.worker_reporting import accept_report
from fmheadendd.tests.test_alsa_worker import source


def test_private_alsa_routing_and_device_transfer(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    s=source()
    for factory in (DirectHttpFactory(tmp_path),DirectHttpFactory(tmp_path,local=True,podcast=True)):
        with pytest.raises(DependencyUnavailable):factory(s)
    p=DirectHttpFactory(tmp_path,alsa=True)(s)
    assert p.direct_runtime==str(tmp_path) and p._create['source']['config']==s.config
    assert p._catalog is None and p._podcast_service is None
    fresh=p.fresh()
    assert fresh.output is p.output and fresh.direct_runtime==p.direct_runtime and fresh.generation!=p.generation
    assert ProcessPipeline(s).direct_runtime is None


@pytest.mark.parametrize('enabled',[False,True])
def test_private_alsa_selection_and_defaults(monkeypatch,enabled):
    from fmheadendd import app
    import uvicorn
    monkeypatch.setattr(sys,'platform','linux')
    for variable in ('FMHEADEND_DIRECT_HTTP_PROTOTYPE','FMHEADEND_DIRECT_LOCAL_PROTOTYPE','FMHEADEND_DIRECT_PODCAST_PROTOTYPE'):
        monkeypatch.delenv(variable,raising=False)
    monkeypatch.setenv('FMHEADEND_DIRECT_ALSA_PROTOTYPE','1' if enabled else '0')
    monkeypatch.setattr(sys,'argv',['fmheadendd','--experimental-audio-worker','--mock-controller'])
    create=Mock();monkeypatch.setattr(app,'create_app',create);monkeypatch.setattr(uvicorn,'run',Mock())
    app.main();factory=create.call_args.kwargs['pipeline_factory']
    if enabled:assert isinstance(factory,DirectHttpFactory) and factory.alsa and not factory.local and not factory.podcast
    else:assert isinstance(factory,ExperimentalAudioFactory)


def test_alsa_direct_progress_and_generation_fences(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    p=DirectHttpFactory(tmp_path,alsa=True)(source());p.set_output_lease(1,1)
    heartbeat=dict(blocks=10,bytes=19200,produced_ns=time.monotonic_ns(),writer_at=time.monotonic())
    accept_report(p,'heartbeat',heartbeat);at=p._last_pcm
    accept_report(p,'heartbeat',heartbeat);assert p._last_pcm==at
    p._accept('control',envelope(p.source_id,'f'*32,1,'heartbeat',dict(heartbeat,blocks=11,bytes=21120)))
    assert p.stale_messages==1 and p._last_pcm==at
    with pytest.raises(ProtocolError,match='forbidden'):
        p._accept('pcm',envelope(p.source_id,p.generation,1,'pcm',{}),bytes(4))
