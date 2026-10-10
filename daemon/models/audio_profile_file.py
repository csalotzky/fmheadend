"""The single supported first-release modular profile schema."""
from typing import Literal

from pydantic import ConfigDict, model_validator

from .configuration import Model, Name
from .dsp import IMPLEMENTATIONS, STAGES


class Stage(Model):
    selected: str
    implementations: dict[str, dict]


class AudioProfileFile(Model):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)
    schema_version: Literal[1]
    name: Name
    description: str
    stages: dict[str, Stage]

    @model_validator(mode='before')
    @classmethod
    def document(cls, value):
        if isinstance(value, dict):
            if type(value.get('schema_version')) is not int:
                raise ValueError('schema_version must be an integer')
        return value

    @model_validator(mode='after')
    def settings(self):
        if set(self.stages) != set(STAGES):
            raise ValueError('Profile must contain exactly the twelve ordered DSP stages')
        for key, stage in self.stages.items():
            choices = STAGES[key][1]
            if stage.selected not in choices:
                raise ValueError(f'{key}: unsupported implementation {stage.selected}')
            if stage.selected != 'OFF' and stage.selected not in stage.implementations:
                raise ValueError(f'{key}: selected implementation settings missing')
            for name, settings in stage.implementations.items():
                if name not in choices or name == 'OFF':
                    raise ValueError(f'{key}: unsupported saved implementation {name}')
                stage.implementations[name] = IMPLEMENTATIONS[name][0].model_validate(settings, strict=True).model_dump()
        # Canonical order does not depend on JSON object insertion order.
        self.stages = {key: self.stages[key] for key in STAGES}
        return self


class CloneProfileFile(Model):
    name: Name


class ProfileFileSummary(Model):
    name: str
    description: str
    path: str
    kind: Literal["default", "custom"]


class ProfileFileResult(ProfileFileSummary):
    profile: AudioProfileFile


class ProfileSaveResult(ProfileFileResult):
    saved: Literal[True] = True
    applied: bool
    restart_errors: dict[int, str]
