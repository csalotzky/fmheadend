from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


PositiveId = Annotated[int, Field(strict=True, gt=0)]
Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
Rate = Annotated[int, Field(strict=True, gt=0)]
JsonObject = dict[str, JsonValue]


class SourceType(StrEnum):
    HTTP = "HTTP"
    UDP = "UDP"
    RTP = "RTP"
    ALSA = "ALSA"


class HttpConfig(Model):
    uri: str
    buffer_ms: int = Field(default=3000, ge=0)
    connect_timeout_ms: int = Field(default=10000, gt=0)

    @field_validator("uri")
    @classmethod
    def valid_uri(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or any(c.isspace() for c in value):
            raise ValueError("uri must be an HTTP(S) URL")
        return value


class UdpConfig(Model):
    host: Name = "0.0.0.0"
    port: Port
    multicast_group: str | None = None
    format: Literal["S16LE", "S16BE", "S24LE", "S24BE", "S32LE", "S32BE", "F32LE", "F32BE", "U8"]
    sample_rate: Rate
    channels: int = Field(ge=1, le=8, strict=True)


class RtpConfig(Model):
    host: Name = "0.0.0.0"
    port: Port
    multicast_group: str | None = None
    codec: Literal["PCM", "MP3", "AAC", "OPUS"]
    payload_type: int = Field(ge=0, le=127, strict=True)
    clock_rate: Rate | None = None
    channels: int | None = Field(default=None, ge=1, le=8, strict=True)
    codec_data: str | None = Field(default=None, pattern=r"^(?:[0-9A-Fa-f]{2})+$")


class AlsaConfig(Model):
    device: Name
    sample_rate: Rate = 48000
    channels: int = Field(default=2, ge=1, le=8, strict=True)


SOURCE_CONFIGS = {
    SourceType.HTTP: HttpConfig, SourceType.UDP: UdpConfig,
    SourceType.RTP: RtpConfig, SourceType.ALSA: AlsaConfig,
}


class InputSourceConfig(Model):
    name: Name
    type: SourceType
    enabled: bool = True
    config: JsonObject
    audio_profile_id: PositiveId | None = None
    gst_options: dict[str, JsonObject] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config(self):
        self.config = SOURCE_CONFIGS[self.type].model_validate(self.config).model_dump(mode="json")
        if set(self.gst_options) - {"source", "queue", "resample"}:
            raise ValueError("gst_options supports source, queue and resample property maps")
        return self


class InputSource(InputSourceConfig):
    id: PositiveId
    timestamp: datetime


class AudioProfileConfig(Model):
    name: Name
    processing_enabled: bool = False
    gain_db: float = 0
    eq_config: JsonObject | None = None
    compressor_config: JsonObject | None = None
    limiter_config: JsonObject | None = None
    processor: Literal["GSTREAMER"] = "GSTREAMER"
    advanced_options: JsonObject | None = None


class AudioProfile(AudioProfileConfig):
    id: PositiveId
    timestamp: datetime


def validate_rds_text(text: str) -> str:
    # Encoding is a later milestone. Reject controls; retain Unicode text here.
    if any(not char.isprintable() for char in text):
        raise ValueError("RDS text must not contain control characters")
    return text


class PsEntry(Model):
    text: str = Field(min_length=1, max_length=8)
    duration_s: float = Field(default=5, gt=0)
    _text = field_validator("text")(validate_rds_text)


class ModulatorConfig(Model):
    audio_mode: Literal["DIGITAL", "ANALOG"] = "DIGITAL"
    enabled: bool = False
    input_source_id: PositiveId | None = None
    frequency_khz: int | None = Field(default=None, gt=0, strict=True)
    stereo: bool = True
    preemphasis_us: Literal[50, 75] = 50
    rf_output_dbuv: float | None = Field(default=None, ge=0, le=6553.5, multiple_of=0.1, strict=True)
    rds_enabled: bool = False
    rds_af_khz: list[Annotated[int, Field(strict=True, ge=87600, le=107900, multiple_of=100)]] = Field(default_factory=list, max_length=25)
    rds_ecc: int | None = Field(default=None, ge=0, le=255, strict=True)
    rds_pi: int = Field(default=0, ge=0, le=65535, strict=True)
    rds_ps: list[PsEntry] = Field(default_factory=lambda: [PsEntry(text="FMHEAD")], min_length=1)
    rds_pty: int = Field(default=0, ge=0, le=31, strict=True)
    rds_tp: bool = False
    rds_rt: str = Field(default="", max_length=64)
    rds_metadata_to_rt: bool = False
    _rt = field_validator("rds_rt")(validate_rds_text)

    @model_validator(mode="after")
    def analog_has_no_source(self):
        if self.audio_mode == "ANALOG":
            self.input_source_id = None
        return self


class Modulator(ModulatorConfig):
    id: int = Field(ge=0, le=7, strict=True)
    timestamp: datetime


class SystemConfig(Model):
    spi_device: Name = "/dev/spidev0.0"
    spi_clock_hz: int = Field(default=25000000, gt=0)
    i2c_device: Name = "/dev/i2c-1"
    rp2354_i2c_address: int | None = Field(default=None, ge=8, le=119)
    rp_status_poll_interval_ms: int = Field(default=1000, gt=0)
    web_listen_address: Name = "0.0.0.0"
    web_port: Port = 8080
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


def patch_model(name, model):
    # Missing fields are omitted; explicit null is checked against the full
    # model after merging, so nullable relationships can be cleared safely.
    return create_model(name, __base__=Model, **{
        key: (field.annotation | None, None) for key, field in model.model_fields.items()
    })


InputSourcePatch = patch_model("InputSourcePatch", InputSourceConfig)
AudioProfilePatch = patch_model("AudioProfilePatch", AudioProfileConfig)
ModulatorPatch = patch_model("ModulatorPatch", ModulatorConfig)
SystemConfigPatch = patch_model("SystemConfigPatch", SystemConfig)


class RtOverride(Model):
    text: str = Field(max_length=64)
    ttl_s: float | None = Field(default=None, gt=0)
    _text = field_validator("text")(validate_rds_text)


class RtStatus(Model):
    text: str
    origin: Literal["override", "metadata", "fallback"]
    remaining_ttl_s: float | None = None


class ErrorResponse(Model):
    error: str
    message: str


class SourceTestResult(Model):
    input_bitrate_bps: int | None = Field(default=None, gt=0)
    station_name: str | None = None
    ok: bool = True
    mocked: bool = True
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    output_caps: str | None = None
    stream_title: str | None = None
    message: str = "Mock probe only; no connection or decoding was attempted."
