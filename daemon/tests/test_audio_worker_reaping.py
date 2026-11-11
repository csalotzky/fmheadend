"""Late reap ownership: deterministic handles plus real Linux waitpid evidence."""
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from fmheadendd.errors import DependencyUnavailable, SourcePending
from fmheadendd.sources import process_pipeline as backend
from fmheadendd.tests.test_audio_worker import (
    LINUX, PCM, adapter, simulated_process, source, until, finish,
)


class DelayedChild:
    """Independent exit and reap gates; no signal implies process exit."""
    pid = 12345

    def __init__(self, fault=None):
        self.exited = threading.Event()
        self.reaped = threading.Event()
        self.late = threading.Event()
        self.boundary = threading.Event()
        self.release_boundary = threading.Event()
        self.fault = fault
        self.calls = 0
        self.signals = []
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append('TERM')
        if self.fault == 'signal':
            raise OSError('injected TERM failure')

    def kill(self):
        self.signals.append('KILL')
        if self.fault == 'lookup':
            raise ProcessLookupError('exit raced with signal')
        if self.fault == 'kill-once':
            self.fault = None
            raise OSError('injected KILL failure')

    def wait(self, timeout):
        self.calls += 1
        if self.calls <= 3:
            if self.calls == 3 and self.fault == 'boundary':
                self.boundary.set()
                assert self.release_boundary.wait(3)
            raise subprocess.TimeoutExpired('delayed-child', timeout)
        self.late.set()
        if self.calls == 4 and self.fault == 'wait':
            raise RuntimeError('injected late wait failure')
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired('delayed-child', timeout)
        self.returncode = 17
        self.reaped.set()
        return self.returncode


def enter_cleanup(pipeline, child, monkeypatch):
    # Start the actual owner/finally path with a pre-owned fault-injection handle.
    pair = socket.socketpair
    monkeypatch.setattr(backend, 'socket', SimpleNamespace(
        socketpair=lambda *args: pair(), AF_UNIX=None,
        SOCK_STREAM=socket.SOCK_STREAM, timeout=socket.timeout))
    pipeline.process = child
    pipeline._stop.set()
    try:
        pipeline.start()
    except DependencyUnavailable:
        pass


@pytest.mark.parametrize('fault', [None, 'signal', 'lookup', 'kill-once', 'wait'])
def test_late_exit_keeps_owner_until_wait_succeeds(adapter, monkeypatch, fault):
    child = DelayedChild(fault)
    enter_cleanup(adapter, child, monkeypatch)
    try:
        assert child.late.wait(2)
        assert child.signals[:2] == ['TERM', 'KILL']
        assert adapter._thread.is_alive() and adapter._thread.daemon
        assert adapter in backend._owners
        assert not adapter._done.is_set() and not child.reaped.is_set()
        assert adapter.snapshot()['state'] == 'ERROR'
        with pytest.raises(DependencyUnavailable, match='pending'):
            adapter.stop()
        # Terminal start cannot launch another process or reset stop intent.
        owner = adapter._thread
        with pytest.raises(DependencyUnavailable):
            adapter.start()
        assert adapter._thread is owner
        child.exited.set()
        assert adapter._done.wait(3)
        owner.join(1)
        assert child.reaped.is_set() and adapter.exit_code == 17
        assert adapter not in backend._owners
        adapter.stop()
    finally:
        child.exited.set()
        adapter._thread.join(3)


def test_exit_at_final_deadline_cannot_fall_between_owners(adapter, monkeypatch):
    child = DelayedChild('boundary')
    enter_cleanup(adapter, child, monkeypatch)
    try:
        assert child.boundary.wait(2)
        owner = adapter._thread
        child.exited.set()  # Exit before the final timeout is delivered.
        child.release_boundary.set()
        assert adapter._done.wait(2)
        owner.join(1)
        assert child.reaped.is_set() and child.calls == 4
        assert adapter._thread is owner  # No handoff/thread creation window.
    finally:
        child.exited.set()
        child.release_boundary.set()
        adapter._thread.join(3)


