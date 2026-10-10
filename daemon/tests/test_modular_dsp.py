import pytest
from pydantic import ValidationError
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.dsp import STAGES, IMPLEMENTATIONS, neutral_stages
from fmheadendd.sources.dsp import dsp_stages, editor_description


def profile():
    return dict(schema_version=1, description="Test profile", name='Neutral', stages=neutral_stages())


class Element:
    def __init__(self, factory, props):
        self.factory, self.props = factory, props


def build(document):
    return dsp_stages(lambda factory, **p: Element(factory, p), AudioProfileFile.model_validate(document))


def test_fixed_order_and_gain_domains():
    doc = profile()
    for key, gain in [('input_gain', -3), ('master_gain', 6), ('output_trim', -6)]:
        impl = doc['stages'][key]['selected']
        doc['stages'][key]['implementations'][impl]['gain_db'] = gain
    doc['stages']['limiter']['selected'] = 'LSP_LIMITER'
    chain = build(doc)
    assert list(AudioProfileFile.model_validate(doc).stages) == list(STAGES)
    assert [x.factory for x in chain] == ['volume', 'audiocheblimit',
        'volume', IMPLEMENTATIONS['LSP_LIMITER'][1], 'audiocheblimit', 'volume']
    assert [e.props['volume'] for e in chain if e.factory == 'volume'] == pytest.approx([10**(-3/20),10**(6/20),10**(-6/20)])



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
    ('lpf','CHEBYSHEV','cutoff_hz',24000),
    ('wideband','LSP_COMPRESSOR','threshold_dbfs','-12'), ('input_gain','VOLUME','gain_db',float('nan')),
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



@pytest.mark.parametrize('stage,impl', [('gate','LSP_MULTIBAND_GATE'),('gate','LSP_MULTIBAND_EXPANDER'),
    ('agc','LSP_AGC'),('wideband','LSP_COMPRESSOR'),('limiter','LSP_LIMITER')])
def test_lsp_selected_enabled_and_exact_units(stage,impl):
    doc=profile(); doc['stages'][stage]['selected']=impl
    el=next(e for e in build(doc) if e.factory==IMPLEMENTATIONS[impl][1])
    assert el.props['enabled'] is True
    if impl=='LSP_MULTIBAND_GATE': assert el.props['gr_0']==pytest.approx(10**(-24/20))
    if impl=='LSP_MULTIBAND_EXPANDER': assert el.props['em_0']==0
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
        from fmheadendd.models.dsp_editor import editor_schema
        assert description['implementations'][key]['schema']==editor_schema(model)
    assert set(description['implementations']) == set(IMPLEMENTATIONS)
    assert description['template']['schema_version'] == 1


def test_native_property_clamping_is_not_reported_as_success():
    from fmheadendd.errors import DependencyUnavailable
    class Clamp(Element):
        def get_property(self, key): return .1
    with pytest.raises(DependencyUnavailable, match='native readback'):
        dsp_stages(lambda factory, **p: Clamp(factory,p), AudioProfileFile.model_validate(profile()))


@pytest.mark.parametrize('stage,removed', [('gate','DYNAMIC'), ('eq','EQ'),
    ('eq','LEGACY_EQ'), ('wideband','DYNAMIC'), ('limiter','RGLIMITER'), ('lpf','SINC')])
def test_deleted_identifiers_rejected_even_when_inactive(stage, removed):
    assert removed not in IMPLEMENTATIONS
    assert removed not in STAGES[stage][1]
    doc = profile()
    doc['stages'][stage]['selected'] = removed
    with pytest.raises(ValidationError, match='unsupported implementation'):
        AudioProfileFile.model_validate(doc)
    doc['stages'][stage]['selected'] = 'OFF'
    doc['stages'][stage]['implementations'][removed] = {}
    with pytest.raises(ValidationError, match='unsupported saved implementation'):
        AudioProfileFile.model_validate(doc)


@pytest.mark.parametrize('change', [{'schema_version': 2}, {'compatibility': None},
    {'stages': {'gain': {}, 'processor': {}}}])
def test_only_current_first_release_shape_is_supported(change):
    with pytest.raises(ValidationError):
        AudioProfileFile.model_validate(profile() | change)


