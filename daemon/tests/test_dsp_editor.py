from fmheadendd.models.dsp import IMPLEMENTATIONS, STAGES
from fmheadendd.models.dsp_editor import editor_schema, DOCUMENTATION


def test_editor_metadata_preserves_schema_limits_and_default_values():
    for key, (model, factory, _) in IMPLEMENTATIONS.items():
        schema = editor_schema(model)
        if key in ('FM_HEADEND_LIMITER', 'FM_HEADEND_CLIPPER'):
            assert DOCUMENTATION.get(key) is None  # No published plugin manual URL.
        else:
            assert DOCUMENTATION[key].startswith(('https://lsp-plug.in/?page=manuals&section=',
                                             'https://gstreamer.freedesktop.org/documentation/'))
        if key.startswith('LSP_'):
            assert DOCUMENTATION[key].endswith(factory.removeprefix('lsp-plug-in-plugins-lv2-').replace('-', '_'))
        original = model.model_json_schema()
        for field, value in original['properties'].items():
            for constraint in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'default'):
                assert schema['properties'][field].get(constraint) == value.get(constraint)
    assert STAGES['master_gain'][0] == 'Overall Gain'


def test_field_specific_resolution_and_fixed_mode_hidden():
    def fields(key): return editor_schema(IMPLEMENTATIONS[key][0])['properties']
    assert fields('CHEBYSHEV')['poles']['enum'] == list(range(2, 33, 2))
    assert fields('CHEBYSHEV')['cutoff_hz']['slider']['scale'] == 'log'
    assert fields('LSP_COMPRESSOR')['ratio']['slider']['scale'] == 'log'
    gate = editor_schema(IMPLEMENTATIONS['LSP_MULTIBAND_GATE'][0])['$defs']['MultibandGateBand']['properties']
    assert gate['attack_ms']['slider']['offset'] == .1  # supports real zero
    assert fields('LSP_AGC')['long_grow_ms']['slider']['significant_digits'] == 3
    assert fields('VOLUME')['gain_db']['slider']['step'] == .1
    assert fields('TRIM')['gain_db']['slider']['max'] == 0
    assert fields('LSP_MULTIBAND')['mode']['hidden'] is True
    eq = editor_schema(IMPLEMENTATIONS['LSP_EQ'][0])['$defs']['LspEqBand']['properties']
    assert eq['q']['slider']['scale'] == 'log' and eq['q']['slider']['offset'] == .1
    assert eq['gain_db']['slider']['min'] == -35



def test_fm_peak_controls_metadata_and_defaults():
    limiter = editor_schema(IMPLEMENTATIONS['FM_HEADEND_LIMITER'][0])['properties']
    clipper = editor_schema(IMPLEMENTATIONS['FM_HEADEND_CLIPPER'][0])['properties']
    assert limiter['true_peak_detection']['type'] == 'boolean'
    assert limiter['true_peak_detection']['default'] is False
    assert clipper['clipping_mode']['enum'] == ['hard', 'soft']
    assert clipper['clipping_mode']['default'] == 'hard'
    assert clipper['clipping_mode']['enum_labels'] == {'hard': 'Hard', 'soft': 'Soft'}
    knee = clipper['soft_knee_db']
    assert (knee['minimum'], knee['maximum'], knee['default']) == (0, 6, 2.)
    assert knee['visible_when'] == {'clipping_mode': 'soft'}
    assert knee['slider']['step'] == .1
