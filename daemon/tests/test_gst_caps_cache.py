"""Focused regressions for negotiated caps caching; no hardware or network."""
from types import SimpleNamespace
import pytest
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline

class Caps:
    def __init__(self, **fields):
        self.fields = dict(format='S16LE', rate=48000, channels=2, layout='interleaved') | fields
        self.parses = self.strings = 0
    def get_structure(self, index):
        self.parses += 1
        return self
    def get_name(self): return 'audio/x-raw'
    def get_string(self, key): return self.fields[key]
    def get_value(self, key): return self.fields[key]
    def is_equal(self, other): return self.fields == other.fields
    def to_string(self):
        self.strings += 1
        return repr(self.fields)

def pipeline():
    p = GstPipeline(InputSourceConfig(name='test', type='HTTP', config={'uri': 'http://localhost/test'}))
    p.staging.clock = lambda:0
    p.Gst = SimpleNamespace(FlowReturn=SimpleNamespace(OK=1, ERROR=0))
    return p

def sample(p, caps):
    buf = SimpleNamespace(get_size=lambda:4, extract_dup=lambda start,size:b'1234')
    value = SimpleNamespace(get_caps=lambda:caps, get_buffer=lambda:buf)
    return p._sample(SimpleNamespace(emit=lambda signal:value))

def test_unchanged_caps_do_not_repeat_gi_field_marshalling():
    p = pipeline(); first = Caps(); equivalent = Caps()
    assert sample(p, first) == 1
    expected_text = p._caps
    assert sample(p, equivalent) == 1
    assert first.parses == first.strings == 1
    assert equivalent.parses == equivalent.strings == 0
    assert p._caps == expected_text
    assert p.staging.pop() == p.staging.pop() == b'1234'

@pytest.mark.parametrize('change', [{'rate':44100}, {'format':'F32LE'}, {'channels':1}, {'layout':'non-interleaved'}])
def test_changed_caps_still_fail_closed(change):
    p = pipeline(); assert sample(p, Caps()) == 1
    p.staging.clear()
    assert sample(p, Caps(**change)) == 0
    assert p.staging.pop() is None
    assert 'Unexpected appsink caps' in p._callback_error

def test_missing_caps_fails_closed():
    p = pipeline()
    assert sample(p, None) == 0
    assert p._callback_error == 'Missing appsink caps'
    assert p.staging.pop() is None

def test_attempt_resets_cache_before_build():
    p = pipeline(); sample(p, Caps())
    p._stop.set()
    p.Gst.State = SimpleNamespace(PLAYING=1)
    p.Gst.StateChangeReturn = SimpleNamespace(FAILURE=0)
    def build():
        assert p._validated_output_caps is None
        assert p._validated_output_text is None
        p._pipeline = SimpleNamespace(set_state=lambda state:1)
    p._build = build
    p._attempt()

def test_worker_reuses_input_metadata_and_observes_new_caps():
    p = pipeline()
    first, equal, changed = Caps(), Caps(), Caps(rate=44100, channels=1)
    p.Gst.State = SimpleNamespace(PLAYING=1)
    p.Gst.StateChangeReturn = SimpleNamespace(FAILURE=0)
    p._pipeline = SimpleNamespace(set_state=lambda state:1)
    p._bus = SimpleNamespace(timed_pop=lambda timeout:None)
    def build():
        p._input_caps = first
        p.staging.put(b'1234')
    p._build = build
    seen = []
    def wait(timeout):
        seen.append(p.snapshot())
        if len(seen) < 3:
            p._input_caps = equal if len(seen) == 1 else changed
            p.staging.put(b'1234')
        else:
            p._stop.set()
    p._audio_ready.wait = wait
    p._attempt()
    assert [s['input_sample_rate'] for s in seen] == [48000, 48000, 44100]
    assert [s['input_channels'] for s in seen] == [2, 2, 1]
    assert (first.parses, equal.parses, changed.parses) == (1, 0, 1)


def test_idle_ceiling_does_not_extend_audio_stall_deadline(monkeypatch):
    from fmheadendd.sources import gst_pipeline
    p = pipeline(); p.timeout_s = 1
    now = [100.0]
    monkeypatch.setattr(gst_pipeline.time, 'monotonic', lambda:now[0])
    p.Gst.State = SimpleNamespace(PLAYING=1)
    p.Gst.StateChangeReturn = SimpleNamespace(FAILURE=0)
    p._pipeline = SimpleNamespace(set_state=lambda state:1)
    p._build = lambda:None
    p._bus = SimpleNamespace(timed_pop=lambda timeout:None)
    def pop():
        now[0] = 100.99
        return None
    p.staging.pop = pop
    waits = []
    def wait(timeout):
        waits.append(timeout)
        p._stop.set()
    p._audio_ready.wait = wait
    p._attempt()
    assert waits == pytest.approx([.01])

@pytest.mark.gstreamer
def test_native_sample_caps_reference_survives_sample_release():
    import gc
    from fmheadendd.errors import DependencyUnavailable
    from fmheadendd.sources.gst_pipeline import load_gst
    try:
        Gst = load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))
    p = pipeline(); p.Gst = Gst
    pcm_caps = 'audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved'
    def deliver(text):
        caps = Gst.Caps.from_string(text)
        buffer = Gst.Buffer.new_allocate(None, 4, None)
        buffer.fill(0, b'1234')
        native_sample = Gst.Sample.new(buffer, caps, None, None)
        return p._sample(SimpleNamespace(emit=lambda signal:native_sample))
    assert deliver(pcm_caps) == Gst.FlowReturn.OK
    cached = p._validated_output_caps
    gc.collect()
    assert deliver(pcm_caps) == Gst.FlowReturn.OK
    assert p._validated_output_caps is cached
    assert p.staging.pop() == p.staging.pop() == b'1234'
    assert deliver(pcm_caps.replace('rate=48000', 'rate=44100')) == Gst.FlowReturn.ERROR
    assert p.staging.pop() is None
