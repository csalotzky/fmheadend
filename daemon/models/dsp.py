"""Validated settings for the installed native/LSP stereo adapters.

Units are profile units, not inferred from plugin GUI labels. No arbitrary
GObject properties are accepted. Defaults for LSP are installed 1.2.21 values.
"""
from typing import Literal
from pydantic import Field, model_validator
from .configuration import Model


class Gain(Model):
    gain_db: float = Field(0, le=20, title='Gain (dB)')


class Trim(Gain):
    gain_db: float = Field(0, le=0, title='Output Trim (dB)')


class Chebyshev(Model):
    cutoff_hz: float = Field(30, gt=0, lt=24000, title='Cutoff (Hz)')
    poles: int = Field(4, ge=2, le=32, multiple_of=2, title='Poles')
    ripple_db: float = Field(0, ge=0, le=200, json_schema_extra={'hidden': True})
    type: Literal[1, 2] = Field(1, json_schema_extra={'hidden': True})


class LspDynamics(Model):
    threshold_dbfs: float = Field(-12, ge=-60, le=0, title='Threshold (dBFS)')
    ratio: float = Field(4, ge=1, le=100, title='Ratio (N:1)')
    attack_ms: float = Field(20, ge=0, le=2000, title='Attack (ms)')
    release_ms: float = Field(100, ge=0, le=5000, title='Release (ms)')
    knee_db: float = Field(6, ge=0, le=23.99, title='Knee (dB)')
    makeup_db: float = Field(0, ge=-60, le=60, title='Makeup gain (dB)')
    hold_ms: float = Field(0, ge=0, le=1000, json_schema_extra={'hidden': True})
    release_threshold: float = Field(0, ge=0, le=1, json_schema_extra={'hidden': True})
    stereo_split: bool = Field(False, json_schema_extra={'hidden': True})
    dry_gain: float = Field(0, ge=0, le=10, json_schema_extra={'hidden': True})
    wet_gain: float = Field(1, ge=0, le=10, json_schema_extra={'hidden': True})
    dry_wet_percent: float = Field(100, ge=0, le=100, json_schema_extra={'hidden': True})


class LspExpander(LspDynamics):
    mode: Literal['downward', 'upward'] = 'downward'


class LspGate(Model):
    threshold_dbfs: float = Field(-24, ge=-60, le=0, title='Threshold (dBFS)')
    reduction_db: float = Field(24, ge=0, le=72, title='Reduction (dB attenuation)')
    attack_ms: float = Field(20, ge=0, le=2000, title='Attack (ms)')
    hold_ms: float = Field(0, ge=0, le=1000, title='Hold (ms)')
    release_ms: float = Field(100, ge=0, le=5000, title='Release (ms)')
    zone_db: float = Field(6, ge=0, le=60, json_schema_extra={'hidden': True})
    hysteresis: bool = Field(False, json_schema_extra={'hidden': True})
    hysteresis_threshold: float = Field(.25119, ge=.001, le=1, json_schema_extra={'hidden': True})
    hysteresis_zone: float = Field(.50118, ge=.001, le=1, json_schema_extra={'hidden': True})


class LspAutogain(Model):
    target_lufs: float = Field(-23, ge=-60, le=0, title='Target loudness (LUFS)')
    maximum_gain_enabled: bool = Field(True, title='Limit maximum gain')
    maximum_gain_db: float = Field(36, ge=0, le=108, title='Maximum gain (dB)', json_schema_extra={'enabled_when': 'maximum_gain_enabled'})
    silence_lufs: float = Field(-72, ge=-84, le=-36, title='Silence threshold (LUFS)')
    long_grow_ms: float = Field(500, ge=10, le=10000, title='Long-time Grow (ms per saved amount)')
    long_fall_ms: float = Field(500, ge=10, le=10000, title='Long-time Fall (ms per saved amount)')
    short_fall_ms: float = Field(10, ge=.1, le=40, title='Short-time Fall (ms per saved amount)')
    short_grow_enabled: bool = Field(False, title='Short-time Grow enabled')
    short_grow_ms: float = Field(40, ge=1, le=500, title='Short-time Grow (ms per saved amount)', json_schema_extra={'enabled_when': 'short_grow_enabled'})
    long_period_ms: float = Field(400, ge=100, le=2000, json_schema_extra={'hidden': True})
    short_period_ms: float = Field(20, ge=5, le=100, json_schema_extra={'hidden': True})
    drift_db: float = Field(12, ge=0, le=24, json_schema_extra={'hidden': True})
    # Native amounts index: 0.1/.5/1/3/6/9/10/12/15/18/20/21/24 dB.
    long_grow_amount: int = Field(7, ge=0, le=12, json_schema_extra={'hidden': True})
    long_fall_amount: int = Field(7, ge=0, le=12, json_schema_extra={'hidden': True})
    short_grow_amount: int = Field(7, ge=0, le=12, json_schema_extra={'hidden': True})
    short_fall_amount: int = Field(7, ge=0, le=12, json_schema_extra={'hidden': True})


class LspLimiter(Model):
    threshold_dbfs: float = Field(0, ge=-48, le=0, title='Threshold (dBFS)')
    lookahead_ms: float = Field(5, ge=.1, le=20, title='Lookahead (ms)')
    attack_ms: float = Field(5, ge=.25, le=20, title='Attack (ms)')
    release_ms: float = Field(5, ge=.25, le=20, title='Release (ms)')
    stereo_link_percent: float = Field(100, ge=0, le=100, title='Stereo Link (%)')
    oversampling: Literal[0] = Field(0, json_schema_extra={'hidden': True})
    mode: Literal[0] = Field(0, json_schema_extra={'hidden': True})
    boost: Literal[False] = Field(False, json_schema_extra={'hidden': True})
    alr: bool = Field(False, json_schema_extra={'hidden': True})
    alr_attack_ms: float = Field(5, ge=.1, le=200, json_schema_extra={'hidden': True})
    alr_release_ms: float = Field(50, ge=10, le=1000, json_schema_extra={'hidden': True})