def test_all_surviving_settings_roundtrip_without_retuning():
    doc = profile()
    doc['stages']['limiter']['implementations']['LSP_LIMITER'].update(threshold_dbfs=-3., alr_release_ms=72.)
    doc['stages']['eq']['implementations']['LSP_EQ']['bands'][0].update(q=.7, gain_db=.75)
    doc['stages']['agc']['implementations']['LSP_AGC'].update(long_grow_ms=10000., maximum_gain_db=6.)
    doc['stages']['master_gain']['implementations']['VOLUME']['gain_db'] = 18.
    for key, (_, choices) in STAGES.items():
        if 'OFF' in choices: doc['stages'][key]['selected'] = 'OFF'
    assert AudioProfileFile.model_validate(doc).model_dump() == doc


def test_lpf_default_is_specific():
    parsed = AudioProfileFile.model_validate(profile())
    assert parsed.stages['hpf'].implementations['CHEBYSHEV']['poles'] == 4
    assert parsed.stages['lpf'].implementations['CHEBYSHEV'] == dict(cutoff_hz=16000, poles=14, ripple_db=0, type=1)


@pytest.mark.parametrize('selected', ['OFF', 'LSP_LIMITER', 'FM_HEADEND_LIMITER'])
@pytest.mark.parametrize('weighting', [0, 50, 75])
def test_exclusive_limiter_roundtrip_and_order(selected, weighting):
    doc = profile()
    stage = doc['stages']['limiter']
    stage['selected'] = selected
    settings = stage['implementations']['FM_HEADEND_LIMITER']
    settings.update(weighting_us=weighting, threshold_dbfs=-11., lookahead_ms=6.,
                    attack_ms=.5, release_ms=42., stereo_link_percent=80., safety_margin_db=3.)
    parsed = AudioProfileFile.model_validate_json(AudioProfileFile.model_validate(doc).model_dump_json())
    assert parsed.model_dump() == doc
    chain = build(doc)
    expected = ['volume', 'audiocheblimit', 'volume']
    if selected != 'OFF': expected.append(IMPLEMENTATIONS[selected][1])
    expected += ['audiocheblimit', 'volume']
    assert [e.factory for e in chain] == expected
    if selected == 'FM_HEADEND_LIMITER':
        assert chain[3].props == settings
    elif selected == 'LSP_LIMITER':
        assert chain[3].props == dict(enabled=True, th=1., lk=5., at=5., rt=5.,
            slink=100., ovs=0, mode=0, boost=False, alr=False, alr_at=5., alr_rt=50.)


@pytest.mark.parametrize('selected', ['OFF', 'LSP_LIMITER'])
def test_existing_profiles_need_no_new_implementation_settings(selected):
    doc = profile()
    doc['stages']['limiter']['selected'] = selected
    del doc['stages']['limiter']['implementations']['FM_HEADEND_LIMITER']
    assert AudioProfileFile.model_validate(doc).model_dump() == doc


@pytest.mark.parametrize('key,value', [('weighting_us', 25), ('threshold_dbfs', -61.),
    ('lookahead_ms', 21.), ('attack_ms', -1.), ('release_ms', 0.),
    ('stereo_link_percent', 101.), ('safety_margin_db', 13.)])
def test_invalid_fm_limiter_settings(key, value):
    doc = profile()
    doc['stages']['limiter']['implementations']['FM_HEADEND_LIMITER'][key] = value
    with pytest.raises(ValidationError): AudioProfileFile.model_validate(doc)


def test_fm_editor_defaults_and_choices():
    from fmheadendd.models.dsp import FmHeadendLimiter, LspLimiter
    from fmheadendd.models.dsp_editor import editor_schema
    assert FmHeadendLimiter().model_dump() == dict(threshold_dbfs=-9, lookahead_ms=5,
        attack_ms=.25, release_ms=20, stereo_link_percent=100, weighting_us=50,
        safety_margin_db=2.5, bypass=False)
    assert 'fm_limiter_weighting' not in LspLimiter.model_fields
    field = editor_schema(FmHeadendLimiter)['properties']['weighting_us']
    assert field['enum'] == [0, 50, 75]
    assert field['enum_labels'] == {'0': 'Off', '50': '50 \u00b5s', '75': '75 \u00b5s'}


