from typing import Protocol
from ..errors import DependencyUnavailable


class MuxClient(Protocol):
    def attach(self, modulator_id: int, source_id: int) -> None: ...
    def detach(self, modulator_id: int) -> None: ...
    def get_status(self) -> dict: ...
    def close(self) -> None: ...


class MockMuxClient:
    """Tracks producer assignments only; never processes or buffers PCM."""
    def __init__(self):
        self.available = True
        self.producers: dict[int, int] = {}

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
                "channels": [{"id": i, "underruns": 0, "overruns": 0} for i in range(8)], "last_error": None}

    def close(self):
        self.producers.clear()