def test_transport_cleanup_exception_does_not_discard_owner(adapter, monkeypatch):
    from unittest.mock import Mock
    child = DelayedChild()
    broken = Mock()
    broken.close.side_effect = OSError('injected close failure')
    monkeypatch.setattr(backend, 'configure', lambda sock: None)
    monkeypatch.setattr(backend, 'socket', SimpleNamespace(socketpair=lambda *args: (broken, broken),
        AF_UNIX=None, SOCK_STREAM=socket.SOCK_STREAM, timeout=socket.timeout))
    adapter.process = child
    adapter._stop.set()
    try:
        try:
            adapter.start()
        except DependencyUnavailable:
            pass
        assert child.late.wait(2)
        until(lambda: broken.close.call_count >= 4)
        assert adapter._thread.is_alive() and not adapter._done.is_set()
        child.exited.set()
        assert adapter._done.wait(2) and child.reaped.is_set()
    finally:
        child.exited.set()
        adapter._thread.join(3)


def test_late_owner_blocks_replacement_survives_manager_close_and_sibling(
        simulated_process, tmp_path, monkeypatch):
    from fmheadendd.database import Database
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.tests.profile_support import profile_store
    db = Database(tmp_path / 'late.db')
    first, second = (db.save('sources', source(n)) for n in ('late', 'sibling'))
    manager = SourceManager(db, pipeline_factory=backend.ProcessPipeline,
                            profile_files=profile_store(tmp_path))
    late = backend.ProcessPipeline(first)
    manager.consumers[first.id] = set()
    manager._publish_pipeline(first.id, late, 0)
    child = DelayedChild()
    sibling = None
    try:
        try:
            manager.acquire(second.id, 1)
        except SourcePending:
            pass
        sibling = manager.pipelines[second.id]
        assert until(lambda: sibling.output.read(1)) == PCM
        enter_cleanup(late, child, monkeypatch)
        assert child.late.wait(2)
        assert 'pending' in manager._restart_audio(first)
        assert manager.pipelines[first.id] is late
        assert len(simulated_process) == 1  # Only the sibling was launched.
        assert until(lambda: sibling.output.read(1)) == PCM
        started = time.monotonic()
        manager.close()
        assert time.monotonic() - started < 0.5
        assert not manager.pipelines
        assert late in backend._owners and late._thread.is_alive()
        assert not late._done.is_set()
        child.exited.set()
        assert late._done.wait(2) and child.reaped.is_set()
    finally:
        child.exited.set()
        if late._thread:
            late._thread.join(3)
        if sibling:
            finish(sibling)
        manager.close()
        db.close()


def test_interpreter_cleanup_has_one_budget_and_retains_pending_owners(adapter, monkeypatch):
    monkeypatch.setattr(backend, '_exiting', False)  # Restore hook state for pytest.
    child = DelayedChild()
    enter_cleanup(adapter, child, monkeypatch)
    try:
        assert child.late.wait(2)
        started = time.monotonic()
        backend._shutdown_owners(timeout=0.05)
        assert time.monotonic() - started < 0.5
        assert child.signals.count('KILL') >= 2
        assert adapter in backend._owners and not adapter._done.is_set()
        with pytest.raises(DependencyUnavailable, match='parent shutdown is terminal'):
            backend.ProcessPipeline(source()).start()
        child.exited.set()
        assert adapter._done.wait(2) and child.reaped.is_set()
    finally:
        child.exited.set()
        adapter._thread.join(3)


