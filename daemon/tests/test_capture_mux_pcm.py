"""Standalone capture tool; Unix tests use isolated sockets, never the real mux."""
import importlib.util
import io
import os
from pathlib import Path
import socket
import wave

import pytest


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location(
        'capture_mux_pcm', Path(__file__).resolve().parents[2] / 'tools/capture_mux_pcm.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unix = pytest.mark.skipif(os.name != 'posix', reason='Unix sockets and muxd flock require POSIX')


def test_fragmented_frames_unchanged(tool, tmp_path):
    capture = tool.Capture(0, tmp_path / 'out.wav', tmp_path)
    raw = io.BytesIO()
    capture.wav = wave.open(raw, 'wb')
    capture.wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
    try:
        for block in (b'\x01', b'\x02\x03', b'\x04\x05\x06', b'\x07\x08\x09'):
            capture.capture(block)
            assert capture.captured_bytes % 4 == 0
            assert len(capture.partial) < 4
        capture.close()
        raw.seek(0)
        with wave.open(raw, 'rb') as result:
            assert result.readframes(99) == bytes(range(1, 9))
            assert result.getnframes() == 2
    finally:
        capture.close()


def test_seconds_validation(tool):
    assert tool.positive_seconds('20') == 20
    for invalid in ('0', '-1', 'nan', 'inf'):
        with pytest.raises(tool.argparse.ArgumentTypeError):
            tool.positive_seconds(invalid)


@unix
def test_all_channels_capture_reconnect_and_shutdown(tool, tmp_path):
    capture = tool.Capture(3, tmp_path / 'out.wav', tmp_path)
    peers = []
    try:
        capture.start()
        assert len(list(tmp_path.glob('pcm*.sock'))) == 8
        for tx in range(8):
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.connect(str(tmp_path / f'pcm{tx}.sock'))
            peers.append(peer)
        capture.poll(.2)
        for tx, peer in enumerate(peers):
            peer.sendall(b'abcdefghXYZ' if tx == 3 else b'NOT SELECTED')
        capture.poll(.2)
        assert capture.captured_bytes == 8 and capture.partial == b'XYZ'
        peers[3].close()
        capture.poll(.2)
        assert capture.partial == b''
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.connect(str(tmp_path / 'pcm3.sock'))
        peers.append(peer)
        capture.poll(.2)
        peer.sendall(b'ijkl')
        capture.poll(.2)
        capture.close()  # Remote peers are still open.
        capture.close()
        assert not list(tmp_path.glob('pcm*.sock'))
        assert capture.lock_fd is None and not capture.clients
        with wave.open(str(tmp_path / 'out.wav'), 'rb') as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48000, 2, 2)
            assert wav.readframes(100) == b'abcdefghijkl'
            assert wav.getnframes() == 3
        assert not (tmp_path / 'mux-control.sock').exists()
    finally:
        for peer in peers:
            peer.close()
        capture.close()


@unix
@pytest.mark.parametrize('existing', ['regular', 'symlink', 'live', 'stale'])
def test_socket_path_safety(tool, tmp_path, existing):
    path = tmp_path / 'pcm7.sock'
    server = None
    if existing == 'regular':
        path.write_bytes(b'keep')
    elif existing == 'symlink':
        path.symlink_to(tmp_path / 'missing')
    else:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        if existing == 'live':
            server.listen(1)
        else:
            server.close()
    identity = path.lstat().st_ino
    capture = tool.Capture(0, tmp_path / 'out.wav', tmp_path)
    try:
        if existing == 'stale':
            capture.start()
        else:
            with pytest.raises(RuntimeError):
                capture.start()
            assert path.lstat().st_ino == identity
            assert not (tmp_path / 'out.wav').exists()
    finally:
        capture.close()
        if server is not None:
            server.close()
    assert path.exists() == (existing in ('regular', 'live'))
    if existing == 'symlink':
        assert path.is_symlink()


@unix
def test_ctrl_c_finalizes_wav_and_removes_only_owned_paths(tool, tmp_path, monkeypatch):
    def interrupt(capture, timeout):
        capture.capture(b'1234')
        # A foreign replacement must not be removed by cleanup.
        (tmp_path / 'pcm7.sock').unlink()
        (tmp_path / 'pcm7.sock').write_bytes(b'foreign')
        raise KeyboardInterrupt
    monkeypatch.setattr(tool.Capture, 'poll', interrupt)
    assert tool.main(['--tx', '0', '--output', str(tmp_path / 'out.wav'),
                      '--runtime-dir', str(tmp_path)]) == 0
    assert (tmp_path / 'pcm7.sock').read_bytes() == b'foreign'
    assert len(list(tmp_path.glob('pcm*.sock'))) == 1
    with wave.open(str(tmp_path / 'out.wav'), 'rb') as wav:
        assert wav.readframes(1) == b'1234'


@unix
def test_mux_lock_and_elapsed_limit(tool, tmp_path):
    first = tool.Capture(0, tmp_path / 'first.wav', tmp_path)
    second = tool.Capture(1, tmp_path / 'second.wav', tmp_path)
    try:
        first.start()
        with pytest.raises(RuntimeError, match='owns the runtime'):
            second.start()
        second.close()
        assert len(list(tmp_path.glob('pcm*.sock'))) == 8
    finally:
        second.close()
        first.close()
    assert tool.main(['--tx', '1', '--output', str(tmp_path / 'limited.wav'),
                      '--runtime-dir', str(tmp_path), '--seconds', '.02']) == 0
    with wave.open(str(tmp_path / 'limited.wav'), 'rb') as wav:
        assert wav.getnframes() == 0  # No synthetic silence during absence.
    assert not list(tmp_path.glob('pcm*.sock'))
