"""Experimental Linux HTTP/LOCAL/PODCAST backend; parent output and cached state.

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
from .audio_ipc import (PCM_MAX_AGE, ProtocolError, configure, envelope, pack, receive)
from .output import (PCM_CAPS, PcmOutput, DEFAULT_STAGING_BUFFER_MS,
                     DEFAULT_STAGING_MAX_AGE_MS, DEFAULT_CONSUMER_BUFFER_MS)

log = logging.getLogger(__name__)
METADATA = ('stream_title', 'title', 'artist', 'codec', 'bitrate_bps',
            'input_sample_rate', 'input_channels', 'output_caps')

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
    radio_url = None  # Radio Browser candidate/persistence IPC is deferred.
    audio_profile_id = ''
    worker_module = 'fmheadendd.sources.audiod'

    def __init__(self, source, *, profile=None, output=None, timeout_s=None,
                 reconnect=True, telemetry_configured=False,
                 pcm_staging_buffer_ms=DEFAULT_STAGING_BUFFER_MS,
                 pcm_staging_max_age_ms=DEFAULT_STAGING_MAX_AGE_MS,
                 pcm_consumer_buffer_ms=DEFAULT_CONSUMER_BUFFER_MS,
                 podcast_database_path=None, podcast_service=None):
        if source.type not in ('HTTP', 'LOCAL', 'PODCAST'):
            raise ValueError('audiod supports HTTP, LOCAL and PODCAST only')
        if sys.platform != 'linux':
            raise DependencyUnavailable('Experimental audiod requires Linux socketpair/pass_fds')
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
        self._pcm_error = None
        self._sequences = {'control': 0, 'pcm': 0}
        self.stale_messages = 0
        self.stale_pcm = 0
        self.accepted_request = 0
        self.managed_recovery = False
        self.recovery_exhausted = False
        self._last_status = time.monotonic()
        self._last_pcm = self._last_status
        self._local_waiting = False
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

    def recovery_observation(self):
        with self._lock:
            return self._last_status, self._last_pcm, self._status['state'], self._fatal_error, self._done.is_set() and self._service_done.is_set()

    def fresh(self):
        from ..models.audio_profile_file import AudioProfileFile
        profile = self._create['profile']
        result = type(self)(self.source.model_copy(deep=True), output=self.output,
            profile=AudioProfileFile.model_validate(profile) if profile else None,
            podcast_service=self._podcast_service, podcast_database_path=self._podcast_database_path,
            **self._create['options'])
        result.audio_profile_id = self.audio_profile_id
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

    def pcm_snapshot(self):
        with self._lock:
            staging = dict(self._staging)
        return dict(staging=staging, consumers=self.output.snapshot())

    def request_telemetry(self, enabled, hz=50.0):
        # Explicitly unavailable in this PoC; never pretend native meters exist.
        pass

    def telemetry_snapshot(self):
        return dict(telemetry_available=False, generation=0)

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
        if self.managed_recovery and not self.recovery_exhausted and (self.fatal_error or self._stop.is_set()):
            raise SourcePending('Audio worker recovery pending')
        if self.fatal_error or self._done.is_set():
            raise SourceStartupFailed(self.fatal_error or 'Audio worker exited')
        if self._stop.is_set():
            raise DependencyUnavailable('Audio worker shutdown pending')
        if self.startup_pending:
            raise SourcePending('Audio worker preparation pending')

    def stop(self, *, retain_service=False):
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
            if channel == 'pcm':
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
                status = InputSourceStatus(source_id=self.source_id, **payload['status'])
                if payload['format'] != PCM_CAPS:
                    raise ProtocolError('Unsupported PCM format')
                probe = SourceTestResult.model_validate(payload['probe'])
                for key in METADATA:
                    self._status[key] = getattr(status, key)
                self._status['state'] = 'RUNNING'
                self._status['last_error'] = None
                self._probe = probe
                self.output.publish(pcm)
                self._last_pcm = time.monotonic()
                self._has_pcm = True
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
            elif kind == 'status' and set(payload) == ({'status', 'staging', 'fatal_error'} |
                    ({'local_waiting'} if self._catalog is not None else set())):
                if self._catalog is not None:
                    if type(payload['local_waiting']) is not bool:
                        raise ProtocolError('Invalid LOCAL waiting state')
                    waiting = payload['local_waiting']
                    if self._local_waiting and not waiting:
                        # A fresh playable queue gets its own bounded PCM grace.
                        self._last_pcm = time.monotonic()
                    self._local_waiting = waiting
                status = InputSourceStatus(source_id=self.source_id, **payload['status']).model_dump(mode='json')
                self._staging = PcmQueueStatus.model_validate(payload['staging']).model_dump()
                self._last_status = time.monotonic()
                # Metadata only advances with its corresponding PCM record.
                for key in ('state', 'last_error', 'error_scope', 'reconnect_count'):
                    self._status[key] = status[key]
                if payload['fatal_error'] is not None:
                    if not isinstance(payload['fatal_error'], str):
                        raise ProtocolError('Invalid fatal error')
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

    def _read_pcm(self, sock):
        try:
            while not self._stop.is_set():
                try:
                    self._accept('pcm', *receive(sock))
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
            pcm, child_pcm = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            sockets.extend((pcm, child_pcm))
            for sock in sockets:
                configure(sock)
            if self._stop.is_set():
                return
            self.process = subprocess.Popen(
                [sys.executable, '-m', self.worker_module, '--control-fd', str(child_control.fileno()),
                 '--pcm-fd', str(child_pcm.fileno())],
                pass_fds=(child_control.fileno(), child_pcm.fileno()), close_fds=True,
                stdin=subprocess.DEVNULL)
            child_control.close()
            child_pcm.close()
            log.info('audiod source=%s generation=%s pid=%s started', self.source_id, self.generation, self.process.pid)
            control.sendall(self._create_packet)
            reader = threading.Thread(target=self._read_pcm, args=(pcm,), daemon=True,
                                      name=f'audiod-receive-{self.source_id}')
            reader.start()
            deadline = time.monotonic() + self.timeout_s + 1
            request = 1
            while not self._stop.is_set():
                with self._lock:
                    pcm_error = self._pcm_error
                if pcm_error and time.monotonic() - pcm_error[0] > 0.3:
                    raise ProtocolError(pcm_error[1])
                if not self._prepared and time.monotonic() >= deadline:
                    raise TimeoutError('Audio worker startup deadline expired')
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
        self._fail('Audio worker survived KILL deadline; cleanup pending, owner retained')
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
    """Explicit HTTP/LOCAL/PODCAST opt-in; legacy HTTP-only option is unchanged."""
    def __call__(self, source, **options):
        from .gst_pipeline import GstPipeline
        return (ProcessPipeline if source.type in ('HTTP', 'LOCAL', 'PODCAST') else GstPipeline)(source, **options)
