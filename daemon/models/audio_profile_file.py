"""Version 1 file foundation; deliberately independent of Source DB references."""
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .configuration import BasicConfig, Model, Name


class GainSettings(Model):
    gain_db: float = Field(default=0, le=20)


class GainImplementations(Model):
    GST_VOLUME: GainSettings


class GainStage(Model):
    selected: Literal["GST_VOLUME"]
    implementations: GainImplementations


class ProcessorImplementations(Model):
    GST_BASIC: BasicConfig


class ProcessorStage(Model):
    selected: Literal["NONE", "GST_BASIC"]
    implementations: ProcessorImplementations


class ProfileStages(Model):
    gain: GainStage
    processor: ProcessorStage


class AudioProfileFile(Model):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    schema_version: Literal[1]
    name: Name
    stages: ProfileStages

    @model_validator(mode="before")
    @classmethod
    def strict_document(cls, value):
        if isinstance(value, dict):
            if type(value.get('schema_version')) is not int:
                raise ValueError("schema_version must be an integer")
            if 'stages' in value:
                # FastAPI's normal body validation must not coerce nested DSP settings.
                ProfileStages.model_validate(value['stages'], strict=True)
        return value


class CloneProfileFile(Model):
    name: Name


class ProfileFileSummary(Model):
    name: str
    path: str
    kind: Literal["default", "custom"]


class ProfileFileResult(ProfileFileSummary):
    profile: AudioProfileFile


class ProfileSaveResult(ProfileFileResult):
    saved: Literal[True] = True
    applied: bool
    restart_errors: dict[int, str]
