"""Serialize control/SQLite work off the ASGI loop while media workers run independently."""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from .errors import DomainError

logger = logging.getLogger(__name__)


class DiscoveryExecutor:
    """One read-only job, no queue; timed-out jobs retain their admission slot."""
    def __init__(self, timeout=65.0):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="headend-discovery")
        self.timeout = timeout
        self.pending = None
        self.closing = False

    async def call(self, function):
        if self.closing or self.pending is not None:
            raise DomainError(503, 'DISCOVERY_BUSY', 'Discovery is busy or shutting down')
        future = asyncio.get_running_loop().run_in_executor(self.executor, function)
        self.pending = future
        future.add_done_callback(self._complete)
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.TimeoutError as exc:
            raise DomainError(503, 'DISCOVERY_TIMEOUT', 'Discovery did not finish within its timeout') from exc

    def _complete(self, future):
        self.pending = None
        # Also observe failures after an HTTP timeout or client cancellation.
        error = future.exception()
        if error is not None:
            logger.error('Discovery failed', exc_info=(type(error), error, error.__traceback__))

    async def close(self):
        self.closing = True
        try:
            if self.pending is not None:
                await asyncio.shield(self.pending)
        finally:
            self.executor.shutdown(wait=True)


class ControlExecutor:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="headend-control")
        self.pending = 0

    async def call(self, function):
        if self.pending >= 32:
            raise DomainError(503, "CONTROL_BUSY", "Too many pending control requests")
        self.pending += 1
        future = asyncio.get_running_loop().run_in_executor(self.executor, function)
        future.add_done_callback(lambda unused: self._complete())
        return await asyncio.shield(future)

    def _complete(self):
        self.pending -= 1

    def close(self):
        self.executor.shutdown(wait=True)


def control_route(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        request = kwargs["request"]
        def invoke():
            try:
                return function(*args, **kwargs)
            finally:
                # Failed actions can also change desired/actual state. Publish
                # on the same owner thread; reads never multiply sampling work.
                if request.method != 'GET':
                    request.app.state.publish_status()
        return await request.app.state.control.call(invoke)
    return wrapped
