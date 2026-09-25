"""Fixed-order stereo processing, with explicit legacy Basic compatibility."""
import math
from ..models.dsp import IMPLEMENTATIONS, STAGES, neutral_stages
from ..errors import DependencyUnavailable

BLOCKED = {'SINC': 'Target GStreamer 1.26.2 sinc tests produced intermittent non-finite/huge output; disabled pending native investigation'}


def linear(db):
    return 10 ** (db / 20)


def dsp_stages(make, profile):
    create = make
    def make(factory, **properties):
        element = create(factory, **properties)
        # GObject can warn and clamp instead of raising on a version mismatch.
        # Fail explicitly rather than claim that the requested setting applied.
        if hasattr(element, 'get_property'):
            for key, value in properties.items():
                actual = element.get_property(key.replace('_', '-'))
                equal = (math.isclose(actual, value, rel_tol=1e-5, abs_tol=1e-7)
                         if isinstance(value, float) else actual == value)
                if not equal:
                    raise DependencyUnavailable(f'{factory}.{key}: requested {value}, native readback {actual}')
        return element
    order = list(STAGES)
    if profile.compatibility == 'gst_basic_v1':
        order.remove('lpf')
        order.insert(order.index('limiter'), 'lpf')
    result = []
    for stage in order:
        selection = profile.stages[stage]
        name = selection.selected
        if name == 'OFF':
            continue
        if name in BLOCKED:
            raise DependencyUnavailable(BLOCKED[name])
        p = selection.implementations[name]
        factory = IMPLEMENTATIONS[name][1]
        if name in ('VOLUME', 'TRIM'):
            props = dict(volume=linear(p['gain_db']))
        elif name == 'CHEBYSHEV':
            props = dict(mode=1 if stage == 'hpf' else 0, cutoff=p['cutoff_hz'], poles=p['poles'],
                         type=p['type'], ripple=p['ripple_db'])
        elif name == 'SINC':
            props = dict(mode=0, cutoff=p['cutoff_hz'], length=p['length'], window=p['window'],
                         low_latency=p['low_latency'], drain_on_changes=p['drain_on_changes'])
        elif name == 'DYNAMIC':
            props = dict(mode=1 if stage == 'gate' else 0, threshold=linear(p['threshold_dbfs']),
                         ratio=p['ratio'], characteristics=0 if p['knee'] == 'hard' else 1)
        elif name == 'LEGACY_EQ':
            props = dict(band0=p['low_db'], band1=p['mid_db'], band2=p['high_db'])
        elif name == 'EQ':
            if not p['bands']:
                continue
            element = make(factory, num_bands=len(p['bands']))
            for index, band in enumerate(p['bands']):
                child = element.get_child_by_index(index)
                for prop, value in dict(freq=band['frequency_hz'], gain=band['gain_db'],
                                        bandwidth=band['bandwidth_hz'], type=band['type']).items():
                    child.set_property(prop, value)
            result.append(element)
            continue
        elif name == 'RGLIMITER':
            props = dict(enabled=True)
        elif name in ('LSP_COMPRESSOR', 'LSP_EXPANDER'):
            props = dict(enabled=True, al=linear(p['threshold_dbfs']), at=p['attack_ms'], rt=p['release_ms'],
                         kn=linear(-p['knee_db']), mk=linear(p['makeup_db']), hold=p['hold_ms'],
                         rrl=p['release_threshold'], ssplit=p['stereo_split'], cdr=p['dry_gain'],
                         cwt=p['wet_gain'], cdw=p['dry_wet_percent'])
            if name == 'LSP_COMPRESSOR': props.update(cm=0, cr=p['ratio'])
            else: props.update(em=0 if p['mode'] == 'downward' else 1, er=p['ratio'])
        elif name == 'LSP_GATE':
            props = dict(enabled=True, gt=linear(p['threshold_dbfs']), gr=linear(-p['reduction_db']),
                         at=p['attack_ms'], rt=p['release_ms'], hold=p['hold_ms'], gz=linear(-p['zone_db']),
                         gh=p['hysteresis'], ht=p['hysteresis_threshold'], hz=p['hysteresis_zone'])
        elif name == 'LSP_AGC':
            props = dict(enabled=True, level=p['target_lufs'], max_on=p['maximum_gain_enabled'],
                         max_amp=p['maximum_gain_db'], silence=p['silence_lufs'], qamp=p['short_grow_enabled'],
                         tgrow_l=p['long_grow_ms'], tfall_l=p['long_fall_ms'], tgrow_s=p['short_grow_ms'],
                         tfall_s=p['short_fall_ms'], lperiod=p['long_period_ms'], speriod=p['short_period_ms'],
                         drift=p['drift_db'], vgrow_l=p['long_grow_amount'], vfall_l=p['long_fall_amount'],
                         vgrow_s=p['short_grow_amount'], vfall_s=p['short_fall_amount'], weight=5, scmode=0)
        elif name == 'LSP_LIMITER':
            props = dict(enabled=True, th=linear(p['threshold_dbfs']), lk=p['lookahead_ms'], at=p['attack_ms'],
                         rt=p['release_ms'], slink=p['stereo_link_percent'], ovs=p['oversampling'], mode=p['mode'],
                         boost=p['boost'], alr=p['alr'], alr_at=p['alr_attack_ms'], alr_rt=p['alr_release_ms'])
        elif name == 'LSP_EQ':
            props = dict(enabled=True, mode=0)
            for i in range(16):
                if i >= len(p['bands']):
                    props[f'ft_{i}'] = 0
                    continue
                band = p['bands'][i]
                props.update({f'ft_{i}': {'bell': 1, 'high_shelf': 3, 'low_shelf': 5, 'notch': 6}[band['filter']],
                    f'f_{i}': band['frequency_hz'], f'g_{i}': linear(band['gain_db']),
                    f'q_{i}': band['q'], f'fm_{i}': band['native_mode'], f's_{i}': 0,
                    f'xs_{i}': False, f'xm_{i}': False})
        elif name == 'LSP_MULTIBAND':
            props = dict(enabled=True, mode=0)
            active = len(p['crossovers_hz']) + 1
            for i in range(1, 8):
                props[f'cbe_{i}'] = i < active
                if i < active: props[f'sf_{i}'] = p['crossovers_hz'][i-1]
            for i, band in enumerate(p['bands']):
                props.update({f'ce_{i}': i < active and not band['bypass'],
                    f'bs_{i}': False, f'bm_{i}': False, f'cm_{i}': 0,
                    f'al_{i}': linear(band['threshold_dbfs']), f'at_{i}': band['attack_ms'],
                    f'rt_{i}': band['release_ms'], f'cr_{i}': band['ratio'],
                    f'kn_{i}': linear(-band['knee_db']), f'mk_{i}': linear(band['makeup_db']),
                    f'ht_{i}': band['hold_ms']})
        else:
            raise ValueError(f'No verified adapter for {name}')
        result.append(make(factory, **props))
    return result


