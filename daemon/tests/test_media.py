import threading
import time
from types import SimpleNamespace

import pytest
from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig, ModulatorPatch, SourceTestResult
from fmheadendd.sources.gst_pipeline import GstPipeline, RetryBackoff, rtp_parameters, caps_structure, make_rtp_caps
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.output import PcmQueue, PcmOutput, PCM_CAPS
from fmheadendd.modulators.manager import ModulatorManager
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.mux.client import MockMuxClient
from fmheadendd.config import Settings


class ContextOnlyStructure:
    """Model the public context-only API, including borrowed-value lifetime."""
    def __init__(self, values, name="audio/x-raw"):
        self.values = values
        self.name = name
        self.active = False
        self.entries = self.exits = 0

    def __enter__(self):
        assert not self.active
        self.active = True
        self.entries += 1
        return SimpleNamespace(get_name=self.read_name, get_value=self.read, get_string=self.read)

    def __exit__(self, *exc):
        self.active = False
        self.exits += 1

    def read_name(self):
        assert self.active, "Borrowed structure used outside its context"
        return self.name

    def read(self, key):
        assert self.active, "Borrowed structure used outside its context"
        return self.values.get(key)


def source(kind="HTTP", config=None):
    return InputSourceConfig(name="test", type=kind, config=config if config is not None else {"uri": "http://127.0.0.1/live"})


def test_queue_is_bounded_drops_oldest_and_expires():
    now = [0.0]
    queue = PcmQueue(max_bytes=8, clock=lambda: now[0])
    assert queue.put(b"aaaa")
    assert queue.put(b"bbbb")
    assert queue.put(b"cccc")
    assert queue.buffered_bytes == 8
    assert queue.pop() == b"bbbb"
    now[0] = 1
    assert queue.pop() is None
    assert queue.buffered_bytes == 0 and queue.dropped == 2
    assert not queue.put(b"misaligned")
    assert not queue.put(b"x" * 12)


def test_queue_producer_does_not_wait_on_lock():
    queue = PcmQueue()
    queue._lock.acquire()
    try:
        assert queue.put(b"1234") is False
    finally:
        queue._lock.release()


def test_fanout_slow_consumer_independent_and_clear():
    output = PcmOutput()
    output.attach(0)
    output.attach(1)
    for _ in range(30):
        output.publish(b"x" * 1920)
        assert output.read(0) == b"x" * 1920
    assert output._queues[1].buffered_bytes <= 19200
    output.clear()
    assert output.read(0) is None and output.read(1) is None
    output.detach(1)
    output.publish(b"1234")
    assert output.read(1) is None


def test_backoff_schedule_and_reset():
    backoff = RetryBackoff()
    assert [backoff.next_delay() for _ in range(7)] == [1, 2, 5, 10, 30, 30, 30]
    backoff.reset()
    assert backoff.next_delay() == 1


class FakeRealPipeline:
    mocked = False
    instances = []

    def __init__(self, source, **kwargs):
        self.instances.append(self)
        self.source = source
        self.output = PcmOutput()
        self.kwargs = kwargs
        self.starts = self.stops = 0
        self.info = {"state": "RUNNING", "codec": "OPUS", "input_sample_rate": 44100,
                     "input_channels": 1, "stream_title": "Artist - Title", "output_caps": PCM_CAPS}

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def snapshot(self):
        return self.info.copy()

    def probe_result(self):
        return SourceTestResult(ok=self.info["state"] == "RUNNING", mocked=False, output_caps=PCM_CAPS)


@pytest.fixture
def services(tmp_path):
    FakeRealPipeline.instances = []
    db = Database(tmp_path / "media.db")
    sources = SourceManager(db, FakeRealPipeline)
    rp = MockRp2354Client()
    manager = ModulatorManager(db, sources, rp, MockMuxClient(), Settings())
    try:
        yield db, sources, manager, rp
    finally:
        sources.close()
        db.close()


def test_real_abstraction_shares_one_pipeline_and_metadata(services):
    db, sources, manager, rp = services
    saved = db.save("sources", source())
    for tx in (0, 1):
        manager.update(tx, ModulatorPatch(input_source_id=saved.id, frequency_khz=98100))
        manager.start(tx)
    assert len(FakeRealPipeline.instances) == 1
    pipeline = sources.pipelines[saved.id]
    assert pipeline.starts == 1
    assert sources.status(saved.id).stream_title == "Artist - Title"
    pipeline.output.publish(b"1234")
    assert pipeline.output.read(0) == pipeline.output.read(1) == b"1234"
    assert sources.test(saved).ok
    assert len(FakeRealPipeline.instances) == 1
    pipeline.info["state"] = "CONNECTING"
    assert sources.status(saved.id).state == "CONNECTING"
    assert rp.enabled[0] and rp.enabled[1]
    manager.stop(0)
    assert pipeline.stops == 0
    manager.stop(1)
    assert pipeline.stops == 1 and not sources.pipelines


