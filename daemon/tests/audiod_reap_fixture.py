"""Synthetic subprocess handle usable by a fresh interpreter without pytest."""
import subprocess
import threading


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
