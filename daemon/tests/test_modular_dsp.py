import copy
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import STAGES, IMPLEMENTATIONS, neutral_stages
from fmheadendd.sources.basic import basic_stages
from fmheadendd.models.configuration import BasicConfig
from fmheadendd.sources.dsp import dsp_stages, editor_description


def profile():
    return dict(schema_version=2, name='Neutral', compatibility=None, stages=neutral_stages())


class Element:
    def __init__(self, factory, props):
        self.factory, self.props, self.bands = factory, props, {}
    def get_child_by_index(self, index):
        return self.bands.setdefault(index, Element('band', {}))
    def set_property(self, key, value): self.props[key] = value


def build(document):
    return dsp_stages(lambda factory, **p: Element(factory, p), AudioProfileFile.model_validate(document))


def test_fixed_order_and_gain_domains():
    doc = profile()
    for key, gain in [('input_gain', -3), ('master_gain', 6), ('output_trim', -6)]:
        impl = doc['stages'][key]['selected']
        doc['stages'][key]['implementations'][impl]['gain_db'] = gain
    chain = build(doc)
    assert list(AudioProfileFile.model_validate(doc).stages) == list(STAGES)
    assert [x.factory for x in chain] == ['volume', 'audiocheblimit', 'equalizer-nbands',
        'volume', 'rglimiter', 'audiocheblimit', 'volume']
    assert [e.props['volume'] for e in chain if e.factory == 'volume'] == pytest.approx([10**(-3/20),10**(6/20),10**(-6/20)])


def test_legacy_basic_chain_and_values_are_preserved():
    original = json.loads((Path(__file__).parents[2] / 'audio-profiles/default/basic.json').read_text())
    before = basic_stages(lambda factory, **p: Element(factory, p), BasicConfig())
    after = [e for e in build(original) if e.factory != 'volume']
    assert [(e.factory,e.props) for e in after] == [(e.factory,e.props) for e in before]
    migrated = AudioProfileFile.model_validate(original)
    assert migrated.schema_version == 2 and migrated.compatibility == 'gst_basic_v1'
    assert AudioProfileFile.model_validate(migrated.model_dump()) == migrated


def test_hidden_inactive_and_off_settings_preserved():
    doc = profile()
    doc['stages']['lpf']['implementations']['CHEBYSHEV']['ripple_db'] = .75
    doc['stages']['lpf']['selected'] = 'OFF'
    validated = AudioProfileFile.model_validate(doc).model_dump()
    assert validated['stages'] == doc['stages']
    validated['stages']['lpf']['selected'] = 'CHEBYSHEV'
    assert build(validated)[-2].props['ripple'] == .75


@pytest.mark.parametrize('stage,impl,key,value', [
    ('output_trim','TRIM','gain_db',.1), ('hpf','CHEBYSHEV','poles',3),
    ('lpf','SINC','length',100), ('lpf','SINC','cutoff_hz',24000),
    ('wideband','DYNAMIC','threshold_dbfs','-12'), ('input_gain','VOLUME','gain_db',float('nan')),
    ('limiter','LSP_LIMITER','made_up_ceiling',-.5),
])
def test_reject_invalid_including_inactive_settings(stage,impl,key,value):
    doc = profile(); doc['stages'][stage]['implementations'][impl][key] = value
    with pytest.raises(ValidationError): AudioProfileFile.model_validate(doc)


def test_incomplete_and_unavailable_schema_rejected():
    doc = profile(); del doc['stages']['multiband']
    with pytest.raises(ValidationError): AudioProfileFile.model_validate(doc)
    doc = profile(); doc['stages']['agc']['selected'] = 'LOUDNORM'
    with pytest.raises(ValidationError): AudioProfileFile.model_validate(doc)


def test_basic_dynamics_native_mapping_and_eq_bandwidth():
    doc=profile(); doc['stages']['gate']['selected']='DYNAMIC'
    doc['stages']['wideband']['selected']='DYNAMIC'
    chain=build(doc)
    dynamic=[e for e in chain if e.factory=='audiodynamic']
    assert [e.props['mode'] for e in dynamic] == [1,0]
    assert [e.props['ratio'] for e in dynamic] == [2,.5]
    assert dynamic[0].props['threshold'] == pytest.approx(10**(-12/20))
    eq=next(e for e in chain if e.factory=='equalizer-nbands')
    assert len(eq.bands)==3 and eq.bands[1].props['bandwidth']==1100
    assert 'q' not in eq.bands[1].props
    doc['stages']['eq']['implementations']['EQ']['bands']=[]
    assert not any(e.factory=='equalizer-nbands' for e in build(doc))


@pytest.mark.parametrize('stage,impl', [('gate','LSP_GATE'),('gate','LSP_EXPANDER'),
    ('agc','LSP_AGC'),('wideband','LSP_COMPRESSOR'),('limiter','LSP_LIMITER')])
