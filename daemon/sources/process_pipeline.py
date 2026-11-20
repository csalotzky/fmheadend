"""Experimental Linux HTTP/LOCAL/PODCAST/ALSA/UDP/RTP backend; parent output and cached state.

start/stop initiate work and never wait for a child/native reply on the control
executor. Pending STOP raises DependencyUnavailable, preserving SourceManager
ownership until a subsequent stop confirms reap. WorkerRecovery owns restart policy.
"""
from copy import deepcopy
import atexit
import logging
import socket
import subprocess
import sys
import threading
import time
import uuid

from ..errors import DependencyUnavailable, SourcePending, SourceStartupFailed
from ..models.configuration import SourceTestResult
from ..models.runtime_status import InputSourceStatus, PcmQueueStatus
from .audio_ipc import (PCM_MAX_AGE, PcmDecoder, ProtocolError, configure, envelope, pack, receive)
from .output import (PCM_CAPS, PcmOutput, DEFAULT_STAGING_BUFFER_MS,
                     DEFAULT_STAGING_MAX_AGE_MS, DEFAULT_CONSUMER_BUFFER_MS)

log = logging.getLogger(__name__)
METADATA = ('stream_title', 'title', 'artist', 'codec', 'bitrate_bps',
            'input_sample_rate', 'input_channels', 'output_caps')
# Cold Python/model initialization precedes the first control report. This
# absolute, one-shot allowance is separate from runtime status supervision.
STARTUP_TIMEOUT = 12.0

# Strong references survive SourceManager.close() clearing its registry. These
# are cleanup owners, not a restart supervisor. Only completed owners leave.
_owners = set()
_owners_lock = threading.Lock()
_exiting = False


def _shutdown_owners(timeout=1.5):
    """Bounded interpreter-exit grace, shared across all children, not per child.

    Daemon owners keep reaping while Python runs. At interpreter exit we cannot
    promise a wait for an uninterruptible child: after parent death Linux adopts
    it. This hook sends final KILL intent, not a false completion notification.
    Normal application shutdown must first stop submitting source operations.
    """
    global _exiting
    with _owners_lock:
        _exiting = True
        owners = tuple(_owners)
    for owner in owners:
        owner._stop.set()
    deadline = time.monotonic() + timeout
    for owner in owners:
        owner._done.wait(max(0, deadline - time.monotonic()))
    for owner in owners:
        if not owner._done.is_set() and owner.process is not None:
            try:
                owner.process.kill()
            except ProcessLookupError:
                pass  # The owner still performs wait, including this race.
            except Exception:
                log.exception('audiod final shutdown signal failed')


atexit.register(_shutdown_owners)


