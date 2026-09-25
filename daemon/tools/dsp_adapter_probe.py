"""Run copied production adapters in an isolated workspace, without daemon I/O."""
import json
import math
import time
from pathlib import Path
from dsp_native_probe import Gst, process, tone, rms
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import IMPLEMENTATIONS, STAGES, neutral_stages
from fmheadendd.sources.dsp import dsp_stages


def make(factory, **properties):
    element = Gst.ElementFactory.make(factory)
    assert element is not None, factory
    for key, value in properties.items():
        name = key.replace('_', '-')
        assert element.find_property(name), (factory, name)
        element.set_property(name, value)
        # GObject warnings/clamping must not pass silently.
        actual = element.get_property(name)
        if isinstance(value, float): assert math.isclose(actual, value, rel_tol=1e-5, abs_tol=1e-7), (factory, name, value, actual)
        else: assert actual == value, (factory, name, value, actual)
    return element


def isolated(stage, implementation, settings=None):
    stages = neutral_stages()
    for key, (_, choices) in STAGES.items():
        if 'OFF' in choices: stages[key]['selected'] = 'OFF'
    stages[stage]['selected'] = implementation
    if settings: stages[stage]['implementations'][implementation].update(settings)
    return AudioProfileFile.model_validate(dict(schema_version=1, name='Probe', stages=stages))


def run(profile, samples):
    return process(dsp_stages(make, profile), samples)


started, cpu = time.monotonic(), time.process_time()
evidence = {}
x = tone(1000, .4, frames=48000)
for stage, (_, choices) in STAGES.items():
    for impl in choices:
        if impl == 'OFF': continue
        y = run(isolated(stage, impl), x)
        assert len(y) == len(x) and all(math.isfinite(v) for v in y) and max(abs(v) for v in y) < 2
        evidence[stage + '/' + impl] = dict(gain=rms(y)/rms(x), peak=max(abs(v) for v in y))

# True channel separation, also for linked stereo processors.
left_only = x.copy(); left_only[1::2] = [0.] * (len(left_only)//2)
for stage, impl in [('gate','LSP_GATE'), ('gate','LSP_EXPANDER'), ('agc','LSP_AGC'),
                    ('eq','LSP_EQ'), ('wideband','LSP_COMPRESSOR'), ('multiband','LSP_MULTIBAND'), ('limiter','LSP_LIMITER')]:
    y = run(isolated(stage, impl), left_only)
    assert max(abs(v) for v in y[1::2]) < 1e-7, impl

for filter_name, expected in [('bell', 1.99526), ('notch', 0.)]:
    band = {'filter':filter_name, 'frequency_hz':1000., 'gain_db':6., 'q':2., 'native_mode':0}
    y = run(isolated('eq','LSP_EQ',{'bands':[band]}), x)
    assert abs(rms(y)/rms(x)-expected)<.01
assert abs(rms(run(isolated('eq','LSP_EQ',{'bands':[]}),x))/rms(x)-1)<1e-6

for count in (4,8):
    p = isolated('multiband','LSP_MULTIBAND')
    settings = p.stages['multiband'].implementations['LSP_MULTIBAND']
    settings['crossovers_hz'] = [120.,1000.,5000.] if count==4 else [80.,160.,320.,640.,1280.,2560.,5120.]
    for band in settings['bands']: band.update(bypass=True, threshold_dbfs=-26., ratio=4.)
    y = run(p,x)
    assert .98 < rms(y)/rms(x) < 1.02
    for band in settings['bands']: band['bypass']=False
    y = run(p,x)
    assert rms(y)/rms(x)<.5
    evidence[f'multiband_{count}_compressed_gain']=rms(y)/rms(x)

# Levelled output after a step; qamp explicitly Off/On both exercised.
grow_outputs=[]
for grow in (False, True):
    signal = tone(1000,.4,frames=96000)+tone(1000,.025,frames=96000)
    y = run(isolated('agc','LSP_AGC',{'short_grow_enabled':grow, 'long_grow_ms':10000., 'short_grow_ms':1.}), signal)
    assert rms(y) > 0 and rms(y) < rms(signal)
    evidence[f'agc_short_grow_{grow}_final_rms']=rms(y)
    grow_outputs.append(y)
evidence['agc_short_grow_max_difference']=max(abs(a-b) for a,b in zip(*grow_outputs))
assert evidence['agc_short_grow_max_difference'] > .001

evidence['elapsed_s']=time.monotonic()-started
evidence['cpu_s']=time.process_time()-cpu
print(json.dumps(evidence,indent=2))
