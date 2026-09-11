"""Serialize control/SQLite work off the ASGI loop while media workers run independently."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from .errors import DomainError


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
        return await request.app.state.control.call(partial(function, *args, **kwargs))
    return wrapped
