import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fmheadendd.sources.direct_pcm import DirectOutput, DirectMux
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.audio_ipc import envelope
from fmheadendd.tests.audiod_fixture import FakeDecoder, PCM
from fmheadendd.tests.test_audio_worker import source


def test_eight_queues_share_bytes_and_revoke_is_channel_local(tmp_path,monkeypatch):
    out=DirectOutput(1,tmp_path,300); out.pipeline=FakeDecoder(source(),output=out)
    def unavailable(ch):raise OSError('missing mux')
    monkeypatch.setattr(out.writer,'_connect',unavailable)
    try:
        for ch in range(8):out.assign(dict(channel=ch,revision=1,lease=ch+1))
        out.publish(PCM)
        blocks=[out.output.read(ch) for ch in range(8)]
        assert all(b is blocks[0] for b in blocks) and blocks[0]==PCM
        out.assign(dict(channel=3,revision=2,lease=None))
        assert set(out.writer.producers)==set(range(8))-{3}
        assert out.leases==[1,2,3,None,5,6,7,8]
        out.publish(bytes(len(PCM)))  # digital silence is progress
        assert out.progress()['bytes']==2*len(PCM)
        assert all(out.output.read(ch)==bytes(len(PCM)) for ch in range(8) if ch!=3)
        out.assign(dict(channel=3,revision=3,lease=9))
        out.revoke(); assert not out.writer.producers and not out.output.snapshot()
    finally:out.writer.close()


def test_independent_pending_revoke_and_exit(tmp_path):
    def worker():return SimpleNamespace(_stop=threading.Event(),_done=threading.Event(),
        direct_runtime=str(tmp_path),direct_applied=(0,None),direct_applied_channels={},
        set_output_lease=Mock(),stop=Mock())
    a,b=worker(),worker(); now=[0]
    mux=DirectMux(SimpleNamespace(pipelines={1:a,2:b},output_for=lambda _:None),tmp_path,lambda:now[0])
    for ch in range(8):mux.attach(ch,1)
    for ch in (2,5):
        with pytest.raises(Exception,match='pending'):mux.attach(ch,2)
    a.direct_applied_channels[5]=(2,None)
    mux.service()
    assert mux.channels[5].owner is b and mux.channels[2].owner is a
    assert all(mux.channels[ch].owner is a for ch in (0,1,3,4,6,7))
    now[0]=2; mux.service()
    assert mux.channels[2].owner is a  # no release on timeout
    a._done.set(); a._stop.set(); mux.service()
    assert mux.channels[2].owner is b


def test_completions_fenced_by_channel_and_generation(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path))
    p.set_output_lease(1,4,2); p.set_output_lease(2,None,5)
    def accept(seq,ch,rev,lease,generation=None):
        p._accept('control',envelope(p.source_id,generation or p.generation,seq,
            'output_applied',dict(channel=ch,revision=rev,lease=lease)))
    accept(1,2,2,None)  # another channel's completion
    assert not p.direct_applied_channels
    accept(2,5,2,None,'f'*32)
    assert not p.direct_applied_channels
    accept(3,5,2,None)
    assert p.direct_applied_channels=={5:(2,None)}
    p.set_output_lease(3,8,5); accept(4,5,2,None)
    assert p.direct_applied_channels=={5:(2,None)}


def test_slow_channel_cannot_roll_metadata_back(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path))
    out=DirectOutput(1,tmp_path,300); out.pipeline=FakeDecoder(source('old'),output=out)
    for ch in (0,1):
        out.output.attach(ch); out.leases[ch]=ch+1; p.set_output_lease(1,ch+1,ch)
    out.publish(PCM); older=[out.output.read(ch) for ch in (0,1)]
    out.pipeline.source=source('new'); out.publish(PCM)
    newer=out.output.read(0)
    out.writer.frame_sent(0,newer); out.writer.frame_sent(1,older[1])
    progress=out.progress()
    p._accept('control',envelope(p.source_id,p.generation,1,'direct_progress',progress))
    assert p.snapshot()['stream_title']=='new'


def test_desired_returns_to_owner_while_revoke_pending(tmp_path):
    a=SimpleNamespace(_stop=threading.Event(),_done=threading.Event(),
        direct_runtime=str(tmp_path),direct_applied=(0,None),direct_applied_channels={},
        set_output_lease=Mock(),stop=Mock())
    mux=DirectMux(SimpleNamespace(pipelines={1:a},output_for=lambda _:None),tmp_path)
    mux.attach(4,1); mux.detach(4)
    with pytest.raises(Exception,match='pending'):mux.attach(4,1)
    assert mux.channels[4].revoking is not None
    a.direct_applied_channels[4]=(2,None); mux.service()
    assert mux.channels[4].revoking is None and mux.channels[4].owner is a
    a.set_output_lease.assert_called_with(3,3,4)
