"""Demand-driven Controller diagnostics, with one refresh admitted at a time."""
import asyncio
import time

from ..errors import DependencyUnavailable, DomainError


class SpiDiagnostics:
    CACHE_SECONDS = 5.0
    RETRY_SECONDS = 1.0

    def __init__(self, rp, clock=time.monotonic):
        self.rp, self.clock = rp, clock
        self._observed_at = None
        self._values = None
        self._pending = None
        self._error = None
        self._retry_at = 0.0

    async def read(self, control):
        # ASGI-loop admission happens before submission to the control queue.
        if self._pending is not None:
            await asyncio.shield(self._pending)
        elif self._error is not None and self.clock() < self._retry_at:
            raise DomainError(*self._error)
        elif self._observed_at is None or self.clock() - self._observed_at >= self.CACHE_SECONDS:
            self._pending = asyncio.create_task(self._refresh(control))
            self._pending.add_done_callback(self._complete)
            await asyncio.shield(self._pending)
        return {**self._values, 'observation_age_s': max(0.0, self.clock() - self._observed_at)}

    async def _refresh(self, control):
        try:
            values = await control.call(self.rp.get_spi_diagnostics)
        except (DependencyUnavailable, OSError, DomainError) as exc:
            self._error = ((exc.status, exc.error, exc.message) if isinstance(exc, DomainError)
                           else (503, 'DEPENDENCY_UNAVAILABLE', str(exc)))
            self._retry_at = self.clock() + self.RETRY_SECONDS
            raise DomainError(*self._error) from exc
        self._values = values
        self._observed_at = self.clock()
        self._error = None

    def _complete(self, future):
        self._pending = None
        # Observe errors even if every HTTP caller disconnected during refresh.
        if not future.cancelled():
            future.exception()
