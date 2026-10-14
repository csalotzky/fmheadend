from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model, field_validator, model_validator

from ..sources.local_media import LocalMediaConfig, LocalMediaMode, PlaybackOrder
from ..sources.podcast import PodcastScheduleMode, valid_http_url


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
    LOCAL = "LOCAL"
    PODCAST = "PODCAST"


class HttpConfig(Model):
    uri: str
    radio_browser_uuid: UUID | None = None
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
    device: Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"\S")]
    channel_selection: Literal["stereo", "mono", "left", "right"] = "stereo"

    @field_validator("device")
    @classmethod
    def valid_device(cls, value):
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("ALSA device name must not contain control characters")
        return value


class LocalConfig(Model):
    """Persisted local settings; filesystem availability is never save-time validation."""
    mode: LocalMediaMode
    path: str = Field(min_length=1, strict=True)
    order: PlaybackOrder = PlaybackOrder.SEQUENTIAL
    recursive: bool = Field(default=False, strict=True)

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value):
        # Accept the Linux target's absolute paths during Windows development,
        # as well as absolute paths native to the current host. Never resolve.
        if "\x00" in value or not (PurePosixPath(value).is_absolute() or Path(value).is_absolute()):
            raise ValueError("path must be an absolute host filesystem path, not a URI")
        return value

    def to_core(self) -> LocalMediaConfig:
        """Convert on the playback host; not called during validation/persistence."""
        return LocalMediaConfig(self.mode, Path(self.path), self.recursive, self.order)


class PodcastFeedConfig(Model):
    url: str = Field(min_length=1, max_length=8192, strict=True)
    max_age_days: int | None = Field(default=None, ge=0, strict=True)
    max_episodes: int | None = Field(default=None, ge=0, strict=True)
    _url = field_validator("url")(valid_http_url)


class PodcastConfig(Model):
    # Bound source-test diagnostics/work on this small appliance. No reachability
    # checks occur at save time; query-bearing URLs keep their original spelling.
    feeds: list[PodcastFeedConfig] = Field(min_length=1, max_length=32)
    schedule_mode: PodcastScheduleMode = PodcastScheduleMode.SMART
    refresh_interval_minutes: int = Field(default=30, gt=0, strict=True)
    replay_cooldown_hours: int = Field(default=24, ge=0, strict=True)

    @field_validator("feeds")
    @classmethod
    def unique_feeds(cls, value):
        if len({f.url for f in value}) != len(value):
            raise ValueError("Feed URLs must be unique within a source")
        return value


SOURCE_CONFIGS = {
    SourceType.HTTP: HttpConfig, SourceType.UDP: UdpConfig,
    SourceType.RTP: RtpConfig, SourceType.ALSA: AlsaConfig, SourceType.LOCAL: LocalConfig,
    SourceType.PODCAST: PodcastConfig,
}


def validate_rds_text(text: str) -> str:
    # Encoding is a later milestone. Reject controls; retain Unicode text here.
    if any(not char.isprintable() for char in text):
        raise ValueError("RDS text must not contain control characters")
    return text


class PsEntry(Model):
    text: str = Field(min_length=1, max_length=8)
    duration_s: float = Field(default=5, gt=0)
    _text = field_validator("text")(validate_rds_text)


class RdsProfileConfig(Model):
    rds_af_khz: list[Annotated[int, Field(strict=True, ge=87600, le=107900, multiple_of=100)]] = Field(default_factory=list, max_length=25)
    rds_ecc: int | None = Field(default=None, ge=0, le=255, strict=True)
    rds_pi: int = Field(default=0, ge=0, le=65535, strict=True)
    rds_ps: list[PsEntry] = Field(default_factory=lambda: [PsEntry(text="FM TX")], min_length=1)
    rds_pty: int = Field(default=0, ge=0, le=31, strict=True)
    rds_tp: bool = False
    rds_rt: str = Field(default="", max_length=64)
    rds_rt_enabled: bool = Field(default=True, strict=True)
    rds_rt_termination: Literal["CR", "PAD"] = "CR"
    rds_metadata_to_rt: bool = False
    _rt = field_validator("rds_rt")(validate_rds_text)


class RdsProfile(RdsProfileConfig):
    id: PositiveId


RDS_FIELDS = frozenset(RdsProfileConfig.model_fields)


