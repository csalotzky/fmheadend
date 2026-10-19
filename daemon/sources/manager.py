import logging
import time
from threading import Lock
from ..errors import DependencyUnavailable, DomainError, SourceUnavailable
from ..models.configuration import InputSourceConfig, SourceTestResult
from ..models.runtime_status import InputSourceStatus, SourceState
from .pipeline import MockPipeline, probe
from .radio_recovery import SourceRadioRecovery
from .output import DEFAULT_STAGING_BUFFER_MS, DEFAULT_CONSUMER_BUFFER_MS, DEFAULT_STAGING_MAX_AGE_MS, pcm_capacity_bytes

logger = logging.getLogger(__name__)


class SourceManager:
    def __init__(self, database, pipeline_factory=MockPipeline, profile_files=None, *, clock=time.monotonic,
                 pcm_staging_buffer_ms=DEFAULT_STAGING_BUFFER_MS,
                 pcm_consumer_buffer_ms=DEFAULT_CONSUMER_BUFFER_MS,
                 pcm_staging_max_age_ms=DEFAULT_STAGING_MAX_AGE_MS):
        pcm_capacity_bytes(pcm_staging_buffer_ms)
        pcm_capacity_bytes(pcm_consumer_buffer_ms)
        pcm_capacity_bytes(pcm_staging_max_age_ms)
        self.pcm_staging_max_age_ms = pcm_staging_max_age_ms
        self.pcm_staging_buffer_ms = pcm_staging_buffer_ms
        self.pcm_consumer_buffer_ms = pcm_consumer_buffer_ms
        self.database = database
        self.pipeline_factory = pipeline_factory
        self.profile_files = profile_files
        self.pipelines = {}
        self.consumers: dict[int, set[int]] = {}
        # Writers remain control-owned. Telemetry readers capture references and
        # consumer IDs here; never hold this across pipeline or cache operations.
        self._registry_lock = Lock()
        self.statuses: dict[int, InputSourceStatus] = {}
        self.restart_errors: dict[int, str] = {}
        self.clock = clock
        self.radio_recovery = SourceRadioRecovery(self)
        self._profile_observations = {}
        self._telemetry_request = (False, 50.0)
        self._telemetry_lock = Lock()
        self._telemetry_closed = False

    def set_telemetry(self, enabled, hz=50.0):
        """Normal requests are control-owned; terminal disable may bypass control."""
        with self._telemetry_lock:
            self._telemetry_request = (enabled and not self._telemetry_closed, hz)
            with self._registry_lock:
                pipelines = list(self.pipelines.values())
            for pipeline in pipelines:
                if hasattr(pipeline, 'request_telemetry'):
                    pipeline.request_telemetry(*self._telemetry_request)

    def shutdown_telemetry(self):
        """Terminal Python-flag update: no native calls or control-queue wait.

        Serialize with in-flight enable requests; queued enables cannot resurrect
        metering. The lock never spans pipeline construction/start/stop or IO.
        """
        with self._telemetry_lock:
            self._telemetry_closed = True
        self.set_telemetry(False)

    def _publish_pipeline(self, source_id, pipeline, consumer_id=None):
        # Recheck telemetry at publication in case shutdown occurred while the
        # control worker was constructing this previously unregistered wrapper.
        with self._telemetry_lock:
            with self._registry_lock:
                self.pipelines[source_id] = pipeline
                if consumer_id is not None:
                    self.consumers[source_id].add(consumer_id)
            if hasattr(pipeline, 'request_telemetry'):
                pipeline.request_telemetry(*self._telemetry_request)

    def telemetry_snapshot(self):
        """Thread-safe registry copy, followed by wrapper-owned Python cache reads."""
        with self._registry_lock:
            registry = [(source_id, pipeline, sorted(self.consumers.get(source_id, ())))
                        for source_id, pipeline in self.pipelines.items()]
        records = []
        for source_id, pipeline, consumer_ids in registry:
            source = pipeline.source
            sample = (pipeline.telemetry_snapshot() if hasattr(pipeline, 'telemetry_snapshot')
                      else dict(telemetry_available=False, generation=0))
            # Applied identity was captured on the control thread at construction;
            # telemetry must not read SQLite to resolve the dynamic default.
            records.append(dict(source_id=source_id, source_name=source.name,
                audio_profile_id=pipeline.audio_profile_id,
                consumer_tx_ids=consumer_ids, **sample))
        return records

    def status(self, source_id, *, source=None, profile_errors=None):
        source = source if source is not None else self.database.get("sources", source_id)
        status = self.statuses.setdefault(source_id, InputSourceStatus(source_id=source_id))
        pipeline = self.pipelines.get(source_id)
        if pipeline is not None:
            error = self._observed_profile_error(source, profile_errors=profile_errors)
            if error:
                return InputSourceStatus(source_id=source_id, state=SourceState.ERROR, last_error=error, error_scope='INTERNAL')
        if source_id in self.restart_errors:
            return self.statuses[source_id]
        if pipeline is not None and not getattr(pipeline, "mocked", True):
            status = InputSourceStatus(source_id=source_id, **pipeline.snapshot(),
                                      pcm=pipeline.pcm_snapshot() if hasattr(pipeline, 'pcm_snapshot') else None)
            self.statuses[source_id] = status
        return status

    def _observed_profile_error(self, source, *, profile_errors=None):
        """Bound read-only validation work, including repeated metadata lookups.

        Only control-thread observations use this one-second cache. Starting,
        joining, testing and saving audio still validate synchronously. Runtime
        metadata and PCM counters are never taken from this validation cache.

        A publication may pass its own dictionary to share fresh validation
        outcomes by selection. It contains only immutable error strings/None,
        never profile models, and is not retained here. Existing per-source TTL
        hits must not seed it: another source may already need a fresh file read.
        """
        key = self.effective_profile_id(source)
        now = self.clock()
        previous = self._profile_observations.get(source.id)
        if previous is not None and previous[0] == key and 0 <= now - previous[1] < 1.0:
            return previous[2]
        if profile_errors is not None and key in profile_errors:
            error = profile_errors[key]
        else:
            error = None
            try:
                self.validate_config(source)
            except DomainError as exc:
                error = f'{exc.error}: {exc}'
            if profile_errors is not None:
                profile_errors[key] = error
        self._profile_observations[source.id] = (key, self.clock(), error)
        return error

    def _new_pipeline(self, source, **kwargs):
        try:
            profile = self.validate_config(source)
        except DomainError as exc:
            raise DependencyUnavailable(f'{exc.error}: {exc.message}') from exc
        if getattr(self.pipeline_factory, "mocked", True):
            pipeline = self.pipeline_factory(source)
            pipeline.audio_profile_id = self.effective_profile_id(source)
            return pipeline
        if source.type == 'PODCAST':
            kwargs['podcast_database_path'] = self.database.connection.execute('PRAGMA database_list').fetchone()[2]
        pipeline = self.pipeline_factory(source, profile=profile,
            pcm_staging_buffer_ms=self.pcm_staging_buffer_ms,
            pcm_staging_max_age_ms=self.pcm_staging_max_age_ms,
            pcm_consumer_buffer_ms=self.pcm_consumer_buffer_ms, **kwargs)
        pipeline.audio_profile_id = self.effective_profile_id(source)
        with self._telemetry_lock:
            if hasattr(pipeline, 'request_telemetry'):
                pipeline.request_telemetry(*self._telemetry_request)
        return pipeline

    def effective_profile_id(self, source):
        return (self.database.get_system().default_audio_profile_id
                if source.use_default_audio_profile else source.audio_profile_id)

    def validate_config(self, source):
        if self.profile_files is None:
            raise DomainError(503, 'PROFILE_STORAGE_ERROR', 'Profile storage is not configured')
        return self.profile_files.get(self.effective_profile_id(source)).profile

    def apply_default_profile(self, *, retry_failed_only=False):
        """Control-owned; only replace existing inheriting pipelines, once per Source."""
        self._profile_observations.clear()
        errors = {}
        for source_id in list(self.pipelines):
            if retry_failed_only and source_id not in self.restart_errors:
                continue
            source = self.database.get('sources', source_id)
            if source.use_default_audio_profile:
                error = self._restart_audio(source)
                if error:
                    errors[source_id] = error
        return errors

    def validate_profile(self, profile):
        if not getattr(self.pipeline_factory, 'mocked', True):
            try:
                self.pipeline_factory.validate_profile(profile)
            except (DependencyUnavailable, ValueError, TypeError) as exc:
                raise DomainError(503, 'PROFILE_PLUGIN_UNAVAILABLE', str(exc)) from exc

    def save_profile(self, path, data):
        """Control-executor operation; no TX/mux/RDS control commands."""
        from ..models.audio_profile_file import ProfileSaveResult
        profile = self.profile_files.validate(data)
        self.validate_profile(profile)
        saved = self.profile_files.save(path, data)
        self._profile_observations.clear()
        errors = {}
        for source_id in list(self.pipelines):
            source = self.database.get('sources', source_id)
            if self.effective_profile_id(source) != path:
                continue
            error = self._restart_audio(source)
            if error:
                errors[source_id] = error
        return ProfileSaveResult(**saved.model_dump(), applied=not errors, restart_errors=errors)

    def apply_saved_source(self, previous, saved):
        """Called after persistence on the control executor; retain fanout/consumers."""
        from ..models.configuration import SourceSaveResult
        self.radio_recovery.invalidate(saved.id)
        runtime_fields = ('type', 'config', 'use_default_audio_profile', 'input_gain_db',
                          'audio_profile_id', 'gst_options')
        changed = any(getattr(previous, key) != getattr(saved, key) for key in runtime_fields)
        rds_only = (previous.rds_profile != saved.rds_profile and
                    previous.model_dump(exclude={'rds_profile', 'rds_profile_id', 'timestamp'}) ==
                    saved.model_dump(exclude={'rds_profile', 'rds_profile_id', 'timestamp'}))
        needs_apply = not rds_only and (changed or saved.id in self.restart_errors)
        error = self._restart_audio(saved) if needs_apply and saved.id in self.pipelines else None
        return SourceSaveResult(**saved.model_dump(), applied=not error, apply_error=error)

    def _restart_audio(self, source):
        """One ownership-preserving audio replacement, shared by Source/profile Save."""
        source_id = source.id
        self.radio_recovery.invalidate(source_id)
        self._profile_observations.pop(source_id, None)
        old = self.pipelines[source_id]
        try:
            # Failed stop retains old ownership; never create a competing worker.
            old.stop()
            replacement = self._new_pipeline(source, output=getattr(old, 'output', None))
            self._publish_pipeline(source_id, replacement)
            replacement.start()
            self.restart_errors.pop(source_id, None)
            self.statuses[source_id] = InputSourceStatus(source_id=source_id,
                **({'state': SourceState.RUNNING} if getattr(replacement, 'mocked', True) else replacement.snapshot()))
            return None
        except Exception as exc:
            self.restart_errors[source_id] = str(exc)
            self.statuses[source_id] = InputSourceStatus(source_id=source_id, state=SourceState.ERROR, last_error=str(exc),
                error_scope='SOURCE' if isinstance(exc, SourceUnavailable) else 'INTERNAL')
            logger.error('Configuration saved; source %s audio restart failed: %s', source_id, exc,
                         exc_info=not isinstance(exc, SourceUnavailable))
            return str(exc)

    def acquire(self, source_id, modulator_id):
        source = self.database.get("sources", source_id)
        # Joining an existing pipeline must validate too: a live worker with old
        # settings is not proof that its selected file remains usable.
        try:
            self.validate_config(source)
        except DomainError as exc:
            self.statuses[source_id] = InputSourceStatus(source_id=source_id, state=SourceState.ERROR,
                                                        last_error=f'{exc.error}: {exc}', error_scope='INTERNAL')
            raise DependencyUnavailable(f'{exc.error}: {exc}') from exc
        with self._registry_lock:
            consumers = self.consumers.setdefault(source_id, set())
        self._profile_observations.pop(source_id, None)
        if modulator_id in consumers:
            return
        status = self.status(source_id)
        if not consumers:
            status.state = SourceState.CONNECTING
            pipeline = None
            try:
                pipeline = self._new_pipeline(source)
                # Track ownership before a potentially slow native start. If
                # teardown fails, a future acquire must not create a duplicate.
                self._publish_pipeline(source_id, pipeline, modulator_id)
                if not getattr(pipeline, "mocked", True):
                    pipeline.output.attach(modulator_id)
                pipeline.start()
            except Exception as exc:
                try:
                    if pipeline is not None:
                        self.release(source_id, modulator_id)
                    else:
                        with self._registry_lock:
                            self.consumers.pop(source_id, None)
                finally:
                    self.statuses[source_id] = InputSourceStatus(
                        source_id=source_id, state=SourceState.ERROR, last_error=str(exc),
                        error_scope='SOURCE' if isinstance(exc, SourceUnavailable) else 'INTERNAL')
                raise
            if getattr(pipeline, 'mocked', True):
                status.state, status.last_error = SourceState.RUNNING, None
            else:
                self.statuses[source_id] = InputSourceStatus(source_id=source_id, **pipeline.snapshot())
            self.restart_errors.pop(source_id, None)
            if getattr(pipeline, "mocked", True):
                status.codec = "MOCK"
                status.input_sample_rate, status.input_channels = 48000, 2
            logger.info("source %s started", source_id)
        else:
            pipeline = self.pipelines[source_id]
            error = self.restart_errors.get(source_id) or getattr(pipeline, 'fatal_error', None)
            if error:
                scope = self.status(source_id).error_scope
                raise (SourceUnavailable if scope == 'SOURCE' else DependencyUnavailable)(error)
            if not getattr(pipeline, "mocked", True):
                pipeline.output.attach(modulator_id)
        with self._registry_lock:
            consumers.add(modulator_id)

    def release(self, source_id, modulator_id):
        consumers = self.consumers.get(source_id)
        if not consumers:
            return
        if modulator_id not in consumers:
            return
        pipeline = self.pipelines[source_id]
        if len(consumers) == 1:
            self.radio_recovery.invalidate(source_id)
            # Retain ownership if native teardown fails; never replace a worker
            # that might still be alive with a second pipeline for this source.
            pipeline.stop()
            with self._registry_lock:
                self.pipelines.pop(source_id)
                self.consumers.pop(source_id)
            self.statuses[source_id] = InputSourceStatus(source_id=source_id)
            self.restart_errors.pop(source_id, None)
            logger.info("source %s stopped", source_id)
            self._profile_observations.pop(source_id, None)
        with self._registry_lock:
            consumers.discard(modulator_id)
        if not getattr(pipeline, "mocked", True):
            pipeline.output.detach(modulator_id)

    def test(self, source: InputSourceConfig):
        return self.prepare_test(source)()

    def prepare_test(self, source: InputSourceConfig):
        """Control-owned snapshot; returned probe never reads/writes manager or DB state.

        Results describe the submitted configuration, even if it is later edited
        or deleted. Active ingest is sampled now, never restarted by a probe.
        """
        source = source.model_copy(deep=True)
        if source.type == 'LOCAL':
            return lambda: self._test_local(source)
        # Profile lookup includes DB-backed reference callbacks; snapshot it on
        # the owner thread, along with the selected default and PCM settings.
        profile = self.validate_config(source)
        profile_id = self.effective_profile_id(source)
        factory = self.pipeline_factory
        options = dict(pcm_staging_buffer_ms=self.pcm_staging_buffer_ms,
                       pcm_staging_max_age_ms=self.pcm_staging_max_age_ms,
                       pcm_consumer_buffer_ms=self.pcm_consumer_buffer_ms,
                       reconnect=False)
        active = self.pipelines.get(getattr(source, "id", None))
        result = (active.probe_result() if active is not None and
                  not getattr(factory, 'mocked', True) else None)

        def run():
            if source.type == 'PODCAST':
                from ..models.configuration import PodcastConfig
                from .podcast_fetch import probe_podcast
                return probe_podcast(PodcastConfig.model_validate(source.config))
            if getattr(factory, 'mocked', True):
                return probe(source)
            if result is not None:
                return result
            pipeline = None
            try:
                pipeline = factory(source, profile=profile, **options)
                pipeline.audio_profile_id = profile_id
                pipeline.start()
                return pipeline.probe_result()
            except DependencyUnavailable as exc:
                return SourceTestResult(ok=False, mocked=False, message=str(exc))
            finally:
                if pipeline is not None:
                    pipeline.stop()
        return run

    @staticmethod
    def _test_local(source):
        if source.type == 'LOCAL':
            from ..models.configuration import LocalConfig
            from .local_media import resolve_media
            config = LocalConfig.model_validate(source.config)
            folder = config.mode == 'folder'
            count_field = 'local_file_count' if folder else 'playlist_entry_count'
            try:
                count = len(resolve_media(config.to_core()))
            except DomainError as exc:
                message = (('No supported audio files found.' if folder else 'Playlist contains no entries.')
                           if exc.error == 'NO_PLAYABLE_TRACKS' else f'{exc.error}: {exc.message}')
                return SourceTestResult(ok=False, mocked=False, message=message,
                                        **({count_field: 0} if exc.error == 'NO_PLAYABLE_TRACKS' else {}))
            return SourceTestResult(mocked=False, **{count_field: count},
                                    message='Discovery only; audio decoding was not tested.')

    def output_for(self, source_id):
        """Resolve on the control thread; the mux worker only retains this output."""
        pipeline = self.pipelines.get(source_id)
        return getattr(pipeline, "output", None)

    def close(self):
        self.radio_recovery.close()
        for pipeline in list(self.pipelines.values()):
            try:
                pipeline.stop()
            except DependencyUnavailable:
                logger.exception("source worker cleanup failed")
        with self._registry_lock:
            self.pipelines.clear()
            self.consumers.clear()
        self._profile_observations.clear()
