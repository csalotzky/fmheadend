"""Production CLI contract and generation-safe Python service handoffs."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fmheadendd import app
from fmheadendd.sources.http_recovery import HttpUrlRecovery
from fmheadendd.sources.worker_services import RemoteRadio, radio_command
from fmheadendd.sources.direct_pcm import DirectHttpFactory


def test_real_controller_selection_without_experimental_flags(monkeypatch):
    import sys, uvicorn
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(sys, 'argv', ['fmheadendd','--audio-backend','audiod'])
    rp, create, run = Mock(), Mock(), Mock()
    monkeypatch.setattr(app,'Rp2354Client',rp)
    monkeypatch.setattr(app,'create_app',create)
    monkeypatch.setattr(uvicorn,'run',run)
    app.main()
    assert rp.called
    factory = create.call_args.kwargs['pipeline_factory']
    assert isinstance(factory, DirectHttpFactory)
    assert factory.local and factory.podcast and factory.alsa and factory.network
    assert create.call_args.kwargs['mux_factory'] is not None
    assert app.argument_parser().parse_args([]).audio_backend is None


def test_radio_roundtrip_pcm_commit_and_stale_observations():
    port=RemoteRadio(); worker=SimpleNamespace(radio_url=HttpUrlRecovery('http://old.test/a'))
    worker.radio_url.retry(3)
    def publish():
        eligible,trial=worker.radio_url.observe()
        port.update(dict(revision=getattr(worker,'_radio_revision',0),eligible=eligible,trial=trial))
    publish();token=object()
    assert port.offer(token,'http://new.test/a')
    port.update(dict(revision=0,eligible=False,trial=None))
    assert port.observe()[1]['state']=='queued'
    radio_command(worker,port.drain()[0])
    assert worker.radio_url.begin_attempt()=='http://new.test/a'
    worker.radio_url.pcm();publish()
    assert port.observe()[1]['state']=='pcm'
    assert port.observe()[1]['token'] is token
    port.accept(token);radio_command(worker,port.drain()[0])
    assert worker.radio_url.uri=='http://new.test/a'
    assert worker.radio_url.observe()[1] is None


def test_radio_rejection_is_terminal():
    port=RemoteRadio();port.update(dict(revision=0,eligible=True,trial=None))
    assert port.offer(object(),'http://new.test/a')
    port.update(dict(revision=1,eligible=False,trial=None))
    assert port.observe()[1]['state']=='failed'


def test_telemetry_revision_disable_and_native_staleness(monkeypatch):
    import sys,time
    from fmheadendd.sources.process_pipeline import ProcessPipeline
    from fmheadendd.sources.audio_ipc import envelope
    from fmheadendd.tests.test_audio_worker import source
    monkeypatch.setattr(sys,'platform','linux')
    p=ProcessPipeline(source(),telemetry_configured=True)
    p.request_telemetry(True,20)
    def deliver(sequence,revision,stamp):
        p._accept('control',envelope(p.source_id,p.generation,sequence,'telemetry',dict(revision=revision,
            sample=dict(telemetry_available=True,generation=1,sample_monotonic_ns=stamp))))
    deliver(1,0,time.monotonic_ns())
    assert not p.telemetry_snapshot()['telemetry_available']
    deliver(2,1,time.monotonic_ns())
    assert p.telemetry_snapshot()['telemetry_available']
    deliver(3,1,time.monotonic_ns()-1_000_000_000)
    assert not p.telemetry_snapshot()['telemetry_available']
    p.request_telemetry(False)
    deliver(4,1,time.monotonic_ns())
    assert not p.telemetry_snapshot()['telemetry_available']