class InputSourceConfig(Model):
    rds_profile: RdsProfileConfig | None = None
    name: Name
    type: SourceType
    config: JsonObject
    processor_mode: Literal['bypass', 'builtin', 'stereotool'] = 'bypass'
    input_gain_db: float = Field(default=0, le=20)
    audio_profile_path: str | None = None
    gst_options: dict[str, JsonObject] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config(self):
        import re
        if self.processor_mode == 'bypass' and self.audio_profile_path is not None:
            raise ValueError('Bypass requires audio_profile_path=null')
        if self.processor_mode == 'builtin' and (self.audio_profile_path is None or not re.fullmatch(
                r'(default|custom)/[a-zA-Z0-9_-]+\.json', self.audio_profile_path)):
            raise ValueError('Built-in DSP requires a default/custom JSON profile path')
        if self.processor_mode == 'stereotool' and self.audio_profile_path is not None and not re.fullmatch(
                r'(default|custom)/[a-zA-Z0-9_-]+\.sts', self.audio_profile_path):
            raise ValueError('Stereo Tool presets must be relative default/custom .sts paths')
        self.config = SOURCE_CONFIGS[self.type].model_validate(self.config).model_dump(mode="json")
        if set(self.gst_options) - {"source", "queue", "resample"}:
            raise ValueError("gst_options supports source, queue and resample property maps")
        return self


class InputSource(InputSourceConfig):
    rds_profile_id: PositiveId | None = None
    id: PositiveId
    timestamp: datetime


class SourceSaveResult(InputSource):
    saved: Literal[True] = True
    applied: bool
    apply_error: str | None = None


class ModulatorConfig(RdsProfileConfig):
    # Flat fields are effective read projections / legacy override writes only.
    # The repository persists content exclusively in owned RdsProfile rows.
    gain_mode: Literal["AUTO", "MANUAL"] = "AUTO"
    analog_gain_db: float = Field(default=0.0, ge=-3276.8, le=3276.7, multiple_of=0.1, strict=True)
    digital_trim_db: int = Field(default=0, ge=0, le=1, strict=True)
    soft_clipping: bool = Field(default=False, strict=True)
    audio_mode: Literal["DIGITAL", "ANALOG"] = "DIGITAL"
    enabled: bool = False
    input_source_id: PositiveId | None = None
    frequency_khz: int | None = Field(default=None, gt=0, strict=True)
    stereo: bool = True
    preemphasis_us: Literal[50, 75] = 50
    rf_attenuation_db: float | None = Field(default=None, ge=0, le=22.5, multiple_of=1.5, strict=True)
    rds_enabled: bool = False
    rds_follow_source: bool = False
    rds_override_profile: RdsProfileConfig | None = None

    @model_validator(mode="after")
    def analog_has_no_source(self):
        if self.rds_follow_source and (self.audio_mode != "DIGITAL" or self.input_source_id is None):
            raise ValueError("Follow source requires DIGITAL and an assigned source")
        if self.audio_mode == "ANALOG":
            self.input_source_id = None
        return self


class Modulator(ModulatorConfig):
    rds_override_profile_id: PositiveId | None = None
    id: int = Field(ge=0, le=7, strict=True)
    timestamp: datetime


class SystemConfig(Model):
    global_stopped: bool = False
    spi_device: Name = "/dev/spidev0.0"
    spi_clock_hz: int = Field(default=25000000, gt=0)
    i2c_device: Name = "/dev/i2c-1"
    controller_i2c_address: int | None = Field(default=None, ge=8, le=119)
    controller_status_poll_interval_ms: int = Field(default=1000, gt=0)
    web_listen_address: Name = "0.0.0.0"
    web_port: Port = 8080
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


def patch_model(name, model, *, exclude=()):
    # Missing fields are omitted; explicit null is checked against the full
    # model after merging, so nullable relationships can be cleared safely.
    return create_model(name, __base__=Model, **{
        key: (field.annotation | None, None) for key, field in model.model_fields.items() if key not in exclude
    })


InputSourcePatch = patch_model("InputSourcePatch", InputSourceConfig)
ModulatorPatch = patch_model("ModulatorPatch", ModulatorConfig)
SystemConfigPatch = patch_model("SystemConfigPatch", SystemConfig, exclude={"global_stopped"})


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


class PodcastFeedProbe(Model):
    feed_id: str
    title: str | None = None
    error: str | None = None
    discovered_episodes: int
    eligible_episodes: int | None = None


class PodcastProbe(Model):
    feeds_attempted: int
    feeds_succeeded: int
    discovered_episodes: int
    eligible_episodes: int
    feeds: list[PodcastFeedProbe]


class SourceTestResult(Model):
    podcast: PodcastProbe | None = None
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
