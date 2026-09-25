"""Version 2 modular profiles with strict, read-only v1 normalization."""
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .configuration import BasicConfig, Model, Name
from .dsp import IMPLEMENTATIONS, STAGES, neutral_stages


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


class LegacyProfileFile(Model):
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


class Stage(Model):
    selected: str
    implementations: dict[str, dict]


def migrate_v1(value):
    """Lossless, read-time migration. Never writes the original file on GET."""
    old = LegacyProfileFile.model_validate(value, strict=True)
    stages = neutral_stages()
    for stage, (_, choices) in STAGES.items():
        if 'OFF' in choices:
            stages[stage]['selected'] = 'OFF'
    stages['input_gain']['implementations']['VOLUME']['gain_db'] = old.stages.gain.implementations.GST_VOLUME.gain_db
    basic = old.stages.processor.implementations.GST_BASIC
    stages['hpf']['implementations']['CHEBYSHEV']['cutoff_hz'] = basic.highpass_hz
    stages['lpf']['implementations']['CHEBYSHEV']['cutoff_hz'] = basic.lowpass_hz
    stages['eq']['implementations']['LEGACY_EQ'] = dict(low_db=basic.eq_low_db, mid_db=basic.eq_mid_db, high_db=basic.eq_high_db)
    stages['wideband']['implementations']['DYNAMIC'].update(
        threshold_dbfs=basic.compressor_threshold_dbfs, ratio=1 / basic.compressor_ratio, knee='soft')
    if old.stages.processor.selected == 'GST_BASIC':
        for key, impl, enabled in [('hpf', 'CHEBYSHEV', basic.highpass_enabled),
                                  ('eq', 'LEGACY_EQ', True),
                                  ('wideband', 'DYNAMIC', basic.compressor_enabled),
                                  ('lpf', 'CHEBYSHEV', basic.lowpass_enabled),
                                  ('limiter', 'RGLIMITER', basic.limiter_enabled)]:
            stages[key]['selected'] = impl if enabled else 'OFF'
    return dict(schema_version=2, name=old.name, compatibility='gst_basic_v1', stages=stages)


class AudioProfileFile(Model):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)
    schema_version: Literal[2]
    name: Name
    # Explicit exception preserving the inherited LPF-before-limiter sound.
    compatibility: Literal['gst_basic_v1'] | None = None
    stages: dict[str, Stage]

    @model_validator(mode='before')
    @classmethod
    def document(cls, value):
        if isinstance(value, dict):
            if type(value.get('schema_version')) is not int:
                raise ValueError('schema_version must be an integer')
            if value['schema_version'] == 1:
                return migrate_v1(value)
        return value

    @model_validator(mode='after')
    def settings(self):
        if set(self.stages) != set(STAGES):
            raise ValueError('Profile must contain exactly the eleven ordered DSP stages')
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
    path: str
    kind: Literal["default", "custom"]


class ProfileFileResult(ProfileFileSummary):
    profile: AudioProfileFile


class ProfileSaveResult(ProfileFileResult):
    saved: Literal[True] = True
    applied: bool
    restart_errors: dict[int, str]