class ProcessPipeline:
    mocked = False
    radio_url = None
    audio_profile_id = ''
    worker_module = 'fmheadendd.sources.audiod'

    def __init__(self, source, *, profile=None, output=None, timeout_s=None,
                 reconnect=True, telemetry_configured=False,
                 pcm_staging_buffer_ms=DEFAULT_STAGING_BUFFER_MS,
                 pcm_staging_max_age_ms=DEFAULT_STAGING_MAX_AGE_MS,
                 pcm_consumer_buffer_ms=DEFAULT_CONSUMER_BUFFER_MS,
                 podcast_database_path=None, podcast_service=None, direct_runtime=None):
        if source.type not in ('HTTP', 'LOCAL', 'PODCAST', 'ALSA', 'UDP', 'RTP'):
            raise ValueError('audiod supports HTTP, LOCAL, PODCAST, ALSA, UDP and RTP only')
        if sys.platform != 'linux':
            raise DependencyUnavailable('Experimental audiod requires Linux socketpair/pass_fds')
        if direct_runtime is not None and source.type not in ("HTTP", "LOCAL", "PODCAST", "ALSA", "UDP", "RTP"):
            raise ValueError("Unsupported direct source type")
        self.direct_runtime = direct_runtime
        from .worker_services import RemoteRadio
        self.radio_url = RemoteRadio() if source.type == 'HTTP' and source.config.get('radio_browser_uuid') and reconnect else None
        self.telemetry_configured = telemetry_configured
        self._telemetry_request = (False, 50.0)
        self._telemetry_revision = 0
        self._telemetry_sent = 0
        self._telemetry_cache = None
        self._telemetry_at = 0
        self._direct_intent = (0, None)
        self._direct_intents = {}
        self._direct_sent_channels = {}
        self.direct_applied_channels = {}
        self._direct_metadata_sequence = 0
        self.direct_applied = (0, None)
        self.direct_progress = {}
        self.source = source
        self.output = output if output is not None else PcmOutput(pcm_consumer_buffer_ms)
        self.generation = uuid.uuid4().hex
        self.source_id = getattr(source, 'id', 0)
        self.timeout_s = timeout_s or min(30, max(1, source.config.get('connect_timeout_ms', 10000) / 1000))
        self.reconnect = reconnect
        self._create = dict(source=source.model_dump(mode='json'),
                            profile=profile.model_dump(mode='json') if profile else None,
                            options=dict(timeout_s=self.timeout_s, reconnect=reconnect,
                                pcm_staging_buffer_ms=pcm_staging_buffer_ms,
                                pcm_staging_max_age_ms=pcm_staging_max_age_ms,
                                pcm_consumer_buffer_ms=pcm_consumer_buffer_ms))
        if direct_runtime is not None:
            self._create["direct_runtime"] = str(direct_runtime)
        if telemetry_configured:
            self._create['telemetry_configured'] = True
        # Validate size before launching a process.
        self._create_packet = pack(envelope(self.source_id, self.generation, 1, 'create', self._create))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self.process = None
        self.exit_code = None
        self._status = dict(state='IDLE', last_error=None, error_scope='SOURCE')
        self._staging = {}
        self._probe = SourceTestResult(ok=False, mocked=False, message='Worker has not produced PCM')
        self._fatal_error = None
        self._prepared = False
        self._has_pcm = False
        self._http_retry_evidence = None
        self._http_retry_valid_at = None
        self._http_pcm_produced_at = 0.0
        self._pcm_error = None
        self._sequences = {'control': 0, 'pcm': 0}
        self.control_counts = {}
        self._report_resync = threading.Event()
        self.stale_messages = 0
        self._pcm_metadata_key = None
        self._pcm_models = None
        self.stale_pcm = 0
        self.accepted_request = 0
        self.managed_recovery = False
        self._last_status = time.monotonic()
        self._last_pcm = self._last_status
        self.network_idle_at = 0.0
        self.alsa_retry = None
        self._startup_deadline = None
        self._initial_liveness_at = None
        self._startup_expired = False
        self._local_waiting = False
        self._item_start_ns = 0
        self._item_pcm_produced_ns = 0
        self._podcast_service = podcast_service
        self._podcast_database_path = podcast_database_path
        self._service_cleanup = None
        self._service_done = threading.Event()
        self._service_done.set()
        if source.type == 'PODCAST' and self._podcast_service is None:
            from .podcast_service import PodcastFeedService
            from ..models.configuration import PodcastConfig
            self._podcast_service = PodcastFeedService(PodcastConfig.model_validate(source.config),
                database_path=podcast_database_path, source_id=self.source_id)
        self._catalog = None
        if source.type == 'PODCAST':
            from .podcast_worker import ParentPodcast
            self._catalog = ParentPodcast(self._podcast_service)
        if source.type == 'LOCAL':
            from .local_worker import ParentCatalog
            self._catalog = ParentCatalog(source)

    @property
    def expected_media_wait(self):
        with self._lock:
            return self._local_waiting

    def alsa_retry_allows_wait(self, clock, buffering_timeout):
        """Only a completed device retry extends the PCM decision."""
        with self._lock:
            if (self.alsa_retry is None or not self.reconnect or self._stop.is_set()
                    or self._fatal_error or self._pcm_error or self._done.is_set()):
                return False
            completed, due = self.alsa_retry
            # A newer PCM sample consumes the marker. No update can move the
            # expiry for this completed cycle, including repeated heartbeats.
            if self._has_pcm and self._last_pcm > completed:
                return False
            return completed <= clock() < due + buffering_timeout

    def recovery_observation(self):
        with self._lock:
            state = self._status['state']
            if state == 'RUNNING' and self._item_start_ns:
                produced = (self._item_pcm_produced_ns if self.direct_runtime is None
                            else self.direct_progress.get('produced_ns', 0))
                if produced < self._item_start_ns:
                    # RUNNING can arrive before the matching PCM or direct heartbeat.
                    # Keep the existing startup limit on the unchanged PCM clock.
                    state = 'CONNECTING'
            return self._last_status, self._last_pcm, state, self._fatal_error, self._done.is_set() and self._service_done.is_set()

    @property
    def liveness_phase(self):
        with self._lock:
            return 'STARTUP' if self._initial_liveness_at is None else 'STEADY'

    def _startup_timed_out_locked(self, now):
        if (self._initial_liveness_at is None and self._startup_deadline is not None
                and now >= self._startup_deadline):
            self._startup_expired = True
        return self._startup_expired

    def status_timeout_reason(self, clock, runtime_timeout):
        """Atomic STARTUP/STEADY decision; never acquire a native lock.

        Latch startup expiry under the report lock so a late report cannot race
        retirement and turn this generation back into a healthy one.
        """
        with self._lock:
            now = clock()
            if self._stop.is_set():
                return None
            if self._startup_timed_out_locked(now):
                return 'Worker startup timeout'
            if self._initial_liveness_at is not None and now-self._last_status > runtime_timeout:
                return 'Worker status timeout'
            return None

    def _establish_liveness_locked(self, received_at):
        if self._initial_liveness_at is None:
            self._initial_liveness_at = received_at
            started = (self._startup_deadline-STARTUP_TIMEOUT
                       if self._startup_deadline is not None else received_at)
            log.info('source=%s generation=%s category=worker_liveness phase=STEADY startup_elapsed_s=%.3f',
                     self.source_id, self.generation, received_at-started)

    def fresh(self):
        from ..models.audio_profile_file import AudioProfileFile
        profile = self._create['profile']
        result = type(self)(self.source.model_copy(deep=True), output=self.output,
            profile=AudioProfileFile.model_validate(profile) if profile else None,
            podcast_service=self._podcast_service, podcast_database_path=self._podcast_database_path,
            direct_runtime=self.direct_runtime, telemetry_configured=self.telemetry_configured, **self._create['options'])
        result.audio_profile_id = self.audio_profile_id
        result._diagnostic_outage_since = getattr(self, '_diagnostic_outage_since', None)
        return result

    @property
    def fatal_error(self):
        with self._lock:
            return self._fatal_error

    @property
    def startup_pending(self):
        with self._lock:
            return self._thread is not None and (not self._prepared or
                (self.source.type in ('LOCAL', 'PODCAST') and not self._has_pcm))

    def snapshot(self):
        with self._lock:
            return deepcopy(self._status)

    def current_stream_title(self):
        """Read the immutable text from the latest accepted worker metadata."""
        with self._lock:
            return self._status.get('stream_title')

    def pcm_snapshot(self):
        with self._lock:
            staging = dict(self._staging)
            consumers = dict(self.direct_progress.get('consumers', {})) if self.direct_runtime else None
        return dict(staging=staging, consumers=consumers if consumers is not None else self.output.snapshot())

    def request_telemetry(self, enabled, hz=50.0):
        from ..config import audio_telemetry_hz
        request = (bool(enabled) and self.telemetry_configured, audio_telemetry_hz(hz))
        with self._lock:
            if request != self._telemetry_request:
                self._telemetry_request = request
                self._telemetry_revision += 1
                self._telemetry_cache = None

    def telemetry_snapshot(self):
        with self._lock:
            if (self._stop.is_set() or self._fatal_error or not self._telemetry_request[0]
                    or time.monotonic() - self._telemetry_at > .5
                    or (self._telemetry_cache and self._telemetry_cache.get('telemetry_available') and
                        not 0 <= time.monotonic_ns() - self._telemetry_cache.get('sample_monotonic_ns', 0) <= 500_000_000)):
                return dict(telemetry_available=False, generation=0)
            return deepcopy(self._telemetry_cache or dict(telemetry_available=False, generation=0))

    def probe_result(self):
        with self._lock:
            return self._probe.model_copy(deep=True)

    def update_source_identity(self, source):
        if source.type != self.source.type or getattr(source, 'id', 0) != self.source_id:
            raise ValueError('Cannot change worker source identity')
        self.source = source

    def start(self):
        if self._thread is None:
            with self._lock:
                self._status['state'] = 'CONNECTING'
                if self._startup_deadline is None:
                    self._startup_deadline = time.monotonic() + STARTUP_TIMEOUT
            # A late exit must retain its owner without holding interpreter exit
            # hostage. The registry and bounded atexit hook cover graceful exit.
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f'audiod-owner-{self.source_id}')
            with _owners_lock:
                if _exiting:
                    self._thread = None
                    raise DependencyUnavailable('Audio worker parent shutdown is terminal')
                _owners.add(self)
            try:
                self._thread.start()
            except RuntimeError as exc:
                # No owner thread/process exists to publish terminal completion.
                self._stop.set()
                self._fail(f'Could not start audio owner thread: {exc}')
                self._done.set()
                with _owners_lock:
                    _owners.discard(self)
                raise DependencyUnavailable(self.fatal_error) from exc
        if not self.reconnect:
            # Inactive probes run on the discovery executor, not global control.
            if not self._ready.wait(self.timeout_s + 1):
                raise DependencyUnavailable('Timed out waiting for worker PCM')
        self.check_startup()

    def check_startup(self):
        if self.managed_recovery and (self.fatal_error or self._stop.is_set()):
            raise SourcePending('Audio worker recovery pending')
        if self.fatal_error or self._done.is_set():
            raise SourceStartupFailed(self.fatal_error or 'Audio worker exited')
        if self._stop.is_set():
            raise DependencyUnavailable('Audio worker shutdown pending')
        if self.startup_pending:
            raise SourcePending('Audio worker preparation pending')

    def stop(self, *, retain_service=False):
        with self._lock:
            self._http_retry_evidence=None
            self._source_probe_cancelled = True
            self._source_probe = None
            self._source_probe_report = None
            self._source_probe_sent = None
        if self._podcast_service is not None and not retain_service and self._service_cleanup is None:
            self._service_done.clear()
            def cleanup():
                while True:
                    try:
                        self._podcast_service.close()
                        break
                    except DependencyUnavailable:
                        continue
                self._service_done.set()
            self._service_cleanup = threading.Thread(target=cleanup, daemon=True,
                name=f'podcast-close-{self.source_id}')
            try:
                self._service_cleanup.start()
            except RuntimeError:
                self._service_cleanup = None
                self._service_done.set()  # Next stop() retries starting cleanup.
                self._stop.set()
                raise DependencyUnavailable('Podcast cleanup thread pending')
        self._stop.set()
        with self._lock:
            self.output.clear()
        if self._thread is None:
            return
        if not self.reconnect:
            # Only the existing inactive-probe discovery executor uses this mode.
            self._done.wait(1.8)
        if not self._done.is_set() or not self._service_done.is_set():
            raise DependencyUnavailable('Audio worker shutdown pending; retry after reap')
        if self.process is not None and self.process.poll() is None:
            raise DependencyUnavailable('Audio worker could not be reaped; ownership retained')

    def _fail(self, message):
        with self._lock:
            if self._fatal_error:
                return  # Preserve the original categorized native failure on EOF.
            self._fatal_error = str(message)[:4096]
            if getattr(self, '_diagnostic_outage_since', None) is None:
                self._diagnostic_outage_since = time.monotonic()
            self._status = dict(state='ERROR', last_error=self._fatal_error, error_scope='INTERNAL')
            self._probe = SourceTestResult(ok=False, mocked=False, message=self._fatal_error)
            self.output.clear()
        self._ready.set()

    def _accept(self, channel, message, pcm=b''):
        """Validate before touching output/cache; called only by channel readers."""
        with self._lock:
            if (message['source_id'] != self.source_id or message['generation'] != self.generation
                    or message['sequence'] <= self._sequences[channel]):
                self.stale_messages += 1
                return
            self._sequences[channel] = message['sequence']
            if self._stop.is_set() or self._fatal_error:
                return
            kind, payload = message['kind'], message['payload']
            # Arrival is measured under the same lock as the expiry decision.
            # Only a fully validated status/heartbeat can commit this arrival.
            if kind in ('status', 'heartbeat'):
                received_at = time.monotonic()
                if (self._startup_timed_out_locked(received_at)
                        and not (kind == 'status' and payload.get('fatal_error') is not None)):
                    return
            if kind == 'diagnostic':
                from .diagnostic_events import accept
                accept(self, payload)
                return
            if kind == 'radio_state' and self.radio_url is not None:
                self.radio_url.update(payload)
                return
            if kind == 'telemetry':
                if set(payload) != {'revision','sample'} or type(payload['revision']) is not int:
                    raise ProtocolError('Invalid telemetry envelope')
                sample = payload['sample']
                if (not isinstance(sample, dict) or type(sample.get('telemetry_available')) is not bool
                        or type(sample.get('generation')) is not int or sample['generation'] < 0):
                    raise ProtocolError('Invalid telemetry sample')
                if payload['revision'] == self._telemetry_revision and self._telemetry_request[0]:
                    self._telemetry_cache = sample
                    self._telemetry_at = time.monotonic()
                return
            if channel == 'control':
                self.control_counts[kind] = self.control_counts.get(kind, 0) + 1
            if kind in ('heartbeat', 'direct_state'):
                from .worker_reporting import accept_report
                valid_liveness = accept_report(self, kind, payload)
                if kind == 'heartbeat' and valid_liveness:
                    self._establish_liveness_locked(received_at)
                return
            if kind == 'output_applied' and self.direct_runtime is not None:
                from .direct_pcm import validate_assignment
                assignment = validate_assignment(payload)
                ch = payload.get('channel', 0)
                if assignment == self._direct_intents.get(ch, (0, None)):
                    self.direct_applied_channels[ch] = assignment
                    if ch == 0:
                        self.direct_applied = assignment
                return
            if kind == 'direct_progress' and self.direct_runtime is not None:
                from .direct_pcm import accept_progress
                accept_progress(self, payload)
                return
            if channel == 'pcm':
                if self.direct_runtime is not None:
                    raise ProtocolError('PCM forbidden in direct mode')
                if kind != 'pcm' or set(payload) != {'produced_ns', 'format', 'status', 'probe'}:
                    raise ProtocolError('Invalid PCM header')
                produced = payload['produced_ns']
                if type(produced) is not int or produced <= 0:
                    raise ProtocolError('Invalid PCM time')
                # JSON integers can exceed float range. Compare integer ns so
                # even a bounded record with a huge timestamp is safely dropped.
                age_ns = time.monotonic_ns() - produced
                if not 0 <= age_ns <= int(PCM_MAX_AGE * 1_000_000_000):
                    self.stale_pcm += 1
                    return
                if payload['format'] != PCM_CAPS:
                    raise ProtocolError('Unsupported PCM format')
                key = getattr(payload, 'metadata_key', None)
                if key is not None and key == self._pcm_metadata_key:
                    status, probe = self._pcm_models
                else:
                    status = InputSourceStatus(source_id=self.source_id, **payload['status'])
                    probe = SourceTestResult.model_validate(payload['probe'])
                    if key is not None:
                        self._pcm_metadata_key, self._pcm_models = key, (status, probe)
                for key in METADATA:
                    self._status[key] = getattr(status, key)
                # A sequenced item's tail can arrive after its successor's
                # CONNECTING report. Its source thread owns playback state.
                if self._catalog is None:
                    self._status['state'] = 'RUNNING'
                    self._status['last_error'] = None
                else:
                    self._item_pcm_produced_ns = max(self._item_pcm_produced_ns, produced)
                self._probe = probe
                self.output.publish(pcm)
                self._last_pcm = time.monotonic()
                self._http_pcm_produced_at = max(getattr(self,'_http_pcm_produced_at',0),produced/1e9)
                self._has_pcm = True
                if getattr(self, '_diagnostic_outage_since', None) is not None:
                    from .diagnostic_events import audio_recovered
                    audio_recovered(self)
                if self.reconnect or (self._prepared and self._probe.ok):
                    self._ready.set()
            elif kind == 'accepted' and set(payload) == {'request'} and type(payload['request']) is int and payload['request'] >= 1:
                self.accepted_request = payload['request']
            elif kind == 'prepared' and payload == {'request': 1}:
                self._prepared = True
                if self.reconnect or (self._has_pcm and self._probe.ok):
                    self._ready.set()
            elif kind == ('podcast_played' if self.source.type == 'PODCAST' else 'local_request') and self._catalog is not None:
                self._catalog.accept(payload)
            elif kind == 'status' and set(payload) - ({'item_start_ns'} if self._catalog is not None else set()) == (
                    {'status', 'staging', 'fatal_error'} | ({'local_waiting'} if self._catalog is not None else set())):
                item_start_ns = payload.get('item_start_ns', 0)
                if type(item_start_ns) is not int or not 0 <= item_start_ns <= time.monotonic_ns():
                    raise ProtocolError('Invalid item start timestamp')
                if self._catalog is not None:
                    if type(payload['local_waiting']) is not bool:
                        raise ProtocolError('Invalid LOCAL waiting state')
                    waiting = payload['local_waiting']
                    if self._local_waiting and not waiting:
                        # A fresh playable queue gets its own bounded PCM grace.
                        self._last_pcm = time.monotonic()
                    self._local_waiting = waiting
                status = InputSourceStatus(source_id=self.source_id, **payload['status']).model_dump(mode='json')
                staging = PcmQueueStatus.model_validate(payload['staging']).model_dump()
                if payload['fatal_error'] is not None and not isinstance(payload['fatal_error'], str):
                    raise ProtocolError('Invalid fatal error')
                self._item_start_ns = item_start_ns
                self._staging = staging
                self._last_status = time.monotonic()
                if not self._startup_expired:
                    self._establish_liveness_locked(received_at)
                # Metadata only advances with its corresponding PCM record.
                for key in ('state', 'last_error', 'error_scope', 'reconnect_count'):
                    self._status[key] = status[key]
                if payload['fatal_error'] is not None:
                    self._fatal_error = payload['fatal_error'][:4096]
                    self._status = dict(state='ERROR', last_error=self._fatal_error,
                                        error_scope=status['error_scope'])
                    self._probe = SourceTestResult(ok=False, mocked=False, message=self._fatal_error)
                    self.output.clear()
                    self._ready.set()
            elif kind == 'terminated' and set(payload) == {'error', 'error_scope'}:
                if ((payload['error'] is not None and not isinstance(payload['error'], str))
                        or payload['error_scope'] not in ('SOURCE', 'INTERNAL')):
                    raise ProtocolError('Invalid termination event')
                self._fatal_error = (payload['error'] or 'Worker terminated')[:4096]
                self._status = dict(state='ERROR', last_error=self._fatal_error,
                                    error_scope=payload['error_scope'])
                self._probe = SourceTestResult(ok=False, mocked=False, message=self._fatal_error)
                self.output.clear()
                self._ready.set()
                self._stop.set()
            else:
                raise ProtocolError('Unexpected worker event')

    def set_output_lease(self, revision, lease, channel=0):
        with self._lock:
            from .direct_pcm import validate_assignment
            validate_assignment(dict(channel=channel, revision=revision, lease=lease))
            if revision <= self._direct_intents.get(channel, (0, None))[0] or self._stop.is_set():
                return
            # A queued pre-grant heartbeat can precede the child's attach.
            # Start the existing writer deadline at the first required grant,
            # without refreshing it when additional TXs join an active writer.
            if lease is not None and not any(v is not None for _, v in self._direct_intents.values()):
                self._direct_writer_required_since = time.monotonic()
            self._direct_intents[channel] = (revision, lease)
            if channel == 0:
                self._direct_intent = (revision, lease)

    def request_state(self):
        self._report_resync.set()

    def _read_pcm(self, sock):
        decoder = PcmDecoder()
        try:
            while not self._stop.is_set():
                try:
                    self._accept('pcm', *receive(sock, pcm_decoder=decoder))
                except socket.timeout:
                    continue
        except (OSError, EOFError, ValueError, TypeError) as exc:
            if not self._stop.is_set():
                with self._lock:
                    # Allow the independent control channel to deliver a queued
                    # categorized terminal error before reporting generic EOF.
                    self._pcm_error = (time.monotonic(), f'Audio PCM channel: {exc}')

    def _run(self):
        sockets = []
        reader = None
        try:
            if self._podcast_service is not None:
                self._podcast_service.start()
            control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            sockets.extend((control, child_control))
            pcm = child_pcm = None
            if self.direct_runtime is None:
                pcm, child_pcm = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
                sockets.extend((pcm, child_pcm))
            for sock in sockets:
                configure(sock)
            if self._stop.is_set():
                return
            command = [sys.executable, '-m', self.worker_module, '--control-fd', str(child_control.fileno())]
            inherited = (child_control.fileno(),)
            if child_pcm is not None:
                command += ['--pcm-fd', str(child_pcm.fileno())]
                inherited += (child_pcm.fileno(),)
            self.process = subprocess.Popen(command,
                pass_fds=inherited, close_fds=True,
                stdin=subprocess.DEVNULL)
            child_control.close()
            if child_pcm is not None:
                child_pcm.close()
            log.info('audiod source=%s generation=%s pid=%s started phase=STARTUP startup_deadline=%s',
                     self.source_id, self.generation, self.process.pid, self._startup_deadline)
            control.sendall(self._create_packet)
            if pcm is not None:
                reader = threading.Thread(target=self._read_pcm, args=(pcm,), daemon=True,
                                          name=f'audiod-receive-{self.source_id}')
                reader.start()
            request = 1
            while not self._stop.is_set():
                with self._lock:
                    probe = getattr(self, '_source_probe', None)
                    sent = getattr(self, '_source_probe_sent', None)
                if probe is not None and sent is None:
                    request += 1
                    sent_at = time.monotonic()
                    control.sendall(pack(envelope(self.source_id, self.generation, request, 'source_progress_request', probe)))
                    with self._lock:
                        if getattr(self, '_source_probe', None) is probe:
                            self._source_probe_sent = sent_at
                with self._lock:
                    telemetry = (self._telemetry_revision, self._telemetry_request)
                if telemetry[0] != self._telemetry_sent:
                    request += 1
                    control.sendall(pack(envelope(self.source_id, self.generation, request, 'telemetry_request',
                        dict(revision=telemetry[0], enabled=telemetry[1][0], hz=telemetry[1][1]))))
                    self._telemetry_sent = telemetry[0]
                if self.radio_url is not None:
                    for command in self.radio_url.drain():
                        request += 1
                        control.sendall(pack(envelope(self.source_id, self.generation, request, 'radio_command', command)))
                if self._report_resync.is_set():
                    self._report_resync.clear()
                    request += 1
                    control.sendall(pack(envelope(self.source_id, self.generation, request, 'state_request')))
                with self._lock:
                    pcm_error = self._pcm_error
                if pcm_error and time.monotonic() - pcm_error[0] > 0.3:
                    raise ProtocolError(pcm_error[1])
                with self._lock:
                    initial = self._initial_liveness_at
                    startup_expired = self._startup_timed_out_locked(time.monotonic())
                if startup_expired:
                    raise TimeoutError('Worker startup timeout')
                if not self._prepared and initial is not None and time.monotonic() >= initial+self.timeout_s+1:
                    raise TimeoutError('Audio worker preparation deadline expired')
                if self.direct_runtime is not None:
                    with self._lock:
                        intents = dict(self._direct_intents)
                    for ch, (revision, lease) in intents.items():
                        if revision > self._direct_sent_channels.get(ch, 0):
                            request += 1
                            control.sendall(pack(envelope(self.source_id, self.generation, request,
                                'output_lease', dict(channel=ch, revision=revision, lease=lease))))
                            self._direct_sent_channels[ch] = revision
                if self._catalog is not None:
                    data = self._catalog.poll()
                    if data is not None:
                        request += 1
                        control.sendall(pack(envelope(self.source_id, self.generation,
                            request, 'podcast_snapshot' if self.source.type == 'PODCAST' else 'local_response', data)))
                try:
                    self._accept('control', *receive(control))
                except socket.timeout:
                    if self.process.poll() is not None:
                        raise EOFError(f'Audio worker exit {self.process.returncode}')
                    continue
            try:
                control.sendall(pack(envelope(self.source_id, self.generation, request + 1, 'shutdown')))
            except OSError:
                pass
        except (OSError, EOFError, ValueError, TypeError, RuntimeError, DependencyUnavailable) as exc:
            if not self._stop.is_set():
                self._fail(exc)
        finally:
            self._stop.set()
            try:
                if self.process is not None:
                    self._reap_child(sockets, reader)
                if self._catalog is not None:
                    self._catalog.close()
            finally:
                self._close_transport(sockets, reader)
            with self._lock:
                self.output.clear()
                if not self._fatal_error:
                    self._status = dict(state='IDLE', last_error=None, error_scope='SOURCE')
            self._done.set()
            self._ready.set()
            with _owners_lock:
                _owners.discard(self)

    @staticmethod
    def _close_transport(sockets, reader):
        for sock in sockets:
            try:
                sock.close()
            except Exception:
                log.exception('audiod socket cleanup failed')
        if reader is not None and reader.ident is not None:
            try:
                reader.join(timeout=0.3)
            except Exception:
                log.exception('audiod reader cleanup failed')

    def _reap_child(self, sockets, reader):
        """Keep this same owner until wait succeeds; no late-owner handoff.

        Each wait is bounded; the late loop is intentionally not bounded in
        total while the parent lives. It runs only on a daemon owner thread.
        Exceptions retain ownership and are retried, never treated as exit.
        """
        killed = False
        for action, grace in ((None, 0.3), ('terminate', 0.2), ('kill', 0.5)):
            if action:
                try:
                    getattr(self.process, action)()
                    killed = action == 'kill'
                except ProcessLookupError:
                    pass  # ESRCH is not wait/reap; still wait below.
                except Exception as exc:
                    self._fail(f'Audio worker signal error: {exc}')
            try:
                self.exit_code = self.process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                pass
            except Exception as exc:
                self._fail(f'Audio worker reap error: {exc}')
                if not getattr(self, '_diagnostic_reap_logged', False):
                    self._diagnostic_reap_logged = True
                    from .diagnostic_events import redact
                    log.error('source=%s generation=%s category=reap_failure reason=%s',
                              self.source_id, self.generation, redact(exc))
        self._fail('Audio worker survived KILL deadline; cleanup pending, owner retained')
        log.error('source=%s generation=%s category=reap_failure pid=%s KILL deadline exceeded; owner retained',
                  self.source_id, self.generation, self.process.pid)
        # Preserve the ordinary graceful protocol first, then retire transport
        # resources before a potentially very long late-exit wait.
        self._close_transport(sockets, reader)
        retry = threading.Event()
        while True:
            try:
                if not killed:
                    try:
                        self.process.kill()
                        killed = True
                    except ProcessLookupError:
                        pass
                self.exit_code = self.process.wait(timeout=0.2)
                return
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                # A failed wait/signal must not discard the process. Back off
                # even when the failing operation returns immediately.
                log.exception('audiod source=%s late reap failed; retaining owner', self.source_id)
                retry.wait(0.2)


class ExperimentalHttpFactory:
    """Opt-in HTTP routing only; every other source keeps GstPipeline unchanged."""
    mocked = False

    def __call__(self, source, **options):
        from .gst_pipeline import GstPipeline
        return (ProcessPipeline if source.type == 'HTTP' else GstPipeline)(source, **options)

    @staticmethod
    def validate_profile(profile):
        # Existing profile validation remains in-process until Phase 2B.
        from .gst_pipeline import GstPipeline
        GstPipeline.validate_profile(profile)


class ExperimentalAudioFactory(ExperimentalHttpFactory):
    """Explicit HTTP/LOCAL/PODCAST/ALSA/UDP/RTP opt-in; legacy HTTP-only option is unchanged."""
    def __call__(self, source, **options):
        from .gst_pipeline import GstPipeline
        return (ProcessPipeline if source.type in ('HTTP', 'LOCAL', 'PODCAST', 'ALSA', 'UDP', 'RTP') else GstPipeline)(source, **options)
