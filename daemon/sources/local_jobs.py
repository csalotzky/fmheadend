"""Bounded, non-joining LOCAL jobs, including retired playback generations.

Filesystem calls cannot be cancelled. A retired job keeps its slot/key until it
returns, but cannot publish a result. Daemon threads have no executor atexit join.
Eight scan slots and eight independent CPU slots match the maximum active Sources;
there is no pending-job queue. A stuck scan cannot occupy the shuffle slots.
"""
from threading import Event, Lock, Thread


class Job:
    def __init__(self):
        self.done = Event()
        self.lock = Lock()
        self.cancelled = False
        self.value = self.error = None

    def cancel(self):
        with self.lock:
            self.cancelled = True
            self.value = self.error = None


class LocalJobs:
    def __init__(self, limit=8):
        self.limit = limit
        self.active = {}
        self.lock = Lock()

    def submit(self, owner, operation, wakeup):
        with self.lock:
            if owner in self.active or len(self.active) >= self.limit:
                return None
            job = Job()
            self.active[owner] = job

            def run():
                value = error = None
                try:
                    value = operation()
                except Exception as exc:
                    error = exc
                finally:
                    with self.lock:
                        del self.active[owner]
                    with job.lock:
                        if not job.cancelled:
                            job.value, job.error = value, error
                        job.done.set()
                    wakeup.set()

            try:
                Thread(target=run, name='local-catalog', daemon=True).start()
            except Exception:
                del self.active[owner]
                raise
            return job


scan_jobs = LocalJobs()
cycle_jobs = LocalJobs()
