from enum import StrEnum
from typing import Literal
from pydantic import Field
from .configuration import Model, Modulator


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


class ModulatorSaveResult(Model):
    saved: bool
    stop_confirmed: bool
    restarted: bool
    failed_stage: Literal['stop', 'save', 'start'] | None = None
    message: str
    configuration: Modulator | None = None


class PcmQueueStatus(Model):
    accepted_bytes: int = 0
    buffered_bytes: int = 0
    dropped_blocks: int = 0
    capacity_dropped_bytes: int = 0
    stale_dropped_bytes: int = 0
    invalid_dropped_bytes: int = 0
    cleared_bytes: int = 0


class SourcePcmStatus(Model):
    staging: PcmQueueStatus
    consumers: dict[int, PcmQueueStatus] = Field(default_factory=dict)


class WriterPcmStatus(Model):
    disconnects: int = 0
    stale_disconnects: int = 0
    discarded_pending_bytes: int = 0


class InputSourceStatus(Model):
    observation_age_s: float = 0
    observation_stale: bool = False
    pcm: SourcePcmStatus | None = None
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
    pcm_staging: PcmQueueStatus | None = None
    pcm_consumer: PcmQueueStatus | None = None
    pcm_writer: WriterPcmStatus | None = None
    input_saturated: bool | None = None
    id: int
    source_state: str | None = "IDLE"
    tx_state: TxState = TxState.OFF
    rf_attenuation_db: float | None = None
    underruns: int = 0
    overruns: int = 0
    pcm_bytes_received: int = 0
    pcm_bytes_consumed: int = 0
    # Selected text at publication time; queued RDS transmission may lag.
    rds_rt_effective: str | None = None
    rds_state: str = "DISABLED"
    rds_last_error: str | None = None
    rds_last_error_scope: Literal['TX', 'GLOBAL'] = 'TX'
    rds_groups_accepted: int = 0
    rds_text_ab: int = 0
    hardware_tx_state: str | None = None
    audio_underrun_count: int | None = None
    rds_underrun_count: int | None = None
    rds_fifo_free: int | None = None
    hardware_last_error: str | None = None
    last_error: str | None = None
    last_error_scope: Literal['TX', 'GLOBAL'] = 'TX'


class SystemStatus(Model):
    global_stopped: bool = False
    snapshot_age_s: float = 0
    snapshot_stale: bool = False
    system_state: str = "RUNNING"
    rp2354_state: str = "OK"
    rp_communication_state: str = "UNAVAILABLE"
    rp_observation_stale: bool = True
    rp_observation_age_s: float | None = None
    mux_state: str = "RUNNING"
    mux_transport: str = "mock"
    mux_transport_state: str = "RUNNING"
    mux_frames_sent: int = 0
    mux_late_frames: int = 0
    mux_transport_errors: int = 0
    mux_mocked: bool = True
    spi_errors: int = 0
    i2c_errors: int = 0
    rp_uptime_ms: int | None = None
    rp_spi_audio_link_error_count: int | None = None
    rp_last_error_code: str | None = None
    uptime_s: float = 0
    cpu_percent: float | None = None
    memory_percent: float | None = None
    temperature_c: float | None = None
    last_error: str | None = None
    mocked: bool = True


class HeadendStatus(Model):
    system: SystemStatus
    modulators: list[ModulatorStatus]
