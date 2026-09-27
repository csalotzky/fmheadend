from dataclasses import dataclass
from pathlib import Path
from .regions import PLANS
from .sources.output import DEFAULT_STAGING_BUFFER_MS, DEFAULT_CONSUMER_BUFFER_MS, DEFAULT_STAGING_MAX_AGE_MS, pcm_capacity_bytes

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

    def __post_init__(self):
        pcm_capacity_bytes(self.pcm_staging_buffer_ms)
        pcm_capacity_bytes(self.pcm_consumer_buffer_ms)
        pcm_capacity_bytes(self.pcm_staging_max_age_ms)
        if type(self.region) is not int or self.region not in PLANS:
            raise ValueError("Unknown region: choose 0, 1, 2 or 3")
