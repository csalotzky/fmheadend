"""GStreamer ingest. GI is loaded only when a digital source is opened."""
from contextlib import nullcontext
from dataclasses import dataclass
import logging
import math
import threading
import time

from ..errors import DependencyUnavailable
from ..models.configuration import SourceType, SourceTestResult
from .output import PCM_CAPS, PcmQueue, PcmOutput

log = logging.getLogger(__name__)


def caps_structure(caps, index=0):
    """Scope borrowed structure access across gst-python binding versions.

    Some 1.26 overrides return a context manager; others return a plain
    Gst.Structure. Never retain the borrowed structure outside the with block.
    """
    structure = caps.get_structure(index)
    return structure if hasattr(structure, "__enter__") else nullcontext(structure)


def make_rtp_caps(Gst, config, fields):
    # Populate an owned structure, not a borrowed caps structure. This also
    # avoids older GI bindings' copy-on-access behavior when mutating caps.
    structure = Gst.Structure.new_empty("application/x-rtp")
    for key, value in {"media": "audio", "payload": config["payload_type"], **fields}.items():
        structure.set_value(key, value)
    caps = Gst.Caps.new_empty()
    caps.append_structure(structure)
    return caps


def load_gst():
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        return Gst
    except (ImportError, ValueError) as exc:
        raise DependencyUnavailable("GStreamer/PyGObject is unavailable; install the Linux dependencies documented in README.md") from exc


@dataclass
class RetryBackoff:
    attempt: int = 0

    def next_delay(self):
        delays = (1, 2, 5, 10, 30)
        result = delays[min(self.attempt, len(delays) - 1)]
        self.attempt += 1
        return result

    def reset(self):
        self.attempt = 0


def rtp_parameters(config):
    codec = config["codec"]
    if codec == "OPUS":
        return "rtpopusdepay", {"encoding-name": "OPUS", "clock-rate": 48000}
    if codec == "MP3":
        return "rtpmpadepay", {"encoding-name": "MPA", "clock-rate": 90000}
    if not config.get("clock_rate") or not config.get("channels"):
        raise DependencyUnavailable(f"RTP {codec} requires explicit clock_rate and channels")
    base = {"clock-rate": config["clock_rate"], "encoding-params": str(config["channels"])}
    if codec == "PCM":
        return "rtpL16depay", base | {"encoding-name": "L16", "channels": config["channels"]}
    if not config.get("codec_data"):
        raise DependencyUnavailable("RTP AAC requires codec_data (hexadecimal MPEG4 AudioSpecificConfig)")
    return "rtpmp4gdepay", base | {"encoding-name": "MPEG4-GENERIC", "media": "audio", "mode": "AAC-hbr",
                                  "config": config["codec_data"], "sizelength": 13, "indexlength": 3, "indexdeltalength": 3}


