from typing import Protocol
from ..errors import DependencyUnavailable


class MuxClient(Protocol):
    def attach(self, modulator_id: int, source_id: int) -> None: ...
    def detach(self, modulator_id: int) -> None: ...
    def get_status(self) -> dict: ...
    def set_tx_count(self, count: int) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def close(self) -> None: ...


class MockMuxClient:
    """Tracks producer assignments only; never processes or buffers PCM."""
    def __init__(self):
        self.available = True
        self.producers: dict[int, int] = {}
        self.tx_count = None
        self.stream_state = 'WAITING_FOR_CONFIG'

    def set_tx_count(self, count):
        self.get_status()
        if type(count) is not int or not 1 <= count <= 8:
            raise ValueError('TX count must be an integer 1..8')
        if self.stream_state == 'STREAMING' and self.tx_count != count:
            raise DependencyUnavailable('Pause before changing TX count')
        self.tx_count = count
        if self.stream_state == 'WAITING_FOR_CONFIG':
            self.stream_state = 'STREAMING'

    def pause(self):
        self.get_status()
        if self.tx_count is not None:
            self.stream_state = 'PAUSED'

    def resume(self):
        self.get_status()
        if self.tx_count is None:
            raise DependencyUnavailable('Mux is unconfigured')
        self.stream_state = 'STREAMING'

    def attach(self, modulator_id, source_id):
        if not self.available:
            raise DependencyUnavailable("Mock mux is unavailable")
        self.producers[modulator_id] = source_id

    def detach(self, modulator_id):
        self.producers.pop(modulator_id, None)

    def get_status(self):
        if not self.available:
            raise DependencyUnavailable("Mock mux is unavailable")
        return {"state": "RUNNING", "spi_ok": True, "spi_errors": 0, "frames_sent": 0,
                "tx_count": self.tx_count, "stream_state": self.stream_state,
                "spi_frame_bytes": None if self.tx_count is None else 4 + 512 * self.tx_count,
                "channels": [{"id": i, "underruns": 0, "overruns": 0} for i in range(8)], "last_error": None}

    def close(self):
        self.producers.clear()


def validate_stream_status(status):
    try:
        state, count, size = status['stream_state'], status['tx_count'], status['spi_frame_bytes']
        if state == 'WAITING_FOR_CONFIG':
            valid = count is None and size is None
        else:
            valid = (state in ('PAUSED', 'STREAMING') and type(count) is int and 1 <= count <= 8
                     and type(size) is int and size == 4 + 512 * count)
        if not valid:
            raise ValueError('Inconsistent mux stream configuration')
    except (KeyError, TypeError, ValueError) as exc:
        raise DependencyUnavailable(f'Invalid mux stream status: {exc}') from exc