def test_analog_never_constructs_any_pipeline(services, monkeypatch):
    db, sources, manager, rp = services
    def forbidden(*args, **kwargs):
        pytest.fail("ANALOG constructed a GStreamer pipeline")
    forbidden.mocked = False
    sources.pipeline_factory = forbidden
    saved = db.save("sources", source("ANALOG", {}))
    manager.update(0, ModulatorPatch(input_source_id=saved.id, frequency_khz=98100))
    manager.start(0)
    assert rp.configs[0].audio_mode == "ANALOG"
    assert sources.test(saved).ok
    assert sources.test(source("ANALOG", {})).ok
    manager.stop(0)
    assert not sources.pipelines


def test_probe_cleanup_failure_and_no_config_mutation(services):
    db, sources, manager, rp = services
    class Broken(FakeRealPipeline):
        def start(self):
            raise DependencyUnavailable("Connection refused")
    sources.pipeline_factory = Broken
    before = db.list("modulators")
    result = sources.test(source())
    assert not result.ok and not result.mocked and "Connection refused" in result.message
    assert FakeRealPipeline.instances[-1].stops == 1
    assert db.list("sources") == [] and db.list("modulators") == before


def test_missing_gst_is_failure_not_mock_success(monkeypatch):
    from fmheadendd.sources import gst_pipeline
    def unavailable():
        raise DependencyUnavailable("Missing GI")
    monkeypatch.setattr(gst_pipeline, "load_gst", unavailable)
    pipeline = GstPipeline(source(), timeout_s=0.2)
    with pytest.raises(DependencyUnavailable, match="Missing GI"):
        pipeline.start()
    assert pipeline.snapshot()["state"] == "ERROR"
    assert not pipeline._thread.is_alive()


def test_supervisor_reconnect_and_stop_during_backoff(monkeypatch):
    from fmheadendd.sources import gst_pipeline
    monkeypatch.setattr(gst_pipeline, "load_gst", lambda: SimpleNamespace())
    retry_entered = threading.Event()
    class Drop(GstPipeline):
        def _attempt(self):
            self._update(state="RUNNING")
            self._ready.set()
            retry_entered.set()
            raise DependencyUnavailable("Connection lost")
    pipeline = Drop(source())
    pipeline.start()
    assert retry_entered.wait(1)
    deadline = time.monotonic() + 1
    while pipeline.snapshot()["state"] != "CONNECTING" and time.monotonic() < deadline:
        time.sleep(0.001)
    assert pipeline.snapshot()["state"] == "CONNECTING"
    assert "Connection lost" in pipeline.snapshot()["last_error"]
    pipeline.stop()
    assert not pipeline._thread.is_alive()
    assert pipeline.snapshot()["reconnect_count"] == 0


def test_supervisor_recovers_and_counts_retry(monkeypatch):
    from fmheadendd.sources import gst_pipeline
    monkeypatch.setattr(gst_pipeline, "load_gst", lambda: SimpleNamespace())
    monkeypatch.setattr(RetryBackoff, "next_delay", lambda self: 0.01)
    recovered = threading.Event()
    class Recover(GstPipeline):
        attempts = 0
        def _attempt(self):
            self.attempts += 1
            self._ready.set()
            if self.attempts == 1:
                raise DependencyUnavailable("lost")
            self._update(state="RUNNING", last_error=None)
            recovered.set()
            self._stop.wait(2)
    pipeline = Recover(source())
    pipeline.start()
    assert recovered.wait(1)
    assert pipeline.snapshot()["reconnect_count"] == 1
    assert pipeline.snapshot()["state"] == "RUNNING"
    pipeline.stop()


def test_rtp_explicit_parameters():
    assert rtp_parameters({"codec": "OPUS"})[1]["clock-rate"] == 48000
    assert rtp_parameters({"codec": "MP3"})[0] == "rtpmpadepay"
    with pytest.raises(DependencyUnavailable, match="clock_rate"):
        rtp_parameters({"codec": "PCM"})
    assert rtp_parameters({"codec": "PCM", "clock_rate": 44100, "channels": 2})[1]["encoding-name"] == "L16"
    with pytest.raises(DependencyUnavailable, match="codec_data"):
        rtp_parameters({"codec": "AAC", "clock_rate": 48000, "channels": 2})


@pytest.mark.parametrize("wrapped", [False, True])
def test_apps_sink_callback_only_enqueues_and_validates_caps(wrapped):
    pipeline = GstPipeline(source())
    pipeline.Gst = SimpleNamespace(FlowReturn=SimpleNamespace(OK="OK", ERROR="ERROR"))
    values = {"rate": 48000, "channels": 2, "format": "S16LE", "layout": "interleaved"}
    structure = SimpleNamespace(get_name=lambda: "audio/x-raw", get_string=values.get, get_value=values.get)
    if wrapped:
        structure = ContextOnlyStructure(values)
    caps = SimpleNamespace(get_structure=lambda i: structure, to_string=lambda: PCM_CAPS)
    buffer = SimpleNamespace(get_size=lambda: 4, extract_dup=lambda a, b: b"1234")
    sample = SimpleNamespace(get_caps=lambda: caps, get_buffer=lambda: buffer)
    sink = SimpleNamespace(emit=lambda event: sample)
    def forbidden(data):
        pytest.fail("Callback performed downstream work")
    pipeline.output.publish = forbidden
    assert pipeline._sample(sink) == "OK"
    assert pipeline.staging.pop() == b"1234"
    values["rate"] = 44100
    assert pipeline._sample(sink) == "ERROR"
    assert "Unexpected appsink caps" in pipeline._callback_error
    if wrapped:
        assert structure.entries == structure.exits == 2 and not structure.active


