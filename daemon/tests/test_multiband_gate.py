import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import IMPLEMENTATIONS, neutral_stages
from fmheadendd.sources.dsp import dsp_stages
from fmheadendd.sources.dsp_telemetry import StageMeters


def load_script(path):
    spec = importlib.util.spec_from_file_location('probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('impl,enable', [('LSP_MULTIBAND_GATE', 'ge'), ('LSP_MULTIBAND_EXPANDER', 'ee')])
@pytest.mark.parametrize('count', [1, 4, 8])
def test_band_wiring_telemetry_roundtrip(impl, enable, count):
    document = dict(schema_version=1, name='test', stages=neutral_stages())
    stage = document['stages']['gate']
    stage['selected'] = impl
    settings = stage['implementations'][impl]
    settings['crossovers_hz'] = [80., 160., 320., 640., 1280., 2560., 5120.][:count - 1]
    settings['bands'][0].update(bypass=True, hold_ms=37., makeup_db=3.)
    profile = AudioProfileFile.model_validate(document)
    assert AudioProfileFile.model_validate_json(profile.model_dump_json()) == profile
    elements = []
    dsp_stages(lambda factory, **props: elements.append((factory, props)), profile)
    props = next(p for factory, p in elements if factory == IMPLEMENTATIONS[impl][1])
    assert props[f'{enable}_0'] is False
    assert props['mk_0'] == pytest.approx(10 ** (3 / 20))
    assert props['th_0' if enable == 'ge' else 'ht_0'] == 37
    for i in range(1, 8):
        assert props[f'cbe_{i}'] == (i < count)
        assert props[f'{enable}_{i}'] == (i < count)
        if i < count:
            assert props[f'sf_{i}'] == settings['crossovers_hz'][i - 1]
    assert not props['ssplit'] and not props['bm_0'] and not props['bs_0']
    class Native:
        def get_property(self, key):
            assert key in meters.properties
            return .5
    meters = StageMeters(Native(), impl, settings)
    sample = meters.collect()
    assert 'errors' not in sample
    assert f'elm-{count - 1}l' in sample['values']
    assert f'elm-{count}l' not in sample['values']
    assert ('rl-0' in sample['values']) == (enable == 'ee')
    monitor = load_script(Path(__file__).resolve().parents[2] / 'scripts/audio_monitor.py')
    text = '\n'.join(monitor.render_gate({'stages': {'gate': sample}}))
    assert ('MULTIBAND GATE' if enable == 'ge' else 'MULTIBAND EXPANDER') in text
    assert f'B{count} env' in text and f'B{count + 1} env' not in text
    profile.stages['gate'].selected = 'OFF'
    elements.clear()
    dsp_stages(lambda factory, **props: elements.append((factory, props)), profile)
    assert IMPLEMENTATIONS[impl][1] not in [f for f, _ in elements]


@pytest.mark.parametrize('impl', ['LSP_MULTIBAND_GATE', 'LSP_MULTIBAND_EXPANDER'])
@pytest.mark.parametrize('key,value', [
    ('crossovers_hz', [1000., 1000.]), ('crossovers_hz', [2000., 100.]),
    ('crossovers_hz', [9.]), ('crossovers_hz', [20001.]),
    ('crossovers_hz', [float('nan')]), ('crossovers_hz', list(range(100, 900, 100))),
    ('bands', [{}] * 7), ('bands', [{}] * 9),
])
def test_invalid_multiband_settings(impl, key, value):
    with pytest.raises(ValidationError):
        IMPLEMENTATIONS[impl][0].model_validate({key: value}, strict=True)


def test_prepared_v1_conversion_preserves_other_stages_and_refuses_enabled():
    prepare = load_script(Path(__file__).resolve().parents[1] / 'tools/prepare_multiband_gate_profiles.py')
    document = dict(schema_version=1, name='test', stages=neutral_stages())
    document['stages']['gate'] = dict(selected='OFF', implementations={'LSP_GATE': {'threshold_dbfs': -40.}})
    original = json.loads(json.dumps(document))
    converted = prepare.convert(document)
    assert document == original
    assert converted['schema_version'] == 1
    assert converted['stages']['gate']['selected'] == 'OFF'
    for key in document['stages']:
        if key != 'gate':
            assert converted['stages'][key] == document['stages'][key]
    assert converted['stages']['gate']['implementations']['LSP_MULTIBAND_GATE']['bands'][0]['threshold_dbfs'] == -40
    document['stages']['gate']['selected'] = 'LSP_GATE'
    with pytest.raises(ValueError, match='reviewed'):
        prepare.convert(document)
