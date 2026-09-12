from typing import Literal, Protocol
from collections import deque
import time
from pydantic import Field

from ..errors import DependencyUnavailable
from ..models.configuration import Model, Modulator


class TxConfig(Model):
    frequency_khz: int
    rf_output_dbuv: float | None = Field(default=None, ge=0, le=6553.5, multiple_of=0.1, strict=True)
    stereo: bool
    preemphasis_us: int
    audio_mode: Literal["DIGITAL", "ANALOG"]
    rds_enabled: bool

    @classmethod
    def from_modulator(cls, modulator: Modulator):
        return cls(**modulator.model_dump(include={"frequency_khz", "rf_output_dbuv", "stereo", "preemphasis_us", "rds_enabled", "audio_mode"}))


class Rp2354Client(Protocol):
    def set_tx_config(self, modulator_id: int, config: TxConfig) -> None: ...
    def set_tx_enabled(self, modulator_id: int, enabled: bool) -> None: ...
    def get_info(self) -> dict: ...
    def get_status(self) -> dict: ...
    def get_rds_fifo_free(self) -> list[int]: ...
    def push_rds(self, modulator_id: int, groups) -> None: ...
    def clear_rds(self, modulator_id: int) -> None: ...


class MockRp2354Client:
    def __init__(self, *, clock=time.monotonic, rds_capacity=64):
        self.clock, self.rds_capacity = clock, rds_capacity
        self.rds_fifos = [deque() for _ in range(8)]
        self.rds_history = deque(maxlen=256)
        self.rds_calls = deque(maxlen=256)
        self._rds_times = [clock()] * 8
        self.available = True
        self.enabled = [False] * 8
        self.configs: dict[int, TxConfig] = {}
        self.i2c_errors = 0

    def _check(self):
        if not self.available:
            self.i2c_errors += 1
            raise DependencyUnavailable("Mock RP2354 is unavailable")

    def set_tx_config(self, modulator_id, config):
        self._check()
        self.configs[modulator_id] = config.model_copy(deep=True)

    def set_tx_enabled(self, modulator_id, enabled):
        self._check()
        self.enabled[modulator_id] = enabled

    def get_info(self):
        self._check()
        return {"rp_protocol_version": 1, "rp_firmware_version": "mock", "hardware_revision": "mock", "modulator_count": 8}

    def get_status(self):
        self._check()
        self._drain_rds()
        return {"state": "OK", "i2c_errors": self.i2c_errors, "modulators": [
            {"id": i, "tx_state": "ON" if enabled else "OFF", "rf_output_dbuv": None,
             "audio_underrun_count": 0, "rds_underrun_count": 0, "rds_fifo_free": self.rds_capacity - len(self.rds_fifos[i]),
             "last_error_code": None} for i, enabled in enumerate(self.enabled)]}

    def get_rds_fifo_free(self) -> list[int]:
        self._check()
        self.rds_calls.append(("GET_RDS_FIFO_FREE",))
        self._drain_rds()
        return [self.rds_capacity - len(fifo) for fifo in self.rds_fifos]

    def _drain_rds(self):
        now = self.clock()
        for channel, fifo in enumerate(self.rds_fifos):
            ticks = int((now - self._rds_times[channel]) / (104 / 1187.5))
            if ticks:
                for _ in range(min(ticks, len(fifo))):
                    fifo.popleft()
                self._rds_times[channel] += ticks * (104 / 1187.5)

    def push_rds(self, modulator_id, groups):
        from .protocol import rds_groups, request, Command, Busy, Status
        self._check()
        request(Command.RDS_PUSH, modulator_id, rds_groups(groups))
        self._drain_rds()
        self.rds_calls.append(('RDS_PUSH', modulator_id, len(groups)))
        if len(groups) > self.rds_capacity - len(self.rds_fifos[modulator_id]):
            raise Busy(Command.RDS_PUSH, Status.BUSY)
        self.rds_fifos[modulator_id].extend(groups)
        self.rds_history.extend((modulator_id, group) for group in groups)

    def clear_rds(self, modulator_id):
        from .protocol import request, Command
        self._check()
        request(Command.RDS_CLEAR, modulator_id)
        self.rds_fifos[modulator_id].clear()
        self._rds_times[modulator_id] = self.clock()
        self.rds_calls.append(('RDS_CLEAR', modulator_id))
