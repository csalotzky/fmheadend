import sys,time
from unittest.mock import Mock
import pytest
from fmheadendd.sources.worker_reporting import WorkerReporter,accept_report
from fmheadendd.sources.direct_pcm import DirectOutput
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.audio_ipc import ProtocolError
from fmheadendd.tests.audiod_fixture import FakeDecoder,PCM
from fmheadendd.tests.test_audio_worker import source


def test_unchanged_revision_never_rebuilds_status():
    p=FakeDecoder(source(),output=None); p.status_revision=0
    p.snapshot=Mock(wraps=p.snapshot); p.pcm_snapshot=Mock(wraps=p.pcm_snapshot)
    now=[0.0]; events=[]
    reporter=WorkerReporter(p,None,None,lambda k,v:events.append((k,v)),lambda:now[0])
    for i in range(51):now[0]=i*.2; reporter.poll()
    assert p.snapshot.call_count==p.pcm_snapshot.call_count==1
    assert len([e for e in events if e[0]=='status'])==1
    assert len([e for e in events if e[0]=='heartbeat'])==11
    p.status_revision+=1; reporter.poll()
    assert p.snapshot.call_count==2


def test_eight_render_bindings_serialize_source_metadata_once(tmp_path):
    out=DirectOutput(1,tmp_path,300); out.pipeline=FakeDecoder(source(),output=out)
    for ch in range(8):out.output.attach(ch); out.leases[ch]=ch+1
    out.publish(PCM)
    changes=[]
    for ch in range(8):
        out.writer.frame_sent(ch,out.output.read(ch)); changes.append(out.changes())
    assert sum(c['metadata'] is not None for c in changes)==1
    assert out.changes() is None
    assert out.changes(force=True)['metadata'] is not None
    assert set(out.heartbeat())=={'blocks','bytes','produced_ns','writer_at'}


def test_heartbeat_not_fake_pcm_and_writer_stall(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path)); p.set_output_lease(1,1)
    data=dict(blocks=1,bytes=4,produced_ns=time.monotonic_ns(),writer_at=time.monotonic())
    accept_report(p,'heartbeat',data); first=p._last_pcm
    accept_report(p,'heartbeat',data)
    assert p._last_pcm==first and p._last_status>=first
    p._direct_writer_required_since=time.monotonic()-6
    with pytest.raises(ProtocolError,match='writer stalled'):
        accept_report(p,'heartbeat',dict(data,writer_at=time.monotonic()-6))
