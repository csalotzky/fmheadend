from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: str | Path = "/var/lib/fmheadend/fmheadend.db"
    fm_band_min_khz: int = 87500
    fm_band_max_khz: int = 108000

    def __post_init__(self):
        if not 0 < self.fm_band_min_khz < self.fm_band_max_khz:
            raise ValueError("Invalid FM band limits")