class LspEqBand(Model):
    filter: Literal['bell', 'low_shelf', 'high_shelf', 'notch'] = 'bell'
    frequency_hz: float = Field(1000, ge=10, lt=24000, title='Frequency (Hz)')
    gain_db: float = Field(0, ge=-35, le=35, title='Gain (dB)', json_schema_extra={'disabled_when': {'filter': 'notch'}})
    q: float = Field(1, ge=0, le=100, title='Q')
    native_mode: Literal[0] = Field(0, json_schema_extra={'hidden': True})


class LspEq(Model):
    bands: list[LspEqBand] = Field(default_factory=lambda: [LspEqBand(frequency_hz=f)
        for f in (100, 1100, 11000)], min_length=0, max_length=16)


class MultibandBand(Model):
    bypass: bool = Field(False, title='Compressor Bypass', description='Disables compression for this band without muting it.')
    threshold_dbfs: float = Field(-12, ge=-60, le=0, title='Threshold (dBFS)')
    ratio: float = Field(1, ge=1, le=100, title='Ratio (N:1)')
    attack_ms: float = Field(20, ge=0, le=2000, title='Attack (ms)')
    release_ms: float = Field(100, ge=0, le=5000, title='Release (ms)')
    knee_db: float = Field(6, ge=0, le=23.99, title='Knee (dB)')
    makeup_db: float = Field(0, ge=-60, le=60, title='Makeup gain (dB)')
    hold_ms: float = Field(0, ge=0, le=1000, json_schema_extra={'hidden': True})


class Multiband(Model):
    mode: Literal['classic'] = Field('classic', json_schema_extra={'hidden': True})
    crossovers_hz: list[float] = Field(default_factory=lambda: [120, 1000, 5000], min_length=0,
                                     max_length=7, title='Ascending crossovers (Hz)')
    # Keep unused band's settings when reducing the active crossover count.
    bands: list[MultibandBand] = Field(default_factory=lambda: [MultibandBand() for _ in range(8)],
        min_length=8, max_length=8, json_schema_extra={'active_count_from': 'crossovers_hz'})

    @model_validator(mode='after')
    def ascending(self):
        if any(not 10 <= f <= 20000 for f in self.crossovers_hz) or any(
                a >= b for a, b in zip(self.crossovers_hz, self.crossovers_hz[1:])):
            raise ValueError('Crossovers must be strictly ascending, within 10..20000 Hz')
        return self


# Settings schemas are shared by validation, the editor description and adapters.
IMPLEMENTATIONS = {
    'VOLUME': (Gain, 'volume', 'Gain'),
    'TRIM': (Trim, 'volume', 'Output Trim'),
    'CHEBYSHEV': (Chebyshev, 'audiocheblimit', 'Chebyshev'),
    'LSP_EXPANDER': (LspExpander, 'lsp-plug-in-plugins-lv2-expander-stereo', 'LSP Expander Stereo'),
    'LSP_GATE': (LspGate, 'lsp-plug-in-plugins-lv2-gate-stereo', 'LSP Gate Stereo'),
    'LSP_AGC': (LspAutogain, 'lsp-plug-in-plugins-lv2-autogain-stereo', 'LSP Autogain Stereo'),
    'LSP_COMPRESSOR': (LspDynamics, 'lsp-plug-in-plugins-lv2-compressor-stereo', 'LSP Compressor Stereo'),
    'LSP_LIMITER': (LspLimiter, 'lsp-plug-in-plugins-lv2-limiter-stereo', 'LSP Limiter Stereo'),
    'LSP_EQ': (LspEq, 'lsp-plug-in-plugins-lv2-para-equalizer-x16-stereo', 'LSP Parametric EQ x16 Stereo'),
    'LSP_MULTIBAND': (Multiband, 'lsp-plug-in-plugins-lv2-mb-compressor-stereo', 'LSP Multiband Compressor x8 Stereo'),
}
STAGES = {
    'input_gain': ('Profile Input Gain', ['VOLUME']),
    'hpf': ('HPF', ['OFF', 'CHEBYSHEV']),
    'gate': ('Gate / Expander', ['OFF', 'LSP_EXPANDER', 'LSP_GATE']),
    'agc': ('AGC / Leveller', ['OFF', 'LSP_AGC']),
    'eq': ('EQ', ['OFF', 'LSP_EQ']),
    'wideband': ('Wideband Compressor', ['OFF', 'LSP_COMPRESSOR']),
    'multiband': ('Multiband Compressor', ['OFF', 'LSP_MULTIBAND']),
    'master_gain': ('Overall Gain', ['VOLUME']),
    'limiter': ('Limiter', ['OFF', 'LSP_LIMITER']),
    'lpf': ('LPF', ['OFF', 'CHEBYSHEV']),
    'output_trim': ('Output Trim', ['TRIM']),
}


def neutral_stages():
    result = {}
    for key, (_, choices) in STAGES.items():
        result[key] = {'selected': choices[0], 'implementations': {
            name: IMPLEMENTATIONS[name][0]().model_dump() for name in choices if name != 'OFF'}}
    result['hpf']['selected'] = 'CHEBYSHEV'
    result['lpf']['selected'] = 'CHEBYSHEV'
    result['lpf']['implementations']['CHEBYSHEV']['cutoff_hz'] = 15000
    return result
