from dataclasses import dataclass
from pathlib import Path
import math
from .regions import PLANS
from .sources.output import DEFAULT_STAGING_BUFFER_MS, DEFAULT_CONSUMER_BUFFER_MS, DEFAULT_STAGING_MAX_AGE_MS, pcm_capacity_bytes

DEFAULT_PASSWD_FILE = "/var/lib/fmheadend/htpasswd"


def audio_telemetry_hz(value):
    value = float(value)
    if not math.isfinite(value) or not 10 <= value <= 100:
        raise ValueError('Audio telemetry rate must be finite and between 10 and 100 Hz')
    return value


@dataclass(frozen=True)
class Settings:
    database_path: str | Path = "/var/lib/fmheadend/fmheadend.db"
    region: int = 1
    webui_dir: str | Path | None = None
    audio_profiles_default_dir: str | Path = "/usr/share/fmheadend/audio-profiles/default"
    audio_profiles_custom_dir: str | Path = "/var/lib/fmheadend/audio-profiles/custom"
    pcm_staging_buffer_ms: int = DEFAULT_STAGING_BUFFER_MS
    pcm_consumer_buffer_ms: int = DEFAULT_CONSUMER_BUFFER_MS
    pcm_staging_max_age_ms: int = DEFAULT_STAGING_MAX_AGE_MS
    media_browser_roots: tuple[str | Path, ...] = ()
    audio_telemetry_socket: str | Path | None = None
    audio_telemetry_hz: float = 50.0
    passwd_file: str | Path | None = None
    controller_status_poll_interval_ms: int = 1000

    def __post_init__(self):
        if type(self.controller_status_poll_interval_ms) is not int or self.controller_status_poll_interval_ms <= 0:
            raise ValueError("Controller status poll interval must be a positive integer")
        audio_telemetry_hz(self.audio_telemetry_hz)
        pcm_capacity_bytes(self.pcm_staging_buffer_ms)
        pcm_capacity_bytes(self.pcm_consumer_buffer_ms)
        pcm_capacity_bytes(self.pcm_staging_max_age_ms)
        if type(self.region) is not int or self.region not in PLANS:
            raise ValueError("Unknown region: choose 0, 1, 2 or 3")
        if any(str(root).replace("\\", "/") == "/" for root in self.media_browser_roots):
            raise ValueError("Media browser root must not be the filesystem root")
        if any(not Path(root).is_absolute() for root in self.media_browser_roots):
            raise ValueError("Media browser roots must be absolute paths")
        if any(Path(root) == Path(Path(root).anchor) for root in self.media_browser_roots):
            raise ValueError("Media browser root must not be the filesystem root")
