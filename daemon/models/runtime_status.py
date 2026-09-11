from enum import StrEnum
from .configuration import Model


class SourceState(StrEnum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    BUFFERING = "BUFFERING"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


class TxState(StrEnum):
    OFF = "OFF"
    STARTING = "STARTING"
    ON = "ON"
    ERROR = "ERROR"


class InputSourceStatus(Model):
    source_id: int
    state: SourceState = SourceState.IDLE
    codec: str | None = None
    input_sample_rate: int | None = None
    input_channels: int | None = None
    bitrate_bps: int | None = None
    stream_title: str | None = None
    artist: str | None = None
    title: str | None = None
    output_caps: str | None = None
    reconnect_count: int = 0
    last_error: str | None = None


class ModulatorStatus(Model):
    id: int
    source_state: str = "IDLE"
    tx_state: TxState = TxState.OFF
    rf_output_dbuv: float | None = None
    underruns: int = 0
    overruns: int = 0
    pcm_bytes_received: int = 0
    pcm_bytes_consumed: int = 0
    last_error: str | None = None


class SystemStatus(Model):
    system_state: str = "RUNNING"
    rp2354_state: str = "OK"
    mux_state: str = "RUNNING"
    mux_transport: str = "mock"
    mux_transport_state: str = "RUNNING"
    mux_frames_sent: int = 0
    mux_mocked: bool = True
    spi_errors: int = 0
    i2c_errors: int = 0
    uptime_s: float = 0
    cpu_percent: float | None = None
    memory_percent: float | None = None
    temperature_c: float | None = None
    last_error: str | None = None
    mocked: bool = True


class HeadendStatus(Model):
    system: SystemStatus
    modulators: list[ModulatorStatus]