def editor_description():
    """One small editor contract derived from authoritative settings validation."""
    from .gst_pipeline import load_gst
    from ..errors import DependencyUnavailable
    try:
        Gst = load_gst()
    except DependencyUnavailable:
        Gst = None
    implementations = {}
    for key, (model, factory, label) in IMPLEMENTATIONS.items():
        available = key not in BLOCKED and bool(Gst and Gst.ElementFactory.find(factory))
        implementations[key] = dict(label=label, factory=factory, available=available,
            reason=None if available else BLOCKED.get(key, f'GStreamer element unavailable on this daemon: {factory}'),
            schema=model.model_json_schema(), defaults=model().model_dump())
    return dict(template=dict(schema_version=2, name='', compatibility=None, stages=neutral_stages()),
                stages=[dict(key=k, label=v[0], choices=v[1]) for k, v in STAGES.items()],
                implementations=implementations,
                helpers={
                    'input_gain': 'Shared profile gain after the independent Source Gain. Boost may drive later dynamics harder.',
                    'hpf': 'Removes very low-frequency content; excessive cutoff can remove musical bass.',
                    'gate': 'Downward expansion reduces quiet content. Gate Reduction attenuates a closed gate; it is not bypass.',
                    'agc': 'Levelling is not peak limiting. Grow/Fall times apply per saved amount (12 dB by default). Quick Grow may raise noise and pump.',
                    'eq': 'GStreamer Bandwidth is in Hz; LSP uses Q. Remove disables the filter slot; Notch has no gain control.',
                    'wideband': 'Full-band dynamics, independent of Multiband. Basic uses native amplitude slope, not a conventional N:1 ratio.',
                    'multiband': 'Crossovers define active bands. Compressor Bypass preserves the band audio and its saved settings.',
                },
                blocked={'agc': ['audioloudnorm: unavailable; no verified installed adapter'],
                         'multiband': ['Classic mode only. Eight-programme target load measurement pending.']})
