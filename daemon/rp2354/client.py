from typing import Literal, Protocol
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
    def from_modulator(cls, modulator: Modulator, analog: bool):
        return cls(**modulator.model_dump(include={"frequency_khz", "rf_output_dbuv", "stereo", "preemphasis_us", "rds_enabled"}),
                   audio_mode="ANALOG" if analog else "DIGITAL")


class Rp2354Client(Protocol):
    def set_tx_config(self, modulator_id: int, config: TxConfig) -> None: ...
    def set_tx_enabled(self, modulator_id: int, enabled: bool) -> None: ...
    def get_info(self) -> dict: ...
    def get_status(self) -> dict: ...
    def get_rds_fifo_free(self) -> list[int]: ...


class MockRp2354Client:
    def __init__(self):
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
        return {"state": "OK", "i2c_errors": self.i2c_errors, "modulators": [
            {"id": i, "tx_state": "ON" if enabled else "OFF", "rf_output_dbuv": None,
             "audio_underrun_count": 0, "rds_underrun_count": 0, "rds_fifo_free": 0,
             "last_error_code": None} for i, enabled in enumerate(self.enabled)]}

    def get_rds_fifo_free(self) -> list[int]:
        self._check()
        return [0] * 8
