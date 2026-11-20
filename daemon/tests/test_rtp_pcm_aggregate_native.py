"""Native content, age and lifecycle tests for the RTP L16 aggregator."""
from array import array
import time

import pytest

from fmheadendd.sources.gst_pipeline import load_gst
from fmheadendd.sources.rtp_pcm_aggregate import register_rtp_pcm_aggregate


pytestmark = pytest.mark.gstreamer
RATE = 48000
BLOCK = 1920


def pts(frame):
    return frame * 10**9 // RATE


def pcm(frame, count):
    return array('H', (value for index in range(frame, frame + count)
                      for value in (index, 65535 - index))).tobytes()


@pytest.fixture
def aggregate():
    pytest.importorskip('gi')
    gst = load_gst()
    if not register_rtp_pcm_aggregate():
        pytest.skip('Native RTP PCM aggregator is unavailable')
    pipeline = gst.parse_launch('appsrc name=input is-live=true format=time '
        'caps="audio/x-raw,format=S16BE,layout=interleaved,rate=48000,channels=2" '
        '! fmrtppcmaggregate name=aggregate ! appsink name=output sync=false')
    source, element, sink = (pipeline.get_by_name(name) for name in ('input', 'aggregate', 'output'))
    assert pipeline.set_state(gst.State.PLAYING) != gst.StateChangeReturn.FAILURE
    try:
        yield gst, pipeline, source, element, sink
    finally:
        assert pipeline.set_state(gst.State.NULL) != gst.StateChangeReturn.FAILURE


def send(gst, source, start, count, *, discont=False):
    data = pcm(start, count)
    buffer = gst.Buffer.new_allocate(None, len(data), None)
    assert buffer.fill(0, data) == len(data)
    buffer.pts = pts(start)
    if discont:
        buffer.set_flags(gst.BufferFlags.DISCONT)
    assert source.emit('push-buffer', buffer) == gst.FlowReturn.OK


def receive(gst, sink, start, count, *, discont):
    sample = sink.emit('try-pull-sample', gst.SECOND)
    assert sample is not None
    buffer = sample.get_buffer()
    assert buffer.extract_dup(0, buffer.get_size()) == pcm(start, count)
    assert abs(buffer.pts - pts(start)) <= 1
    assert buffer.duration == pts(count)
    assert buffer.has_flags(gst.BufferFlags.DISCONT) == discont


def test_block_size_and_fresh_discontinuity(aggregate):
    gst, _, source, element, sink = aggregate
    send(gst, source, 0, 347)
    send(gst, source, 347, 1700)
    receive(gst, sink, 0, BLOCK, discont=True)
    send(gst, source, 2588, BLOCK, discont=True)
    receive(gst, sink, BLOCK, 127, discont=False)
    receive(gst, sink, 2588, BLOCK, discont=True)
    assert element.get_property('expired-frames') == 0


def test_wall_clock_expiry_with_continuous_pts(aggregate):
    gst, _, source, element, sink = aggregate
    send(gst, source, 0, 347)
    assert sink.emit('try-pull-sample', 240 * gst.MSECOND) is None
    send(gst, source, 347, BLOCK)
    receive(gst, sink, 347, BLOCK, discont=True)
    assert element.get_property('expired-frames') == 347


def test_expiry_preserves_newer_pending_frames(aggregate):
    gst, _, source, element, sink = aggregate
    send(gst, source, 0, 347)
    assert sink.emit('try-pull-sample', 120 * gst.MSECOND) is None
    send(gst, source, 347, 347)
    send(gst, source, 694, BLOCK - 347)
    receive(gst, sink, 347, BLOCK, discont=True)
    assert element.get_property('expired-frames') == 347


def test_age_starts_at_upstream_output(aggregate):
    gst, _, source, element, sink = aggregate

    def stall_before_chain(_pad, _info):
        time.sleep(.12)
        return gst.PadProbeReturn.REMOVE

    element.get_static_pad('sink').add_probe(gst.PadProbeType.BUFFER, stall_before_chain)
    send(gst, source, 0, BLOCK)
    assert sink.emit('try-pull-sample', 240 * gst.MSECOND) is None
    send(gst, source, BLOCK, BLOCK)
    receive(gst, sink, BLOCK, BLOCK, discont=True)
    assert element.get_property('expired-frames') == BLOCK


def test_retained_source_pad_outlives_element(aggregate):
    gst, _, _, _, _ = aggregate
    element = gst.ElementFactory.make('fmrtppcmaggregate')
    pad = element.get_static_pad('src')
    disposed = []
    element.weak_ref(lambda: disposed.append(True))
    del element
    assert disposed == [True]
    # GStreamer requires a parent for activation. Reusing this retained pad
    # must not call a probe that still points at the destroyed aggregator.
    replacement = gst.Bin.new(None)
    assert replacement.add_pad(pad)
    assert pad.set_active(True)
    pad.push_event(gst.Event.new_stream_start('retained-pad'))
    segment = gst.Segment.new()
    segment.init(gst.Format.TIME)
    pad.push_event(gst.Event.new_segment(segment))
    try:
        assert pad.push(gst.Buffer.new_allocate(None, 4, None)) == gst.FlowReturn.NOT_LINKED
    finally:
        assert pad.set_active(False)


@pytest.mark.parametrize('pause, counter', [(0, 'reset-frames'), (.24, 'expired-frames')])
def test_stop_discards_incomplete_block(aggregate, pause, counter):
    gst, pipeline, source, element, sink = aggregate
    send(gst, source, 0, BLOCK + 347)
    receive(gst, sink, 0, BLOCK, discont=True)
    # Receiving the first block synchronizes with the chain call that kept the tail.
    time.sleep(pause)
    assert pipeline.set_state(gst.State.NULL) != gst.StateChangeReturn.FAILURE
    assert element.get_property(counter) == 347
