from dataclasses import dataclass
from pathlib import Path
from .regions import PLANS

@dataclass(frozen=True)
class Settings:
    database_path: str | Path = "/var/lib/fmheadend/fmheadend.db"
    region: int = 1
    webui_dir: str | Path | None = None
    audio_profiles_default_dir: str | Path = "/usr/share/fmheadend/audio-profiles/default"
    audio_profiles_custom_dir: str | Path = "/var/lib/fmheadend/audio-profiles/custom"

    def __post_init__(self):
        if type(self.region) is not int or self.region not in PLANS:
            raise ValueError("Unknown region: choose 0, 1, 2 or 3")
