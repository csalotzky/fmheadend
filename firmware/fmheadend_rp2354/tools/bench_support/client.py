"""Bench configuration: same fields and validation as the host TxConfig."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

class TxConfig(Model):
    gain_mode: Literal["AUTO", "MANUAL"] = "AUTO"
    analog_gain_db: float = Field(default=0.0, ge=-3276.8, le=3276.7, multiple_of=0.1, strict=True)
    digital_trim_db: int = Field(default=0, ge=0, le=1, strict=True)
    soft_clipping: bool = Field(default=False, strict=True)
    frequency_khz: int
    rf_attenuation_db: float | None = Field(default=None, ge=0, le=22.5, multiple_of=1.5, strict=True)
    stereo: bool
    preemphasis_us: int
    audio_mode: Literal["DIGITAL", "ANALOG", "SINE"]
    rds_enabled: bool

