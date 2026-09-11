"""Mux client unit tests; no native daemon or hardware is needed."""

import pytest
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.mux.unix_client import UnixMuxClient, Producer
from fmheadendd.sources.output import PcmOutput


class Socket:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.data = bytearray()
        self.closed = False
    def send(self, data):
        result = next(self.steps)
        if isinstance(result, Exception):
            raise result
        self.data.extend(data[:result])
        return result
    def close(self):
        self.closed = True


def setup(steps):
    output = PcmOutput()
    output.attach(0)
    sock = Socket(steps)
    producer = Producer(1, output, sock)
    client = UnixMuxClient(lambda unused: output)
    return client, output, producer, sock


def test_partial_writes_preserve_raw_stereo_bytes():
    client, output, producer, sock = setup([1, 2, 5])
    pcm = b"\x34\x12\xcc\xed\xff\x7f\x00\x80"
    output.publish(pcm)
    for moment in (1, 1.001, 1.002):
        client._step(0, producer, moment)
    assert bytes(sock.data) == pcm
    assert producer.pending == b"" and producer.offset == 0


def test_stalled_partial_write_closes_session_instead_of_breaking_alignment():
    client, output, producer, sock = setup([1, BlockingIOError()])
    output.publish(b"12345678")
    client._step(0, producer, 1)
    client._step(0, producer, 1.01)
    assert len(producer.pending) == 8 and producer.offset == 1
    client._step(0, producer, 1.11)
    assert sock.closed and producer.sock is None
    assert producer.pending == b"" and producer.offset == 0
    assert producer.retry_at == pytest.approx(1.36)


def test_pcm_reconnect_does_not_replay_partial_audio():
    client, output, producer, sock = setup([1, BrokenPipeError()])
    output.publish(b"old!")
    client._step(0, producer, 1)
    client._step(0, producer, 1.01)
    new_sock = Socket([4])
    connects = []
    def reconnect(channel):
        connects.append(channel)
        return new_sock
    client._connect = reconnect
    client._step(0, producer, 1.1)
    assert not connects
    output.publish(b"new!")
    client._step(0, producer, 1.3)
    assert connects == [0]
    assert bytes(new_sock.data) == b"new!"


def test_no_audio_means_no_socket_write():
    client, output, producer, sock = setup([])
    client._step(0, producer, 1)
    assert sock.data == b""


def test_missing_digital_output_cannot_attach():
    client = UnixMuxClient(lambda unused: None)
    with pytest.raises(DependencyUnavailable, match="no digital PCM"):
        client.attach(0, 1)
    assert not client.producers and client._thread is None


@pytest.mark.parametrize("reply", [b'[]\n', b'null\n', b'{}\n', b'{invalid}\n', b'{"state":"RUNNING","channels":[]}\n'])
def test_invalid_control_response_is_dependency_error(monkeypatch, reply):
    from fmheadendd.mux import unix_client
    class ControlSocket:
        def __enter__(self): return self
        def __exit__(self, *unused): pass
        def settimeout(self, value): pass
        def connect(self, value): pass
        def sendall(self, value): pass
        def recv(self, count): return reply
    monkeypatch.setattr(unix_client.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(unix_client.socket, "socket", lambda *args: ControlSocket())
    client = UnixMuxClient(lambda unused: None)
    with pytest.raises(DependencyUnavailable):
        client.get_status()