@LINUX
@pytest.mark.parametrize('exit_at_boundary', [False, True])
def test_real_child_late_exit_is_actually_reaped(adapter, monkeypatch, exit_at_boundary):
    # SIGKILL cannot reliably be delayed in a CI kernel. Delay signal delivery
    # in a transparent handle; the child and ALL successful waits are real.
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen([sys.executable, '-c',
        'import os,sys; os.read(int(sys.argv[1]),1)', str(read_fd)], pass_fds=(read_fd,))
    os.close(read_fd)
    child = DelayedChild('boundary' if exit_at_boundary else None)
    real_wait = process.wait
    waits = []

    def wait(timeout):
        child.calls += 1
        if child.calls <= 3:
            if child.calls == 3 and exit_at_boundary:
                child.boundary.set()
                assert child.release_boundary.wait(3)
            raise subprocess.TimeoutExpired(process.args, timeout)
        child.late.set()
        result = real_wait(timeout=timeout)
        waits.append(result)
        child.reaped.set()
        return result

    monkeypatch.setattr(process, 'wait', wait)
    monkeypatch.setattr(process, 'terminate', child.terminate)
    monkeypatch.setattr(process, 'kill', child.kill)
    enter_cleanup(adapter, process, monkeypatch)
    try:
        assert (child.boundary if exit_at_boundary else child.late).wait(2)
        # waitpid WNOHANG is 0: genuinely still a live child, not cached status.
        assert os.waitpid(process.pid, os.WNOHANG) == (0, 0)
        assert not adapter._done.is_set()
        with pytest.raises(DependencyUnavailable):
            adapter.stop()
        os.write(write_fd, b'x')
        child.release_boundary.set()
        assert adapter._done.wait(3)
        adapter._thread.join(1)
        assert waits == [0] and adapter.exit_code == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        child.release_boundary.set()
        os.close(write_fd)
        adapter._thread.join(3)
        if not adapter._done.is_set():
            os.kill(process.pid, 9)
            real_wait(timeout=3)


@LINUX
def test_real_term_kill_escalation_and_waitpid(adapter, monkeypatch):
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen([sys.executable, '-c',
        'import os,signal,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        'os.write(int(sys.argv[1]),b"r"); signal.pause()', str(write_fd)], pass_fds=(write_fd,))
    os.close(write_fd)
    try:
        import select
        assert select.select([read_fd], [], [], 3)[0]
        assert os.read(read_fd, 1) == b'r'  # TERM-ignore installed before cleanup.
        enter_cleanup(adapter, process, monkeypatch)
        assert adapter._done.wait(3)
        assert adapter.exit_code == -9
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        os.close(read_fd)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


@LINUX
@pytest.mark.parametrize('late', [False, True])
def test_real_parent_graceful_interpreter_exit_is_bounded(late):
    # Run the registered atexit hook, not just a direct unit-test call.
    script = '''
import threading
from fmheadendd.tests.test_audio_worker import FixturePipeline, source, until
from fmheadendd.errors import SourcePending
p = FixturePipeline(source('hang-start'))
try: p.start()
except SourcePending: pass
until(lambda: p.process is not None)
print(p.process.pid, flush=True)
assert p._thread.daemon
# No explicit stop/join. The interpreter hook owns graceful shutdown intent.
'''
    if late:
        # A permanently pending fault-injection handle cannot keep Python alive.
        # This branch tests interpreter bounds, not native child exit semantics.
        script = '''
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.tests.test_audio_worker import source
from fmheadendd.tests.test_audio_worker_reaping import DelayedChild
from fmheadendd.errors import DependencyUnavailable
p = ProcessPipeline(source())
p.process = DelayedChild()
p._stop.set()
try: p.start()
except DependencyUnavailable: pass
assert p.process.late.wait(2)
assert not p._done.is_set() and p._thread.is_alive() and p._thread.daemon
print('late-owner-pending', flush=True)
'''
    parent = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        out, err = parent.communicate(timeout=5)
        assert parent.returncode == 0, err
        if late:
            assert out.strip() == 'late-owner-pending'
        else:
            pid = int(out.strip())
            assert not os.path.exists(f'/proc/{pid}'), 'worker survived normal parent exit'
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