class GstPipeline:
    """One supervised native pipeline per source; worker owns all slow operations.

    appsink copies a bounded PCM block into staging and returns. Bus handling,
    metadata, fan-out and reconnect run on the worker, never in the callback.
    """
    mocked = False

    def __init__(self, source, *, profile=None, output=None, timeout_s=None, reconnect=True):
        if source.type == SourceType.ANALOG:
            raise ValueError("ANALOG must bypass GStreamer")
        self.source = source
        self.profile = profile
        self.output = output or PcmOutput()
        self.timeout_s = timeout_s or min(30, max(1, source.config.get("connect_timeout_ms", 10000) / 1000))
        self.reconnect = reconnect
        self.staging = PcmQueue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._snapshot = {"state": "IDLE", "codec": None, "input_sample_rate": None,
                          "input_channels": None, "bitrate_bps": None, "stream_title": None,
                          "artist": None, "title": None, "output_caps": None,
                          "reconnect_count": 0, "last_error": None}
        self._callback_error = None
        self._last_sample = 0.0
        self._caps = None
        self._pipeline = None
        self._input_caps = None

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)

    def _update(self, **values):
        with self._lock:
            self._snapshot.update(values)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"source-{getattr(self.source, 'id', 'probe')}", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + self.timeout_s
        while not self._ready.wait(0.02):
            if self._finished.is_set() or time.monotonic() >= deadline:
                message = self.snapshot()["last_error"] or "Timed out waiting for decoded PCM audio"
                self.stop()
                raise DependencyUnavailable(message)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_s + 2)
            if self._thread.is_alive():
                raise DependencyUnavailable("GStreamer source did not stop within its timeout")
        self.staging.clear()
        self.output.clear()

    def _make(self, factory, **properties):
        element = self.Gst.ElementFactory.make(factory, None)
        if element is None:
            raise DependencyUnavailable(f"Missing GStreamer element: {factory}")
        for name, value in properties.items():
            element.set_property(name.replace("_", "-"), value)
        self._pipeline.add(element)
        return element

    def _link(self, *elements):
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise DependencyUnavailable(f"Cannot link {left.get_name()} to {right.get_name()}")

    def _overrides(self, element, group):
        allowed = {"source": {"timeout", "user-agent", "buffer-size"},
                   "queue": {"max-size-time"}, "resample": {"quality", "sinc-filter-mode"}}
        for name, value in self.source.gst_options.get(group, {}).items():
            if name not in allowed[group] or element.find_property(name) is None:
                raise DependencyUnavailable(f"Unsupported GStreamer {group} property: {name}")
            if group == "source" and self.source.type == SourceType.HTTP and name == "timeout" and (not isinstance(value, (int, float)) or value <= 0 or value > self.timeout_s):
                raise DependencyUnavailable("Source timeout override must be positive and within the probe deadline (HTTP seconds, UDP nanoseconds)")
            element.set_property(name, value)

    def _source_setup(self, unused, element):
        try:
            config = self.source.config
            if element.find_property("timeout"):
                element.set_property("timeout", math.ceil(self.timeout_s))
            if element.find_property("user-agent"):
                element.set_property("user-agent", config.get("user_agent", "fmheadend/0.1.0"))
            if element.find_property("iradio-mode"):
                element.set_property("iradio-mode", True)
            if element.find_property("retries"):
                element.set_property("retries", 0)
            self._overrides(element, "source")
        except Exception as exc:
            self._callback_error = str(exc)

    def _decoded_pad(self, unused, pad):
        try:
            caps = pad.get_current_caps() or pad.query_caps(None)
            if not caps:
                return
            with caps_structure(caps) as structure:
                is_audio = structure.get_name() == "audio/x-raw"
            if is_audio:
                target = self._convert.get_static_pad("sink")
                if not target.is_linked():
                    self._input_caps = caps.copy()
                    if pad.link(target) != self.Gst.PadLinkReturn.OK:
                        self._callback_error = "Cannot link decoded audio pad"
        except Exception as exc:
            self._callback_error = str(exc)

    def _build(self):
        Gst = self.Gst
        self._pipeline = Gst.Pipeline.new(None)
        self._convert = self._make("audioconvert")
        resample = self._make("audioresample")
        self._overrides(resample, "resample")
        volume = self._make("volume")
        if self.profile and self.profile.processing_enabled:
            # Native volume element performs the actual processing.
            gain = 10 ** (self.profile.gain_db / 20)
            if not math.isfinite(gain) or not 0 <= gain <= 10:
                raise DependencyUnavailable("GStreamer volume gain must be at most +20 dB")
            volume.set_property("volume", gain)
            if any((self.profile.eq_config, self.profile.compressor_config, self.profile.limiter_config, self.profile.advanced_options)):
                raise DependencyUnavailable("Milestone 2 AudioProfile processing supports gain only")
        capsfilter = self._make("capsfilter", caps=Gst.Caps.from_string(PCM_CAPS))
        queue = self._make("queue", max_size_buffers=8, max_size_bytes=19200, max_size_time=100000000, leaky=0)
        self._overrides(queue, "queue")
        sink = self._make("appsink", caps=Gst.Caps.from_string(PCM_CAPS), emit_signals=True,
                          sync=True, max_buffers=2, drop=True, wait_on_eos=False, enable_last_sample=False)
        sink.connect("new-sample", self._sample)
        self._link(self._convert, resample, volume, capsfilter, queue, sink)
        self.sink = sink
        config = self.source.config
        if self.source.type == SourceType.HTTP:
            decode = self._make("uridecodebin", uri=config["uri"], caps=Gst.Caps.from_string("audio/x-raw"),
                                buffer_duration=config.get("buffer_ms", 3000) * 1000000)
            decode.connect("source-setup", self._source_setup)
            decode.connect("pad-added", self._decoded_pad)
        elif self.source.type == SourceType.ALSA:
            src = self._make("alsasrc", device=config["device"])
            self._overrides(src, "source")
            capture = self._make("capsfilter", caps=Gst.Caps.from_string(
                f"audio/x-raw,rate={config['sample_rate']},channels={config['channels']}"))
            self._link(src, capture, self._convert)
        else:
            src = self._make("udpsrc", address=config.get("multicast_group") or config["host"], port=config["port"])
            self._overrides(src, "source")
            if self.source.type == SourceType.UDP:
                src.set_property("caps", Gst.Caps.from_string(
                    f"audio/x-raw,format={config['format']},rate={config['sample_rate']},channels={config['channels']},layout=interleaved"))
                self._link(src, self._convert)
            else:
                factory, fields = rtp_parameters(config)
                caps = make_rtp_caps(Gst, config, fields)
                src.set_property("caps", caps)
                jitter = self._make("rtpjitterbuffer", latency=100, drop_on_latency=True)
                depay = self._make(factory)
                decode = self._make("decodebin", caps=Gst.Caps.from_string("audio/x-raw"))
                decode.connect("pad-added", self._decoded_pad)
                self._link(src, jitter, depay, decode)
        self._bus = self._pipeline.get_bus()

    def _sample(self, sink):
        Gst = self.Gst
        sample = sink.emit("pull-sample")  # already available in new-sample
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps()
        with caps_structure(caps) as structure:
            valid = (structure.get_name() == "audio/x-raw" and structure.get_string("format") == "S16LE"
                     and structure.get_value("rate") == 48000 and structure.get_value("channels") == 2
                     and structure.get_string("layout") == "interleaved")
        if not valid:
            self._callback_error = "Unexpected appsink caps: " + caps.to_string()
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        size = buffer.get_size()
        if size and size <= self.staging.max_bytes and size % 4 == 0:
            self.staging.put(buffer.extract_dup(0, size))
            self._last_sample = time.monotonic()
            self._caps = caps.to_string()
        elif size:
            self._callback_error = "PCM buffer exceeds the bounded output block size or is not frame-aligned"
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _tags(self, tags):
        values = {}
        for tag, key in (("audio-codec", "codec"), ("title", "title"), ("artist", "artist")):
            ok, value = tags.get_string(tag)
            if ok:
                values[key] = "".join(c for c in value if c.isprintable())[:1024]
        for tag in ("bitrate", "nominal-bitrate"):
            ok, value = tags.get_uint(tag)
            if ok:
                values["bitrate_bps"] = value
                break
        old = self.snapshot()
        if "title" in values or "artist" in values:
            title = values.get("title", old["title"]) or ""
            artist = values.get("artist", old["artist"]) or ""
            values["stream_title"] = f"{artist} - {title}" if artist and title else title or artist or None
        self._update(**values)

    def _attempt(self):
        Gst = self.Gst
        self._callback_error, self._caps, self._input_caps = None, None, None
        self._last_sample = 0
        self._update(state="CONNECTING", stream_title=None, artist=None, title=None,
                     codec=None, bitrate_bps=None, input_sample_rate=None, input_channels=None, output_caps=None)
        self._build()
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise DependencyUnavailable("GStreamer refused PLAYING state")
        started = time.monotonic()
        running_since = None
        while not self._stop.is_set():
            message = self._bus.timed_pop(10 * Gst.MSECOND)
            if message:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise DependencyUnavailable(error.message)
                if message.type == Gst.MessageType.EOS:
                    raise DependencyUnavailable("Source reached end of stream")
                if message.type == Gst.MessageType.TAG:
                    self._tags(message.parse_tag())
                if message.type == Gst.MessageType.BUFFERING:
                    self._update(state="BUFFERING" if message.parse_buffering() < 100 else "CONNECTING")
            if self._callback_error:
                raise DependencyUnavailable(self._callback_error)
            if time.monotonic() - (self._last_sample or started) > self.timeout_s:
                raise DependencyUnavailable("Timed out waiting for audio (source stalled)")
            for _ in range(16):
                data = self.staging.pop()
                if data is None:
                    break
                self.output.publish(data)
                if running_since is None:
                    running_since = time.monotonic()
                caps = self._input_caps or self._convert.get_static_pad("sink").get_current_caps()
                fields = {}
                if caps:
                    with caps_structure(caps) as structure:
                        fields = {"input_sample_rate": structure.get_value("rate"), "input_channels": structure.get_value("channels")}
                if self.snapshot()["codec"] is None:
                    fields["codec"] = self.source.config.get("codec") or ("PCM" if self.source.type in (SourceType.UDP, SourceType.ALSA) else "decoded audio")
                self._update(state="RUNNING", output_caps=self._caps, last_error=None, **fields)
                self._ready.set()
            if running_since and time.monotonic() - running_since >= 30:
                self._backoff.reset()

    def _run(self):
        self._backoff = RetryBackoff()
        try:
            self.Gst = load_gst()
            while not self._stop.is_set():
                try:
                    self._attempt()
                except Exception as exc:
                    self._update(state="ERROR", last_error=str(exc))
                    log.warning("source %s: %s", getattr(self.source, "id", "probe"), exc)
                finally:
                    if self._pipeline is not None:
                        self._pipeline.set_state(self.Gst.State.NULL)
                        self._pipeline = None
                    self.staging.clear()
                    self.output.clear()
                if self._stop.is_set() or not self.reconnect or not self._ready.is_set():
                    break
                delay = self._backoff.next_delay()
                self._update(state="CONNECTING", stream_title=None, artist=None, title=None)
                if self._stop.wait(delay):
                    break
                self._update(reconnect_count=self.snapshot()["reconnect_count"] + 1)
        except Exception as exc:
            self._update(state="ERROR", last_error=str(exc))
        finally:
            self._finished.set()

    def probe_result(self):
        info = self.snapshot()
        return SourceTestResult(ok=info["state"] == "RUNNING" and info["output_caps"] is not None, mocked=False, codec=info["codec"],
                                sample_rate=info["input_sample_rate"], channels=info["input_channels"],
                                output_caps=info["output_caps"], stream_title=info["stream_title"],
                                message="Decoded PCM received" if info["output_caps"] else info["last_error"] or "No decoded audio")
