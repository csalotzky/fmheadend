"""GStreamer ingest. GI is loaded only when a digital source is opened."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
import logging
import math
from pathlib import Path
from urllib.parse import unquote, urlsplit
import threading
import time

from ..errors import DependencyUnavailable
from ..models.configuration import SourceType, SourceTestResult, LocalConfig
from .local_playback import LocalPlayback, LocalPlaybackError
from .alsa_capture import link_alsa_capture
from .output import (PCM_CAPS, PcmQueue, PcmOutput, pcm_capacity_bytes,
                     DEFAULT_STAGING_BUFFER_MS, DEFAULT_CONSUMER_BUFFER_MS, DEFAULT_STAGING_MAX_AGE_MS)
from .dsp import PROCESSING_CAPS
from .dsp import dsp_stages
from .dsp_telemetry import METERS, StageMeters

log = logging.getLogger(__name__)


class PipelineConfigurationError(DependencyUnavailable):
    """Permanent construction/format failure, not temporary source availability."""


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
        # Must precede plugin loading. LV2 setup occurs later on streaming
        # threads, beyond the lifetime of any Python construction lock.
        from .lv2_lifecycle import ensure_lv2_lifecycle
        ensure_lv2_lifecycle()
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
    fan-out and reconnect run on the worker. LOCAL serialized sink tags publish
    item metadata with rendered PCM; callbacks never resolve files or perform IO.
    """
    mocked = False

    @staticmethod
    def validate_profile(profile):
        Gst = load_gst()
        def check(factory, **properties):
            element = Gst.ElementFactory.make(factory, None)
            if element is None:
                raise DependencyUnavailable(f'Missing GStreamer element: {factory}')
            for name, value in properties.items():
                element.set_property(name.replace('_', '-'), value)
            return element
        dsp_stages(check, profile)

    def __init__(self, source, *, profile=None, output=None, timeout_s=None, reconnect=True,
                 pcm_staging_buffer_ms=DEFAULT_STAGING_BUFFER_MS,
                 pcm_consumer_buffer_ms=DEFAULT_CONSUMER_BUFFER_MS,
                 pcm_staging_max_age_ms=DEFAULT_STAGING_MAX_AGE_MS,
                 podcast_database_path=None, podcast_runtime_factory=None):
        if source.processor_mode == 'stereotool':
            raise DependencyUnavailable('Stereo Tool processing is not implemented')
        if source.processor_mode == 'builtin' and profile is None:
            raise DependencyUnavailable('Built-in DSP requires its validated profile')
        if source.processor_mode == 'bypass' and profile is not None:
            raise DependencyUnavailable('Bypass must not load a processing profile')
        self._local = None
        self._podcast_database_path = podcast_database_path
        self._podcast_runtime_factory = podcast_runtime_factory
        self._media_idle = False
        self._buffering_paused = False
        self._pending_metadata = None
        self._metadata_dirty = False
        self._uri_change_lock = threading.Lock()
        self.source = source
        self.profile = profile
        self.output = output or PcmOutput(pcm_consumer_buffer_ms)
        self.timeout_s = timeout_s or min(30, max(1, source.config.get("connect_timeout_ms", 10000) / 1000))
        self.reconnect = reconnect
        self._audio_ready = threading.Event()
        pcm_capacity_bytes(pcm_staging_max_age_ms)
        self.staging = PcmQueue(max_bytes=pcm_capacity_bytes(pcm_staging_buffer_ms),
                                max_age_s=pcm_staging_max_age_ms / 1000.0, wakeup=self._audio_ready)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._prepared = threading.Event()
        self.fatal_error = None
        self._finished = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._telemetry_request = (False, 50.0)
        self._telemetry_revision = 0
        self._telemetry_cache = None
        self._telemetry_generation = 0
        self._telemetry_next_ns = 0
        self._dsp_elements = {}
        self._dsp_meters = {}
        self._peak_elements = {}
        self._peak_values = {}
        self._peak_state = None
        self._peak_seqnum = None
        self._snapshot = {"state": "IDLE", "codec": None, "input_sample_rate": None,
                          "input_channels": None, "bitrate_bps": None, "stream_title": None,
                          "artist": None, "title": None, "output_caps": None,
                          "reconnect_count": 0, "last_error": None}
        self._station_name = None
        self._callback_error = None
        self._last_sample = 0.0
        self._caps = None
        self._pipeline = None
        self._input_caps = None
        self._validated_output_caps = None
        self._validated_output_text = None

    def request_telemetry(self, enabled, hz=50.0):
        """Control-thread request only; never touches native elements."""
        with self._lock:
            request = (bool(enabled), hz)
            if request == self._telemetry_request:
                return
            self._telemetry_request = request
            self._telemetry_revision += 1
            self._telemetry_cache = None
        self._audio_ready.set()

    def telemetry_snapshot(self):
        with self._lock:
            return dict(telemetry_available=self._telemetry_cache is not None,
                        generation=self._telemetry_generation,
                        **deepcopy(self._telemetry_cache or {}))

    def _register_dsp(self, stage, implementation, element, settings):
        self._dsp_elements[stage] = element
        if implementation in METERS:
            self._dsp_meters[stage] = StageMeters(element, implementation, settings)

    def _invalidate_telemetry(self, *, discard=False):
        # Worker only. Invalidate BEFORE NULL; restore controls while elements exist.
        with self._lock:
            self._telemetry_cache = None
        for meter in self._dsp_meters.values():
            meter.enable(False)
        self._set_peak_metering(False)
        self._telemetry_next_ns = 0
        if discard:
            self._dsp_meters.clear()
            self._dsp_elements.clear()
            self._peak_elements.clear()

    def _set_peak_metering(self, enabled, hz=50.0, revision=None):
        """Worker only: level computes in native code; we consume its bus messages."""
        state = (enabled, hz, revision)
        if state == self._peak_state:
            return
        self._peak_state = state
        self._peak_values.clear()
        self._peak_seqnum = None
        if enabled and self._peak_elements:
            # Reject queued messages from before enable/rate change, even when
            # a NULL recovery reuses the same native level elements.
            self._peak_seqnum = self.Gst.util_seqnum_next()
        for element in self._peak_elements.values():
            try:
                element.set_property('post-messages', False)
                if enabled:
                    element.set_property('interval', int(1_000_000_000 / hz))
                    element.set_property('post-messages', True)
            except Exception:
                log.warning('Could not configure endpoint peak metering')

    def _peak_message(self, message):
        if not self._peak_elements:
            return
        endpoint = next((key for key, element in self._peak_elements.items() if message.src == element), None)
        if endpoint is None or not self._peak_state or not self._peak_state[0]:
            return
        with self._lock:
            enabled = self._telemetry_request[0]
            revision = self._telemetry_revision
        if not enabled or revision != self._peak_state[2] or self._peak_seqnum is None:
            return
        if self.Gst.util_seqnum_compare(message.get_seqnum(), self._peak_seqnum) <= 0:
            return
        try:
            borrowed = message.get_structure()
            with borrowed if hasattr(borrowed, '__enter__') else nullcontext(borrowed) as structure:
                if structure is None or structure.get_name() != 'level':
                    return
                peaks = structure.get_value('peak')
                if len(peaks) != 2:
                    raise ValueError('Expected stereo level message')
                values = {key: value if isinstance(value, (int, float)) and math.isfinite(value) else None
                          for key, value in zip(('peak_l_dbfs', 'peak_r_dbfs'), peaks)}
            self._peak_values[endpoint] = (time.monotonic_ns(), values)
        except Exception:
            self._peak_values.pop(endpoint, None)

    def _telemetry_tick(self):
        with self._lock:
            enabled, hz = self._telemetry_request
            revision = self._telemetry_revision
        enabled = enabled and not self._stop.is_set() and not self._media_idle and self._pipeline is not None
        for meter in self._dsp_meters.values():
            meter.enable(enabled)
        self._set_peak_metering(enabled, hz, revision)
        if not enabled or not (self._dsp_meters or self._peak_elements):
            with self._lock:
                self._telemetry_cache = None
            self._telemetry_next_ns = 0
            return
        now = time.monotonic_ns()
        if now < self._telemetry_next_ns:
            return
        stages = {stage: meter.collect() for stage, meter in self._dsp_meters.items()}
        end = time.monotonic_ns()
        sample = dict(sample_monotonic_ns=now, collect_duration_ns=end - now, stages=stages)
        for endpoint in ('input', 'output'):
            measured = self._peak_values.get(endpoint)
            # Do not present an old peak as current while PCM is unavailable.
            sample[endpoint] = (measured[1] if measured and now - measured[0] <= max(250_000_000, int(3e9 / hz))
                                else None)
        # No catch-up passes. Native reads take place outside the cache lock.
        self._telemetry_next_ns = end + int(1_000_000_000 / hz)
        with self._lock:
            if revision == self._telemetry_revision and not self._stop.is_set():
                self._telemetry_cache = sample

    def _telemetry_wait(self, maximum):
        if self._telemetry_next_ns:
            return min(maximum, max(0.0, (self._telemetry_next_ns - time.monotonic_ns()) / 1_000_000_000))
        return maximum

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)

    def pcm_snapshot(self):
        return dict(staging=self.staging.snapshot(), consumers=self.output.snapshot())

    def _update(self, **values):
        with self._lock:
            self._snapshot.update(values)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"source-{getattr(self.source, 'id', 'probe')}", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + self.timeout_s
        # Normal operation needs a constructed pipeline, not a live sender.
        # Explicit probes still require decoded PCM within their deadline.
        ready = self._prepared if self.reconnect else self._ready
        while not ready.wait(0.02):
            if self._finished.is_set() or time.monotonic() >= deadline:
                message = self.snapshot()["last_error"] or (
                    "Timed out constructing audio pipeline" if self.reconnect else "Timed out waiting for decoded PCM audio")
                self.stop()
                raise DependencyUnavailable(message)
        if self.fatal_error:
            self.stop()
            raise DependencyUnavailable(self.fatal_error)

    def stop(self):
        if self.source.type == SourceType.PODCAST:
            # Serialize stop intent with buffering-driven state changes.
            with self._uri_change_lock:
                self._stop.set()
        else:
            self._stop.set()
        self._audio_ready.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_s + (32 if self.source.type == SourceType.PODCAST else 2))
            if self._thread.is_alive():
                raise DependencyUnavailable("GStreamer source did not stop within its timeout")
            if self.source.type == SourceType.PODCAST and self._local is not None:
                # Retain manager ownership if an OS/network call outlived the
                # worker's bounded cleanup. Never leave a competing refresh owner.
                self._local.close()
                self._local = None
                self._clear_local_metadata()
        self.staging.clear()
        self.output.clear()

    def _make(self, factory, **properties):
        element = self.Gst.ElementFactory.make(factory, None)
        if element is None:
            raise DependencyUnavailable(f"Missing GStreamer element: {factory}")
        for name, value in properties.items():
            element.set_property(name.replace("_", "-"), value)
        getattr(self, "_element_parent", self._pipeline).add(element)
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
            if self.source.type == SourceType.PODCAST:
                self._local.bind_source(element)
            if element.find_property("timeout"):
                element.set_property("timeout", math.ceil(self.timeout_s))
            if element.find_property("user-agent"):
                element.set_property("user-agent", "fmheadend/0.1.0")
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
        self._invalidate_telemetry(discard=True)
        Gst = self.Gst
        if self.source.type in (SourceType.LOCAL, SourceType.PODCAST):
            if self.source.type == SourceType.LOCAL:
                self._local = LocalPlayback(LocalConfig.model_validate(self.source.config).to_core(), self._audio_ready)
            else:
                from ..models.configuration import PodcastConfig
                from .podcast_runtime import PodcastRuntime
                factory = self._podcast_runtime_factory or PodcastRuntime
                self._local = factory(PodcastConfig.model_validate(self.source.config), self._audio_ready,
                    database_path=self._podcast_database_path, source_id=getattr(self.source, 'id', None))
                self._local.start()
            uri = self._local.initial()
            self._media_idle = uri is None
            self._pipeline = Gst.ElementFactory.make('playbin3', None)
            if self._pipeline is None:
                raise PipelineConfigurationError('Missing GStreamer element: playbin3')
            # Podcast enclosures are non-live: preserve clock pacing through rebuffering.
            flags = 2 | (0x100 if self.source.type == SourceType.PODCAST else 0)
            self._pipeline.set_property('flags', flags)  # Audio, optional buffering; no soft volume.
            if uri is not None:
                self._pipeline.set_property('uri', uri)
            self._pipeline.connect('about-to-finish', self._local_handoff)
            self._pipeline.connect('source-setup', self._source_setup)
            self._element_parent = Gst.Bin.new(None)
        else:
            self._pipeline = Gst.Pipeline.new(None)
            self._element_parent = self._pipeline
        with self._lock:
            self._telemetry_generation += 1
        self._convert = self._make("audioconvert")
        resample = self._make("audioresample")
        self._overrides(resample, "resample")
        volume = self._make('volume', volume=10 ** (self.source.input_gain_db / 20))
        capsfilter = self._make("capsfilter", caps=Gst.Caps.from_string(PCM_CAPS))
        queue = self._make("queue", max_size_buffers=8, max_size_bytes=19200, max_size_time=100000000, leaky=0)
        self._overrides(queue, "queue")
        sink = self._make("appsink", caps=Gst.Caps.from_string(PCM_CAPS), emit_signals=True,
                          sync=True, max_buffers=2, drop=True, wait_on_eos=False, enable_last_sample=False)
        sink.connect("new-sample", self._sample)
        input_peak = self._make('level', post_messages=False, interval=20_000_000, audio_level_meta=False)
        output_peak = self._make('level', post_messages=False, interval=20_000_000, audio_level_meta=False)
        self._peak_elements = dict(input=input_peak, output=output_peak)
        self._peak_state = None
        if self.profile:
            domain = self._make("capsfilter", caps=Gst.Caps.from_string(PROCESSING_CAPS))
            stages = dsp_stages(self._make, self.profile, on_stage=self._register_dsp)
            final_convert = self._make("audioconvert")
            self._link(self._convert, resample, domain, input_peak, volume, *stages,
                       final_convert, capsfilter, queue, output_peak, sink)
        else:
            domain = self._make('capsfilter', caps=Gst.Caps.from_string(PCM_CAPS))
            self._link(self._convert, resample, domain, input_peak, volume, capsfilter, queue, output_peak, sink)
        self.sink = sink
        config = self.source.config
        if self.source.type in (SourceType.LOCAL, SourceType.PODCAST):
            ghost = Gst.GhostPad.new('sink', self._convert.get_static_pad('sink'))
            self._element_parent.add_pad(ghost)
            self._pipeline.set_property('audio-sink', self._element_parent)
            sink.get_static_pad('sink').add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._local_event)
        elif self.source.type == SourceType.HTTP:
            decode = self._make("uridecodebin", uri=config["uri"], caps=Gst.Caps.from_string("audio/x-raw"),
                                buffer_duration=config.get("buffer_ms", 3000) * 1000000)
            decode.connect("source-setup", self._source_setup)
            decode.connect("pad-added", self._decoded_pad)
        elif self.source.type == SourceType.ALSA:
            src = self._make("alsasrc", device=config["device"])
            self._alsa_source = src
            self._overrides(src, "source")
            link_alsa_capture(Gst, self._make, self._link, src, self._convert, config['channel_selection'])
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

    def _local_handoff(self, player):
        # State teardown must not race URI replacement. Never wait here: NULL
        # can be waiting for this very streaming thread to return.
        if self._stop.is_set() or not self._uri_change_lock.acquire(blocking=False):
            return
        try:
            self._local.handoff(player)
        except Exception:
            self._callback_error = 'Sequenced media URI handoff failed'
            self._audio_ready.set()
        finally:
            self._uri_change_lock.release()

    def _local_event(self, pad, info):
        event = info.get_event()
        # These serialized sink events cannot overtake PCM from an earlier item.
        # Bus TAG messages can be queued/prefetched, so LOCAL does not use them.
        if event.type == self.Gst.EventType.STREAM_START:
            uri = self._local.stream_start()
            title = Path(unquote(urlsplit(uri).path)).stem if uri else ''
            title = ''.join(c for c in title if c.isprintable())[:1024] or None
            self._pending_metadata = dict(title=title, artist=None, stream_title=title,
                                          codec=None, bitrate_bps=None)
            if self.source.type == SourceType.PODCAST:
                values = dict(title=None, artist=None, stream_title=None)
                values.update(self._local.identity_metadata())
                self._pending_metadata.update(self._formatted_metadata(values, self._pending_metadata))
            self._metadata_dirty = True
        elif event.type == self.Gst.EventType.TAG and self._pending_metadata is not None:
            values = self._tag_values(event.parse_tag())
            if self.source.type == SourceType.PODCAST:
                values = {k: v for k, v in values.items() if k not in ('title', 'artist', 'stream_title')}
            # An empty tag is not a usable replacement for the filename fallback.
            values = {k: v for k, v in values.items() if k not in ('title', 'artist') or v.strip()}
            self._pending_metadata.update(self._formatted_metadata(values, self._pending_metadata))
            self._metadata_dirty = True
        return self.Gst.PadProbeReturn.OK

    def _local_resume(self, *, failed):
        # Exceptional recovery only. Keep the same native elements, DSP chain,
        # PcmOutput and consumers; normal handoff never changes pipeline state.
        self._invalidate_telemetry()
        with self._uri_change_lock:
            self._pipeline.set_state(self.Gst.State.NULL)
            self._buffering_paused = False
            self._bus.set_flushing(True)
            self._bus.set_flushing(False)
            self._clear_local_metadata()
            uri = self._local.resume(failed=failed)
            # Already decoded tail PCM still belongs to this shared output.
            # Let the ordinary drain/age policy handle it across EOS recovery.
            self._last_sample = 0
            self._media_idle = uri is None
            if self._media_idle:
                self._update(state='ERROR', last_error=self._local.error)
            else:
                self._pipeline.set_property('uri', uri)
                self._pipeline.set_state(self.Gst.State.PLAYING)

    def _buffering(self, percent):
        if self.source.type != SourceType.PODCAST:
            self._update(state="BUFFERING" if percent < 100 else "CONNECTING")
            return
        # Control worker only, serialized with native URI/state changes. A buffering
        # pause is not a stop: keep the same tickets, PCM owner and history counters.
        with self._uri_change_lock:
            if self._stop.is_set() or self._media_idle or self._pipeline is None:
                return
            paused = percent < 100
            if paused == self._buffering_paused:
                return
            state = self.Gst.State.PAUSED if paused else self.Gst.State.PLAYING
            if self._pipeline.set_state(state) == self.Gst.StateChangeReturn.FAILURE:
                raise DependencyUnavailable("GStreamer refused Podcast buffering state")
            self._buffering_paused = paused
            self._update(state="BUFFERING" if paused else "CONNECTING")

    def _sample(self, sink):
        Gst = self.Gst
        sample = sink.emit("pull-sample")  # already available in new-sample
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps()
        # Samples retain their negotiated caps. Revalidate changed caps, but do
        # not marshal the same structure/fields through GI on every PCM block.
        if caps is None:
            self._callback_error = "Missing appsink caps"
            return Gst.FlowReturn.ERROR
        if self._validated_output_caps is None or not caps.is_equal(self._validated_output_caps):
            with caps_structure(caps) as structure:
                valid = (structure.get_name() == "audio/x-raw" and structure.get_string("format") == "S16LE"
                         and structure.get_value("rate") == 48000 and structure.get_value("channels") == 2
                         and structure.get_string("layout") == "interleaved")
            if not valid:
                self._callback_error = "Unexpected appsink caps: " + caps.to_string()
                return Gst.FlowReturn.ERROR
            self._validated_output_caps = caps
            self._validated_output_text = caps.to_string()
        buffer = sample.get_buffer()
        size = buffer.get_size()
        if size and size <= self.staging.max_bytes and size % 4 == 0:
            data = buffer.extract_dup(0, size)
            self._last_sample = time.monotonic()
            self._caps = self._validated_output_text
            # Publish metadata before waking the source worker/readiness path.
            if self._local is not None:
                self._local.pcm(size)
                self._publish_local_metadata()
            self.staging.put(data)
        elif size:
            self._callback_error = "PCM buffer exceeds the bounded output block size or is not frame-aligned"
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _station_headers(self, message):
        # Only transport ICY identity, never decoded title/artist/organization tags.
        if self.source.type != SourceType.HTTP:
            return
        borrowed = message.get_structure()
        if borrowed is None:
            return
        with borrowed if hasattr(borrowed, "__enter__") else nullcontext(borrowed) as structure:
            if structure.get_name() != "http-headers":
                return
            headers = structure.get_value("response-headers")
            if headers is None:
                return
            with headers if hasattr(headers, "__enter__") else nullcontext(headers) as values:
                for index in range(values.n_fields()):
                    key = values.nth_field_name(index)
                    if key.lower() == "icy-name":
                        value = values.get_value(key)
                        if isinstance(value, str):
                            name = "".join(c for c in value if c.isprintable()).strip()[:200]
                            with self._lock:
                                self._station_name = name or None
                        break

    def _clear_local_metadata(self):
        # Worker only, after native NULL has quiesced the sink callbacks.
        self._pending_metadata = None
        self._metadata_dirty = False
        self._update(title=None, artist=None, stream_title=None, codec=None, bitrate_bps=None)

    def _publish_local_metadata(self):
        # Sink streaming thread only: publish at the first actual PCM, not when
        # STREAM_START/preroll or about-to-finish prepares the next item.
        if self._metadata_dirty and self._pending_metadata is not None:
            self._update(**self._pending_metadata)
            self._metadata_dirty = False

    @staticmethod
    def _tag_values(tags):
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
        return values

    @staticmethod
    def _formatted_metadata(values, old):
        values = dict(values)
        if "title" in values or "artist" in values:
            title = values.get("title", old["title"]) or ""
            artist = values.get("artist", old["artist"]) or ""
            values["stream_title"] = f"{artist} - {title}" if artist and title else title or artist or None
        return values

    def _tags(self, tags):
        self._update(**self._formatted_metadata(self._tag_values(tags), self.snapshot()))

    def _bus_failure(self, message):
        error, debug = message.parse_error()
        Gst = self.Gst
        if error.matches(Gst.CoreError.quark(), Gst.CoreError.MISSING_PLUGIN) or error.matches(
                Gst.StreamError.quark(), Gst.StreamError.CODEC_NOT_FOUND):
            return PipelineConfigurationError(error.message)
        if self.source.type == SourceType.ALSA:
            detail = f"ALSA capture {self.source.config['device']}: {error.message}"
            if self.source.config['device'].startswith('hw:'):
                detail += ' Raw hw: capture requires a supported 48 kHz mono/stereo format; use plughw: for ALSA conversion.'
            return DependencyUnavailable(detail)
        return DependencyUnavailable(error.message)

    def _attempt(self):
        Gst = self.Gst
        self._callback_error, self._caps, self._input_caps = None, None, None
        self._validated_output_caps = self._validated_output_text = None
        with self._lock:
            self._station_name = None
        self._last_sample = 0
        self._buffering_paused = False
        self._update(state="CONNECTING", stream_title=None, artist=None, title=None,
                     codec=None, bitrate_bps=None, input_sample_rate=None, input_channels=None, output_caps=None)
        try:
            self._build()
        except LocalPlaybackError:
            raise
        except Exception as exc:
            raise PipelineConfigurationError(str(exc)) from exc
        state_change = self._pipeline.set_state(Gst.State.NULL if self._media_idle else Gst.State.PLAYING)
        if self._callback_error:
            raise PipelineConfigurationError(self._callback_error)
        if state_change == Gst.StateChangeReturn.FAILURE:
            # A failed state change may already have posted a missing-plugin
            # error. Preserve that distinction before accepting a waiting source.
            message = self._bus.timed_pop_filtered(0, Gst.MessageType.ERROR)
            failure = self._bus_failure(message) if message else DependencyUnavailable("GStreamer refused PLAYING state")
            if not isinstance(failure, PipelineConfigurationError):
                self._prepared.set()
            if self._local is None:
                raise failure
            self._local_resume(failed=True)
        self._prepared.set()
        started = time.monotonic()
        running_since = None
        observed_input_caps = None
        input_fields = {}
        while not self._stop.is_set():
            # Clear before inspecting work, so a concurrent callback cannot be
            # lost between draining staging and waiting. GstBus never delays PCM.
            self._audio_ready.clear()
            if self._media_idle:
                self._telemetry_tick()
                self._local.service(self._pipeline)
                with self._local.lock:
                    available = self._local.prepared is not None
                if available:
                    self._local_resume(failed=False)
                    started = time.monotonic()
                else:
                    self._update(state='ERROR', last_error=self._local.error)
                    self._audio_ready.wait(1.0)
                continue
            bus_idle = False
            for _ in range(32):
                message = self._bus.timed_pop(0)
                if message is None:
                    bus_idle = True
                    break
                if message.type == Gst.MessageType.ERROR:
                    if self.source.type == SourceType.PODCAST and self._local.stale_error(message.src):
                        continue
                    if self._local is not None:
                        log.warning('Sequenced media item failed; skipping (GStreamer error)')
                        self._local_resume(failed=True)
                        started = time.monotonic()
                        break
                    raise self._bus_failure(message)
                if message.type == Gst.MessageType.EOS:
                    if self._local is not None:
                        self._local_resume(failed=False)
                        started = time.monotonic()
                        break
                    raise DependencyUnavailable("Source reached end of stream")
                if message.type == Gst.MessageType.ELEMENT:
                    self._peak_message(message)
                    self._station_headers(message)
                if message.type == Gst.MessageType.TAG and self._local is None:
                    self._tags(message.parse_tag())
                if message.type == Gst.MessageType.BUFFERING:
                    self._buffering(message.parse_buffering())
            if self._callback_error:
                raise PipelineConfigurationError(self._callback_error)
            if time.monotonic() - (self._last_sample or started) > self.timeout_s:
                if self._local is not None:
                    log.warning('Sequenced media item stalled; skipping')
                    self._local_resume(failed=True)
                    started = time.monotonic()
                    continue
                raise DependencyUnavailable("Timed out waiting for audio (source stalled)")
            for _ in range(16):
                data = self.staging.pop()
                if data is None:
                    break
                self.output.publish(data)
                if running_since is None:
                    running_since = time.monotonic()
                if self.source.type == SourceType.ALSA:
                    caps = self._alsa_source.get_static_pad("src").get_current_caps()
                else:
                    caps = self._input_caps or self._convert.get_static_pad("sink").get_current_caps()
                if caps is not observed_input_caps and (caps is None or observed_input_caps is None
                                                        or not caps.is_equal(observed_input_caps)):
                    input_fields = {}
                    if caps:
                        with caps_structure(caps) as structure:
                            input_fields = {"input_sample_rate": structure.get_value("rate"), "input_channels": structure.get_value("channels")}
                    observed_input_caps = caps
                fields = dict(input_fields)
                if self.snapshot()["codec"] is None:
                    fields["codec"] = self.source.config.get("codec") or ("PCM" if self.source.type in (SourceType.UDP, SourceType.ALSA) else "decoded audio")
                self._update(state="BUFFERING" if self._buffering_paused else "RUNNING",
                             output_caps=self._caps, last_error=None, **fields)
            else:
                # Bounded work per pass; check stop/bus again before more PCM.
                self._telemetry_tick()
                continue
            self._telemetry_tick()
            if self._local is not None:
                self._local.service(self._pipeline)
            if running_since is not None and bus_idle:
                self._ready.set()
            if not bus_idle:
                # Startup headers/tags must be serviced before reporting ready,
                # but PCM still drains between bounded bus batches.
                continue
            if running_since and time.monotonic() - running_since >= 30:
                self._backoff.reset()
            # PCM and stop wake immediately. Avoid 100 Hz empty-bus polling
            # between 21-43 ms decoded blocks; this ceiling only bounds bus
            # observation when no PCM arrives, never the audio/stall deadline.
            remaining = self.timeout_s - (time.monotonic() - (self._last_sample or started))
            self._audio_ready.wait(self._telemetry_wait(min(0.05, max(0.0, remaining))))

    def _run(self):
        self._backoff = RetryBackoff()
        try:
            self.Gst = load_gst()
            while not self._stop.is_set():
                try:
                    self._attempt()
                except Exception as exc:
                    if isinstance(exc, (PipelineConfigurationError, LocalPlaybackError)) or not isinstance(exc, DependencyUnavailable):
                        self.fatal_error = str(exc)
                    self._update(state="ERROR", last_error=str(exc))
                    log.warning("source %s: %s", getattr(self.source, "id", "probe"), exc)
                finally:
                    self._invalidate_telemetry(discard=True)
                    if self._pipeline is not None:
                        with self._uri_change_lock:
                            self._pipeline.set_state(self.Gst.State.NULL)
                        self._pipeline = None
                    if self._local is not None:
                        self._local.close()
                        self._local = None
                        self._clear_local_metadata()
                    self.staging.clear()
                    self.output.clear()
                if self._stop.is_set() or not self.reconnect or self.fatal_error:
                    break
                delay = self._backoff.next_delay()
                self._update(state="CONNECTING", stream_title=None, artist=None, title=None)
                if self._stop.wait(delay):
                    break
                self._update(reconnect_count=self.snapshot()["reconnect_count"] + 1)
        except Exception as exc:
            self.fatal_error = str(exc)
            self._update(state="ERROR", last_error=str(exc))
        finally:
            self._finished.set()

    def probe_result(self):
        info = self.snapshot()
        with self._lock:
            station_name = self._station_name if info["state"] == "RUNNING" else None
        bitrate = info["bitrate_bps"]
        input_bitrate = bitrate if (self.source.type == SourceType.HTTP and
            info["state"] == "RUNNING" and info["output_caps"] is not None and
            isinstance(bitrate, int) and not isinstance(bitrate, bool) and bitrate > 0) else None
        return SourceTestResult(input_bitrate_bps=input_bitrate, station_name=station_name, ok=info["state"] == "RUNNING" and info["output_caps"] is not None, mocked=False, codec=info["codec"],
                                sample_rate=info["input_sample_rate"], channels=info["input_channels"],
                                output_caps=info["output_caps"], stream_title=info["stream_title"],
                                message="Decoded PCM received" if info["output_caps"] else info["last_error"] or "No decoded audio")
