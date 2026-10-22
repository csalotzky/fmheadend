#!/usr/bin/env python3
"""Capture one TX's unchanged final PCM while draining all eight mux sockets.

Stop the real muxd first. --seconds limits elapsed time, including periods with
no producer. WAV duration counts received frames only; gaps are not zero-filled.
No mux control, SPI or RF functionality is provided. Linux/Unix only.
"""
import argparse
import errno
import math
import os
from pathlib import Path
import selectors
import socket
import stat
import sys
import time
import wave

RATE = 48000
FRAME_BYTES = 4


def stale_socket(path):
    """Return a stale socket's identity; refuse uncertain or live endpoints."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f'Refusing non-socket path: {path}')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(.2)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise RuntimeError(f'Cannot establish that socket is stale: {path}: {exc}') from exc
        else:
            raise RuntimeError(f'Live listener at {path}; stop muxd first')
    return info.st_dev, info.st_ino


def unlink_owned(path, identity):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
        path.unlink()


class Capture:
    def __init__(self, tx, output, runtime_dir):
        self.tx, self.output, self.runtime_dir = tx, Path(output), Path(runtime_dir)
        self.selector = selectors.DefaultSelector()
        self.listeners = []
        self.clients = {}
        self.owned = {}
        self.lock_fd = None
        self.wav = self.file = None
        self.partial = b''
        self.captured_bytes = 0

    def start(self):
        import fcntl
        # Same lock as muxd. Keep its inode after close, as muxd does, to avoid
        # lock-file replacement races. No pipeline/control operations are involved.
        self.lock_fd = os.open(self.runtime_dir / '.muxd.lock',
                               os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another mux listener owns the runtime directory; stop muxd first') from exc
        paths = [self.runtime_dir / f'pcm{i}.sock' for i in range(8)]
        stale = [(path, stale_socket(path)) for path in paths]
        # Check every path before removing any stale sockets or creating output.
        self.file = self.output.open('xb')
        self.wav = wave.open(self.file, 'wb')
        self.wav.setparams((2, 2, RATE, 0, 'NONE', 'not compressed'))
        for tx, (path, identity) in enumerate(stale):
            if identity is not None:
                unlink_owned(path, identity)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listeners.append(listener)
            listener.bind(str(path))
            info = path.lstat()
            self.owned[path] = info.st_dev, info.st_ino
            os.chmod(path, 0o660)
            listener.listen(8)
            listener.setblocking(False)
            self.selector.register(listener, selectors.EVENT_READ, (True, tx))

    def accept(self, listener, tx):
        peer, _ = listener.accept()
        if tx in self.clients:
            peer.close()  # Match muxd: one producer per channel.
            return
        peer.setblocking(False)
        self.clients[tx] = peer
        self.selector.register(peer, selectors.EVENT_READ, (False, tx))
        print(f'TX{tx} connected', flush=True)

    def capture(self, block):
        data = self.partial + block
        size = len(data) - len(data) % FRAME_BYTES
        # Standard RIFF WAV uses a 32-bit size, not RF64. Finalize cleanly at limit.
        if self.captured_bytes + size > (0xFFFFFFFF - 36) // FRAME_BYTES * FRAME_BYTES:
            raise RuntimeError('Standard WAV size limit reached; start a new capture')
        if size:
            self.wav.writeframesraw(data[:size])
            self.captured_bytes += size
        self.partial = data[size:]

    def disconnect(self, tx):
        peer = self.clients.pop(tx)
        self.selector.unregister(peer)
        peer.close()
        if tx == self.tx:
            if self.partial:
                print(f'TX{tx}: discarded {len(self.partial)} incomplete frame bytes', flush=True)
            self.partial = b''  # Never join partial frames across producer sessions.
        print(f'TX{tx} disconnected', flush=True)

    def poll(self, timeout):
        events = self.selector.select(timeout)
        # Process existing producer EOF before accepting a replacement.
        for key, _ in sorted(events, key=lambda event: event[0].data[0]):
            listener, tx = key.data
            if listener:
                self.accept(key.fileobj, tx)
                continue
            try:
                block = key.fileobj.recv(65536)
            except BlockingIOError:
                continue
            except ConnectionError:
                block = b''
            if not block:
                self.disconnect(tx)
            elif tx == self.tx:
                self.capture(block)
            # All other channels are drained without buffering or disk writes.

    def progress(self):
        return f'{self.captured_bytes} bytes / {self.captured_bytes / (RATE * FRAME_BYTES):.3f} s'

    def close(self):
        for tx in list(self.clients):
            self.disconnect(tx)
        for listener in self.listeners:
            listener.close()
        self.listeners.clear()
        self.selector.close()
        try:
            if self.wav is not None:
                self.wav.close()
                self.wav = None
        finally:
            if self.file is not None:
                self.file.close()
                self.file = None
            self.partial = b''
            try:
                for path, identity in self.owned.items():
                    unlink_owned(path, identity)
                self.owned.clear()
            finally:
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                    self.lock_fd = None


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('--seconds must be positive and finite')
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tx', type=int, choices=range(8), required=True)
    parser.add_argument('--output', type=Path, required=True, help='New WAV file (existing files are refused)')
    parser.add_argument('--runtime-dir', type=Path, default=Path('/run/fmheadend'))
    parser.add_argument('--seconds', type=positive_seconds, help='Elapsed capture time; default: until Ctrl-C')
    args = parser.parse_args(argv)
    capture = Capture(args.tx, args.output, args.runtime_dir)
    try:
        capture.start()
        print(f'Capturing TX{args.tx} to {args.output.absolute()} (48000 Hz stereo S16LE)', flush=True)
        started = time.monotonic()
        deadline = started + args.seconds if args.seconds is not None else math.inf
        progress_at = started + 1
        while time.monotonic() < deadline:
            capture.poll(min(.2, max(0, deadline - time.monotonic())))
            if time.monotonic() >= progress_at:
                print(f'Captured {capture.progress()}', flush=True)
                progress_at = time.monotonic() + 1
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, wave.Error) as exc:
        print(f'Capture failed: {exc}', file=sys.stderr)
        return 1
    finally:
        opened = capture.file is not None
        capture.close()
        if opened:
            print(f'Final WAV capture: {capture.progress()}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
