import sys,time
from types import SimpleNamespace
import pytest
from fmheadendd.sources.direct_pcm import DirectOutput,DirectHttpFactory
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.tests.audiod_fixture import FakeDecoder,PCM
from fmheadendd.errors import DependencyUnavailable


def test_local_opt_in_and_pcm1_default(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    s=InputSourceConfig(name='local',type='LOCAL',config={'mode':'folder','path':str(tmp_path),'order':'sequential'})
    with pytest.raises(DependencyUnavailable):DirectHttpFactory(tmp_path)(s)
    p=DirectHttpFactory(tmp_path,local=True)(s)
    assert p.direct_runtime==str(tmp_path) and p._catalog is not None
    assert ProcessPipeline(s).direct_runtime is None


def test_captured_sink_metadata_beats_later_pipeline_revision(tmp_path):
    s=InputSourceConfig(name='first',type='HTTP',config={'uri':'http://127.0.0.1/a'})
    out=DirectOutput(1,tmp_path,300);p=FakeDecoder(s,output=out)
    p.transport_revision=1;p.transport_snapshot=p.snapshot;out.pipeline=p
    old=out.capture(PCM)
    p.source=p.source.model_copy(update={'name':'prefetched'});p.transport_revision=2
    out.output.attach(0);out.lease=1
    out.publish(old);data=out.output.read(0)
    assert data.status['stream_title']=='first' and old.produced_ns<=out.produced_ns
    out.writer.frame_sent(0,data)
    assert out.changes()['metadata']['status']['stream_title']=='first'
    # Same captured identity stays canonical and does not republish each block.
    out.publish(old);out.writer.frame_sent(0,out.output.read(0));assert out.changes() is None
    new=out.capture(PCM);out.publish(new);out.writer.frame_sent(0,out.output.read(0))
    assert out.changes()['metadata']['status']['stream_title']=='prefetched'


@pytest.mark.parametrize('enabled',[False,True])
def test_private_local_selection_keeps_defaults(monkeypatch,enabled):
    from unittest.mock import Mock
    from fmheadendd import app
    import uvicorn
    monkeypatch.setattr(sys,'platform','linux')
    monkeypatch.delenv('FMHEADEND_DIRECT_HTTP_PROTOTYPE',raising=False)
    monkeypatch.setenv('FMHEADEND_DIRECT_LOCAL_PROTOTYPE','1' if enabled else '0')
    monkeypatch.setattr(sys,'argv',['fmheadendd','--experimental-audio-worker','--mock-controller'])
    create=Mock();monkeypatch.setattr(app,'create_app',create);monkeypatch.setattr(uvicorn,'run',Mock())
    app.main();factory=create.call_args.kwargs['pipeline_factory']
    if enabled:assert isinstance(factory,DirectHttpFactory) and factory.local
    else:
        from fmheadendd.sources.process_pipeline import ExperimentalAudioFactory
        assert isinstance(factory,ExperimentalAudioFactory)