def test_stop_failure_keeps_source_ownership(services):
    db, sources, manager, rp = services
    saved = db.save("sources", source())
    manager.update(0, ModulatorPatch(input_source_id=saved.id, frequency_khz=98100))
    manager.start(0)
    pipeline = sources.pipelines[saved.id]
    original = pipeline.stop
    def failed_stop():
        raise DependencyUnavailable("teardown timed out")
    pipeline.stop = failed_stop
    from fmheadendd.errors import DomainError
    with pytest.raises(DomainError):
        manager.stop(0)
    assert sources.pipelines[saved.id] is pipeline
    assert sources.consumers[saved.id] == {0}
    assert manager.active_sources[0] == saved.id
    pipeline.stop = original
    manager.stop(0)
    assert not sources.pipelines


@pytest.mark.parametrize("name,linked", [("audio/x-raw", True), ("video/x-raw", False)])
def test_decoded_pad_uses_context_only_structure(name, linked):
    pipeline = GstPipeline(source())
    wrapper = ContextOnlyStructure({}, name)
    copied = object()
    def copy_caps():
        assert not wrapper.active
        return copied
    caps = SimpleNamespace(get_structure=lambda index: wrapper, copy=copy_caps)
    links = []
    sink_pad = SimpleNamespace(is_linked=lambda: False)
    pad = SimpleNamespace(get_current_caps=lambda: caps, link=lambda target: links.append(target) or "OK")
    pipeline._convert = SimpleNamespace(get_static_pad=lambda name: sink_pad)
    pipeline.Gst = SimpleNamespace(PadLinkReturn=SimpleNamespace(OK="OK"))
    pipeline._decoded_pad(None, pad)
    assert pipeline._callback_error is None
    assert bool(links) == linked
    assert (pipeline._input_caps is copied) == linked
    assert wrapper.entries == wrapper.exits == 1


def test_input_format_reporting_scopes_structure_access():
    pipeline = GstPipeline(source())
    wrapper = ContextOnlyStructure({"rate": 44100, "channels": 1})
    caps = SimpleNamespace(get_structure=lambda index: wrapper)
    pipeline.Gst = SimpleNamespace(MSECOND=1, State=SimpleNamespace(PLAYING="PLAYING"),
                                   StateChangeReturn=SimpleNamespace(FAILURE="FAILURE"))
    pipeline._pipeline = SimpleNamespace(set_state=lambda state: "SUCCESS")
    pipeline._convert = SimpleNamespace(get_static_pad=lambda name: SimpleNamespace(get_current_caps=lambda: caps))
    pipeline._build = lambda: None
    def receive(unused):
        pipeline.staging.put(b"1234")
        pipeline._caps = PCM_CAPS
        pipeline._last_sample = time.monotonic()
        pipeline._stop.set()  # Finish after this worker iteration.
        return None
    pipeline._bus = SimpleNamespace(timed_pop=receive)
    pipeline._attempt()
    assert pipeline.snapshot()["input_sample_rate"] == 44100
    assert pipeline.snapshot()["input_channels"] == 1
    assert pipeline.snapshot()["state"] == "RUNNING"
    assert wrapper.entries == wrapper.exits == 1


def test_structure_context_is_exited_on_exception():
    wrapper = ContextOnlyStructure({})
    caps = SimpleNamespace(get_structure=lambda index: wrapper)
    with pytest.raises(RuntimeError):
        with caps_structure(caps):
            raise RuntimeError("read failed")
    assert wrapper.entries == wrapper.exits == 1 and not wrapper.active


@pytest.mark.parametrize("codec,extra", [
    ("OPUS", {}), ("MP3", {}),
    ("PCM", {"clock_rate": 44100, "channels": 1}),
    ("AAC", {"clock_rate": 48000, "channels": 2, "codec_data": "1190"}),
])
def test_rtp_caps_populates_owned_structure_before_append(codec, extra):
    class OwnedStructure:
        def __init__(self, name):
            self.name = name
            self.fields = {}
            self.transferred = False
        def set_value(self, key, value):
            assert not self.transferred
            self.fields[key] = value
    class Caps:
        def append_structure(self, structure):
            assert isinstance(structure, OwnedStructure)
            structure.transferred = True
            self.structure = structure
    gst = SimpleNamespace(Structure=SimpleNamespace(new_empty=OwnedStructure), Caps=SimpleNamespace(new_empty=Caps))
    config = {"codec": codec, "payload_type": 96, **extra}
    _, fields = rtp_parameters(config)
    caps = make_rtp_caps(gst, config, fields)
    assert caps.structure.name == "application/x-rtp"
    assert caps.structure.fields == {"media": "audio", "payload": 96, **fields}
    assert caps.structure.transferred
