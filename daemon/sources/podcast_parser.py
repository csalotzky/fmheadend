"""One lazy, reusable spawned parser, shared by podcast runtimes and probes."""
import atexit
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import multiprocessing
from threading import Condition, Lock

from ..errors import DomainError
from .podcast import parse_feed


def _parse(data, url, now, base_url):
    # DomainError is not pickle-reconstructible. Never transport arbitrary
    # exception messages/tracebacks, which may contain feed credentials.
    try:
        return parse_feed(data, url, now, base_url=base_url), None
    except DomainError:
        return None, "PODCAST_FEED_INVALID"
    except Exception:
        return None, "PODCAST_PARSE_FAILED"


class PodcastParser:
    def __init__(self, timeout=15, queue_timeout=120):
        self.timeout = timeout
        self.queue_timeout = queue_timeout
        self._lock = Condition()
        self._busy = False
        self._executor = None
        self._closed = False

    def __call__(self, data, url, now, *, base_url=None):
        # Submit only one task: Future.result's timeout otherwise includes the
        # executor queue, allowing one slow feed to kill unrelated requests.
        # Admission has its own bounded budget; expiry never kills the worker.
        with self._lock:
            if not self._lock.wait_for(lambda: self._closed or not self._busy,
                                       timeout=self.queue_timeout):
                raise DomainError(503, "PODCAST_PARSE_BUSY", "Podcast parser is busy")
            if self._closed:
                raise DomainError(503, "PODCAST_PARSE_CLOSED", "Podcast parser is closed")
            self._busy = True
        try:
            return self._run(data, url, now, base_url)
        finally:
            with self._lock:
                self._busy = False
                self._lock.notify_all()

    def _run(self, data, url, now, base_url):
        executor = None
        try:
            with self._lock:
                if self._closed:
                    raise DomainError(503, "PODCAST_PARSE_CLOSED", "Podcast parser is closed")
                if self._executor is None:
                    self._executor = ProcessPoolExecutor(
                        max_workers=1, mp_context=multiprocessing.get_context("spawn"))
                executor = self._executor
                future = executor.submit(_parse, data, url, now, base_url)
            feed, error = future.result(timeout=self.timeout)
        except DomainError:
            raise
        except Exception as exc:
            self._discard(executor)
            code = "PODCAST_PARSE_TIMEOUT" if isinstance(exc, TimeoutError) else "PODCAST_PARSE_FAILED"
            raise DomainError(503, code, "Podcast parser unavailable") from None
        with self._lock:
            # close() may race a completed future before its caller resumes.
            # Never return a result from an invalidated executor generation.
            if self._closed or self._executor is not executor:
                raise DomainError(503, "PODCAST_PARSE_CLOSED", "Podcast parser is closed")
            if error:
                raise DomainError(503, error, "Podcast parsing failed")
            return feed

    @staticmethod
    def _stop(executor):
        # Python 3.11-3.13 have no public terminate_workers API. Keep this
        # compatibility access isolated; shutdown(wait=True) can hang forever
        # on a CPU-bound parser. The public Process join/terminate/kill methods
        # bound teardown and reap the child. Discard the entire executor (and
        # its IPC queues) after termination; never reuse possibly damaged IPC.
        # Even newer terminate_workers() does not provide a bounded reap/kill
        # escalation, so process handles remain necessary for this guarantee.
        processes = list((executor._processes or {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            # Let an idle worker consume the shutdown sentinel normally.
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)

    def _discard(self, executor):
        with self._lock:
            if executor is not None and self._executor is executor:
                self._executor = None
                self._stop(executor)

    def close(self):
        with self._lock:
            self._closed = True
            self._lock.notify_all()
            if self._executor is not None:
                executor, self._executor = self._executor, None
                self._stop(executor)


_shared_lock = Lock()
_shared = None


def shared_parser():
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PodcastParser()
        return _shared


def close_shared_parser():
    global _shared
    with _shared_lock:
        parser, _shared = _shared, None
    if parser is not None:
        parser.close()


atexit.register(close_shared_parser)
