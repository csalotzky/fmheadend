"""ALSA-only normalization, before shared Source processing/fan-out."""

CAPTURE_CAPS = "audio/x-raw,rate=48000,channels=(int)[1,2]"
STEREO_CAPS = "audio/x-raw,channels=2,channel-mask=(bitmask)0x3"
MATRICES = {
    'stereo': ((1.0, 0.0), (0.0, 1.0)),
    'mono': ((0.5, 0.5), (0.5, 0.5)),
    'left': ((1.0, 0.0), (1.0, 0.0)),
    'right': ((0.0, 1.0), (0.0, 1.0)),
}


def link_alsa_capture(Gst, make, link, src, target, selection):
    capture = make('capsfilter', caps=Gst.Caps.from_string(CAPTURE_CAPS))
    # audioconvert's documented mono -> stereo conversion duplicates at unity.
    # Fix positioned FL/FR before the 2x2 selection matrix: raw mono capture
    # must not be forced to two channels or interpreted as "left only".
    normalize = make('audioconvert')
    stereo = make('capsfilter', caps=Gst.Caps.from_string(STEREO_CAPS))
    matrix = MATRICES[selection]
    value = '<' + ', '.join('<' + ', '.join(f'(float){v}' for v in row) + '>' for row in matrix) + '>'
    Gst.util_set_object_arg(target, 'mix-matrix', value)
    link(src, capture, normalize, stereo, target)