def test_selected_fm_limiter_requires_plugin(monkeypatch):
    from types import SimpleNamespace
    from fmheadendd.sources.gst_pipeline import GstPipeline
    from fmheadendd.errors import DependencyUnavailable
    class Native:
        def __init__(self): self.props = {}
        def set_property(self, key, value): self.props[key] = value
        def get_property(self, key): return self.props[key]
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst', lambda: SimpleNamespace(
        ElementFactory=SimpleNamespace(make=lambda factory, name: None if factory == 'fmheadendlimiter' else Native())))
    doc = profile()
    for selected in ('OFF', 'LSP_LIMITER'):
        doc['stages']['limiter']['selected'] = selected
        GstPipeline.validate_profile(AudioProfileFile.model_validate(doc))
    doc['stages']['limiter']['selected'] = 'FM_HEADEND_LIMITER'
    with pytest.raises(DependencyUnavailable, match='Missing GStreamer element: fmheadendlimiter'):
        GstPipeline.validate_profile(AudioProfileFile.model_validate(doc))


@pytest.mark.parametrize('selected', ['OFF', 'FM_HEADEND_CLIPPER'])
@pytest.mark.parametrize('weighting', [0, 50, 75])
@pytest.mark.parametrize('limiter', ['LSP_LIMITER', 'FM_HEADEND_LIMITER'])
def test_clipper_independent_stage_order_properties_and_roundtrip(selected, weighting, limiter):
    doc = profile()
    assert doc['stages']['clipper']['selected'] == 'OFF'
    doc['stages']['limiter']['selected'] = limiter
    doc['stages']['clipper']['selected'] = selected
    settings = doc['stages']['clipper']['implementations']['FM_HEADEND_CLIPPER']
    settings.update(threshold_dbfs=-12., weighting_us=weighting, bypass=False)
    parsed = AudioProfileFile.model_validate_json(AudioProfileFile.model_validate(doc).model_dump_json())
    assert parsed.model_dump() == doc
    chain = build(doc)
    expected = ['volume', 'audiocheblimit', 'volume', IMPLEMENTATIONS[limiter][1], 'audiocheblimit']
    if selected != 'OFF': expected.append('fmheadendclipper')
    assert [e.factory for e in chain] == expected + ['volume']
    if selected != 'OFF': assert chain[-2].props == settings
    doc['stages']['clipper']['selected'] = 'OFF'
    original = build(doc)
    assert [(e.factory,e.props) for e in chain if e.factory != 'fmheadendclipper'] == [(e.factory,e.props) for e in original]


@pytest.mark.parametrize('key,value', [('threshold_dbfs', -61.), ('threshold_dbfs', 1.),
    ('weighting_us', 25), ('bypass', 'false'), ('clip_count', 0), ('max_clipping_db', 0.)])
def test_clipper_rejects_invalid_or_runtime_fields(key, value):
    doc = profile()
    doc['stages']['clipper']['implementations']['FM_HEADEND_CLIPPER'][key] = value
    with pytest.raises(ValidationError): AudioProfileFile.model_validate(doc)


def test_missing_clipper_is_explicit_only_when_selected(monkeypatch):
    from types import SimpleNamespace
    from fmheadendd.sources.gst_pipeline import GstPipeline
    from fmheadendd.errors import DependencyUnavailable
    class Native:
        def __init__(self): self.props = {}
        def set_property(self,key,value): self.props[key] = value
        def get_property(self,key): return self.props[key]
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst', lambda: SimpleNamespace(
        ElementFactory=SimpleNamespace(make=lambda factory,name: None if factory=='fmheadendclipper' else Native())))
    doc = profile()
    GstPipeline.validate_profile(AudioProfileFile.model_validate(doc))
    doc['stages']['clipper']['selected'] = 'FM_HEADEND_CLIPPER'
    with pytest.raises(DependencyUnavailable, match='Missing GStreamer element: fmheadendclipper'):
        GstPipeline.validate_profile(AudioProfileFile.model_validate(doc))
