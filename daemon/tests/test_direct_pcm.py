import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fmheadendd.sources.direct_pcm import DirectOutput, DirectMux, validate_assignment
from fmheadendd.sources.audio_ipc import ProtocolError, envelope
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.tests.test_audio_worker import source, until, PCM
from fmheadendd.tests.audiod_fixture import FakeDecoder
from fmheadendd.mux.unix_client import Producer


@pytest.mark.parametrize('value', [{}, {'revision': True, 'lease': 1},
    {'revision': 1, 'lease': -1}, {'revision': 0, 'lease': None},
    {'revision': 1, 'lease': 1, 'channel': 8}])
def test_invalid_assignment(value):
    with pytest.raises(ProtocolError):
        validate_assignment(value)


def test_progress_without_consumer_and_revoke(tmp_path, monkeypatch):
    out = DirectOutput(1, tmp_path, 300)
    out.pipeline = FakeDecoder(source(), output=out)
    def unavailable(_):
        raise OSError('mux unavailable')
    monkeypatch.setattr(out.writer, '_connect', unavailable)
    out.publish(PCM)
    assert out.progress()['bytes'] == len(PCM) and out.progress()['rendered'] == {}
    out.assign(dict(revision=1, lease=2))
    out.publish(PCM)
    out.assign(dict(revision=2, lease=None))
    assert not out.writer.producers and not out.output.snapshot()
    with pytest.raises(ProtocolError):
        out.assign(dict(revision=1, lease=2))
    out.writer.close()


def test_ledger_timeout_never_grants_until_actual_reap(tmp_path):
    def worker():
        return SimpleNamespace(_stop=__import__('threading').Event(),
            _done=__import__('threading').Event(), direct_applied=(0, None),
            direct_runtime=str(tmp_path), set_output_lease=Mock(), stop=Mock())
    a,b=worker(),worker()
    sources=SimpleNamespace(pipelines={1:a,2:b}, output_for=lambda _: None)
    now=[0.0]
    mux=DirectMux(sources,tmp_path,clock=lambda:now[0])
    mux.attach(0,1)
    with pytest.raises(Exception, match='pending'):
        mux.attach(0,2)
    now[0]=2; mux.service()
    a.stop.assert_called_once(); b.set_output_lease.assert_not_called()
    a.direct_applied=(1,None)  # stale completion
    mux.service(); b.set_output_lease.assert_not_called()
    a._done.set(); mux.service()
    b.set_output_lease.assert_called_once()
    mux.detach(0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux subprocess/pass_fds')
def test_real_child_direct_socket_revoke_and_reap(tmp_path):
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(str(tmp_path/'pcm0.sock')); listener.listen(); listener.settimeout(5)
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path))
    p.worker_module='fmheadendd.tests.audiod_fixture'
    try:
        try:p.start()
        except Exception as exc:
            from fmheadendd.errors import SourcePending
            assert isinstance(exc,SourcePending)
        until(lambda:p._has_pcm)
        assert not p.output.snapshot() and p._sequences['pcm']==0
        p.set_output_lease(1,1)
        conn,_=listener.accept(); conn.settimeout(5)
        data=b''
        while len(data)<len(PCM):data+=conn.recv(len(PCM)-len(data))
        assert data==PCM
        until(lambda:p.snapshot().get('stream_title')=='fixture')
        # mux endpoint disappears while valid PCM production continues.
        pid=p.process.pid; before=p.direct_progress['bytes']
        conn.close(); listener.close(); (tmp_path/'pcm0.sock').unlink()
        until(lambda:p.direct_progress['bytes']>before+len(PCM)*20)
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        listener.bind(str(tmp_path/'pcm0.sock')); listener.listen(); listener.settimeout(5)
        conn,_=listener.accept(); conn.settimeout(5)
        assert conn.recv(4)==PCM[:4] and p.process.pid==pid and p.fatal_error is None
        p.set_output_lease(2,None)
        until(lambda:p.direct_applied==(2,None))
        while conn.recv(65536):pass
        listener.settimeout(.4)
        with pytest.raises(socket.timeout):listener.accept()
        conn.close()
    finally:
        from fmheadendd.errors import DependencyUnavailable
        try:p.stop()
        except DependencyUnavailable:pass
        until(lambda:p._done.is_set())
        p.stop(); listener.close()
    assert p.process.wait(timeout=0)==p.exit_code


def test_stale_generation_and_no_pcm_parent(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'platform','linux')
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path))
    assert 'direct_runtime' in p._create
    p._accept('control',envelope(p.source_id,'f'*32,1,'output_applied',dict(revision=1,lease=1)))
    assert p.stale_messages==1 and p.direct_applied==(0,None)
    with pytest.raises(ProtocolError,match='forbidden'):
        p._accept('pcm',envelope(p.source_id,p.generation,1,'pcm',{}),PCM)


def test_metadata_waits_for_complete_frame_and_stale_partial_closes(tmp_path):
    out=DirectOutput(1,tmp_path,300)
    out.pipeline=FakeDecoder(source(),output=out)
    out.output.attach(0); out.lease=1
    class Partial:
        closed=False
        def send(self,data):return 1
        def close(self):self.closed=True
    sock=Partial(); producer=Producer(1,out.output,sock)
    out.publish(PCM)
    now=time.monotonic()
    for _ in range(3):out.writer._step(0,producer,now)
    assert out.progress()['rendered'] == {}
    out.writer._step(0,producer,now)
    assert out.progress()['rendered']['0']['status']['stream_title']=='fixture'
    out.writer._step(0,producer,now+.11)
    assert sock.closed and producer.pending==b'' and producer.stale_disconnects==1


@pytest.mark.parametrize('direct',[False,True])
def test_identity_update_preserves_output_lease_and_progress(monkeypatch,tmp_path,direct):
    monkeypatch.setattr(sys,'platform','linux')
    p=ProcessPipeline(source(),direct_runtime=str(tmp_path) if direct else None)
    p._direct_intent=(3,7); p.direct_applied=(3,7)
    progress={'bytes':400}; p.direct_progress=progress
    output=p.output; generation=p.generation
    updated=source('renamed')
    p.update_source_identity(updated)
    assert p.source is updated and p.output is output and p.generation==generation
    assert p._direct_intent==(3,7) and p.direct_applied==(3,7)
    assert p.direct_progress is progress