def test_lsp_selected_enabled_and_exact_units(stage,impl):
    doc=profile(); doc['stages'][stage]['selected']=impl
    el=next(e for e in build(doc) if e.factory==IMPLEMENTATIONS[impl][1])
    assert el.props['enabled'] is True
    if impl=='LSP_GATE': assert el.props['gr']==pytest.approx(10**(-24/20))
    if impl=='LSP_EXPANDER': assert el.props['em']==0
    if impl=='LSP_AGC':
        assert el.props['qamp'] is False and el.props['vgrow_l']==7 and el.props['tgrow_l']==500
    if impl=='LSP_COMPRESSOR': assert el.props['al']==pytest.approx(10**(-12/20))
    if impl=='LSP_LIMITER': assert el.props['boost'] is False and el.props['slink']==100 and el.props['ovs']==0


def test_missing_plugin_is_source_failure_not_bypass(monkeypatch):
    from fmheadendd.sources.gst_pipeline import GstPipeline
    from fmheadendd.errors import DependencyUnavailable
    from types import SimpleNamespace
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst', lambda: SimpleNamespace(ElementFactory=SimpleNamespace(make=lambda *a: None)))
    with pytest.raises(DependencyUnavailable, match='Missing GStreamer element'):
        GstPipeline.validate_profile(AudioProfileFile.model_validate(profile()))


def test_unreliable_native_sinc_is_blocked_not_substituted():
    from fmheadendd.errors import DependencyUnavailable
    doc=profile(); doc['stages']['lpf']['selected']='SINC'
    with pytest.raises(DependencyUnavailable, match='sinc tests'):
        build(doc)


def test_lsp_eq_remove_disables_slot_and_q_is_not_bandwidth():
    doc=profile(); stage=doc['stages']['eq'];stage['selected']='LSP_EQ'
    stage['implementations']['LSP_EQ']['bands']=[dict(filter='notch',frequency_hz=1000.,gain_db=0.,q=2.)]
    el=next(e for e in build(doc) if e.factory==IMPLEMENTATIONS['LSP_EQ'][1])
    assert el.props['ft_0']==6 and el.props['q_0']==2 and el.props['ft_1']==0
    stage['implementations']['LSP_EQ']['bands']=[]
    el=next(e for e in build(doc) if e.factory==IMPLEMENTATIONS['LSP_EQ'][1])
    assert all(el.props[f'ft_{i}']==0 for i in range(16))


@pytest.mark.parametrize('count',[1,4,8])
def test_multiband_order_bypass_and_independence(count):
    doc=profile();doc['stages']['multiband']['selected']='LSP_MULTIBAND'
    doc['stages']['wideband']['selected']='LSP_COMPRESSOR'
    p=doc['stages']['multiband']['implementations']['LSP_MULTIBAND']
    p['crossovers_hz']=[float(x) for x in [80,160,320,640,1280,2560,5120][:count-1]]
    p['bands'][0]['bypass']=True
    chain=build(doc)
    el=next(e for e in chain if e.factory==IMPLEMENTATIONS['LSP_MULTIBAND'][1])
    assert chain.index(el)>next(i for i,e in enumerate(chain) if e.factory==IMPLEMENTATIONS['LSP_COMPRESSOR'][1])
    assert el.props['ce_0'] is False and el.props['bm_0'] is False and el.props['bs_0'] is False
    for i in range(1,8): assert el.props[f'cbe_{i}']==(i<count)
    # Reducing active count never deletes the eight stored band configurations.
    assert len(AudioProfileFile.model_validate(doc).stages['multiband'].implementations['LSP_MULTIBAND']['bands'])==8


@pytest.mark.parametrize('crossovers',[[1000.,1000.],[5000.,1000.],[0.],[24000.]])
def test_invalid_crossovers(crossovers):
    doc=profile();doc['stages']['multiband']['implementations']['LSP_MULTIBAND']['crossovers_hz']=crossovers
    with pytest.raises(ValidationError,match='Crossovers'): AudioProfileFile.model_validate(doc)


def test_editor_description_matches_every_validated_setting(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst',lambda: SimpleNamespace(ElementFactory=SimpleNamespace(find=lambda f: True)))
    description=editor_description()
    assert [s['key'] for s in description['stages']]==list(STAGES)
    for key,(model,_,_) in IMPLEMENTATIONS.items():
        assert description['implementations'][key]['defaults']==model().model_dump()
        assert description['implementations'][key]['schema']==model.model_json_schema()
    assert description['implementations']['SINC']['available'] is False


def test_native_property_clamping_is_not_reported_as_success():
    from fmheadendd.errors import DependencyUnavailable
    class Clamp(Element):
        def get_property(self, key): return .1
    with pytest.raises(DependencyUnavailable, match='native readback'):
        dsp_stages(lambda factory, **p: Clamp(factory,p), AudioProfileFile.model_validate(profile()))
