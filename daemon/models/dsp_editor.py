"""Presentation metadata; profile values and DSP adapters are unchanged.

Ranges/units checked against Pi LSP 1.2.21 / GStreamer 1.26.2.
See docs/dsp-editor-controls.md for evidence and slider resolution policy.
"""
LSP_MANUAL = 'https://lsp-plug.in/?page=manuals&section='
DOCUMENTATION = {
    'VOLUME': 'https://gstreamer.freedesktop.org/documentation/volume/index.html',
    'TRIM': 'https://gstreamer.freedesktop.org/documentation/volume/index.html',
    'CHEBYSHEV': 'https://gstreamer.freedesktop.org/documentation/audiofx/audiocheblimit.html',
    **{key: LSP_MANUAL + section for key, section in {
        'LSP_MULTIBAND_EXPANDER': 'mb_expander_stereo', 'LSP_MULTIBAND_GATE': 'mb_gate_stereo',
        'LSP_AGC': 'autogain_stereo', 'LSP_EQ': 'para_equalizer_x16_stereo',
        'LSP_COMPRESSOR': 'compressor_stereo', 'LSP_MULTIBAND': 'mb_compressor_stereo',
        'LSP_LIMITER': 'limiter_stereo',
    }.items()},
}


def editor_schema(model):
    schema = model.model_json_schema()
    def annotate(node):
        for key, field in node.get('properties', {}).items():
            if field.get('hidden'):
                continue
            if key == 'poles':
                field['enum'] = list(range(2, 33, 2))
            elif field.get('type') == 'number':
                low = field.get('minimum', .1 if 'exclusiveMinimum' in field else -60)
                high = field.get('maximum', 23999)
                logarithmic = key.endswith(('_hz', '_ms')) or key in ('ratio', 'q')
                field['slider'] = dict(min=low, max=high,
                    scale='log' if logarithmic else 'linear',
                    offset=.1 if logarithmic and low == 0 else 0,
                    **({'significant_digits': 3} if logarithmic else {'step': 1 if key.endswith('_percent') else .1}))
            elif key == 'crossovers_hz':
                field['items'].update(minimum=10, maximum=20000, title='Crossover (Hz)',
                    slider=dict(min=10, max=20000, scale='log', offset=0, significant_digits=3))
        for definition in node.get('$defs', {}).values():
            annotate(definition)
    annotate(schema)
    return schema
