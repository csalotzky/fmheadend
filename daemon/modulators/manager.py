import logging
import time
import sqlite3
from ..diagnostics import error_scope
from ..errors import DependencyUnavailable, DomainError, SourceUnavailable, SourcePending, SourceStartupFailed
from ..models.configuration import ModulatorConfig, RDS_FIELDS
from ..models.runtime_status import ModulatorStatus, TxState
from ..rp2354.client import TxConfig
from ..rp2354.protocol import Command, Outcome, Status, RpResponseError, IncompatibleProtocol, execution_outcome, validate_frequency
from ..sources.direct_pcm import DirectMux

logger = logging.getLogger(__name__)


class ModulatorManager:
    def __init__(self, database, sources, rp, mux, settings):
        self.database, self.sources, self.rp, self.mux = database, sources, rp, mux
        self.settings = settings
        self.rds = None
        self.recovery = None
        # Keep revisions comparable across daemon restarts on the same boot.
        initial_revision = time.monotonic_ns() // 1_000
        self.statuses = {config.id: ModulatorStatus(id=config.id, intent_revision=initial_revision,
                                                  source_state=None if config.audio_mode == "ANALOG" else "IDLE")
                         for config in database.list("modulators")}
        self.active_sources: dict[int, int] = {}
        self.global_stopped = database.get_system().global_stopped
        self.bulk_stopped: set[int] = set(range(8)) if self.global_stopped else set()

    def list(self):
        return self.database.list("modulators")

    def get(self, modulator_id):
        return self.database.get("modulators", modulator_id)

    @property
    def physical_ids(self):
        info = self.recovery.verified_info if self.recovery else None
        return range(info['modulator_count'] if info else 0)

    def require_available(self, modulator_id):
        if self.recovery is None or self.recovery.verified_info is None:
            raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', 'Controller TX count has not been verified')
        if modulator_id not in self.physical_ids:
            raise DomainError(404, 'NOT_FOUND', f'Physical TX{modulator_id} is unavailable')
        if not self.recovery.availability_ready:
            raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', 'Controller availability synchronization pending')

    def public_get(self, modulator_id):
        self.require_available(modulator_id)
        return self.get(modulator_id)

    def validate(self, config):
        if config.frequency_khz is None:
            if config.enabled:
                raise DomainError(409, "FREQUENCY_REQUIRED", "Configure a transmitter frequency before enabling a modulator")
        else:
            info = self.recovery.verified_info if self.recovery else None
            if info is None:
                raise DomainError(503, "DEPENDENCY_UNAVAILABLE", "Controller frequency capabilities have not been verified")
            try:
                validate_frequency(config.frequency_khz, info)
            except ValueError as exc:
                raise DomainError(422, "INVALID_FREQUENCY", str(exc)) from exc
        if config.input_source_id is not None:
            self.database.get("sources", config.input_source_id)
        elif config.enabled and config.audio_mode == "DIGITAL":
            raise DomainError(409, "SOURCE_REQUIRED", "Assign an input source before enabling a modulator")

    def _edited_config(self, modulator_id, patch):
        old = self.get(modulator_id)
        config = ModulatorConfig.model_validate(old.model_dump(exclude={"id", "timestamp", "rds_override_profile_id"}) | patch.model_dump(exclude_unset=True))
        self.validate(config)
        self.database.effective_rds_profile(config.model_dump(), config.rds_override_profile)
        if config.enabled != old.enabled:
            raise DomainError(409, "LIFECYCLE_REQUIRED", "Use the explicit Start or Stop operation to change TX state")
        return config

    def _require_configurable(self, modulator_id):
        if self.recovery:
            self.recovery.require_ready()
            if modulator_id in self.recovery.pending:
                raise DomainError(409, 'MODULATOR_UNCERTAIN', 'Target TX reconciliation pending; OFF is not confirmed')
        if modulator_id in self.active_sources or self.statuses[modulator_id].tx_state != TxState.OFF:
            raise DomainError(409, 'MODULATOR_ACTIVE', 'Confirmed OFF and released source ownership are required before configuration changes')

    def apply_rds_profiles(self, channels):
        if self.rds:
            for channel in channels:
                if channel in self.physical_ids:
                    self.rds.profile_changed(channel)
            self.rds.tick()

    def _rds_only(self, old, config, patch):
        ignored = RDS_FIELDS | {'id', 'timestamp', 'rds_override_profile_id',
                                'rds_override_profile', 'rds_follow_source'}
        supplied = patch.model_dump(exclude_unset=True)
        if any(k not in ignored and value != getattr(old, k) for k, value in supplied.items()):
            return False
        return old.model_dump(exclude=ignored) == config.model_dump(exclude=ignored)

    def update(self, modulator_id, patch):
        self.require_available(modulator_id)
        old = self.get(modulator_id)
        config = self._edited_config(modulator_id, patch)
        if self._rds_only(old, config, patch):
            saved = self.database.save('modulators', config, modulator_id)
            self._record_intent(modulator_id, 'SAVE')
            if old.model_dump(include=RDS_FIELDS) != saved.model_dump(include=RDS_FIELDS) or old.rds_follow_source != saved.rds_follow_source:
                self.apply_rds_profiles([modulator_id])
            return saved
        self._require_configurable(modulator_id)
        saved = self.database.save("modulators", config, modulator_id)
        self._record_intent(modulator_id, 'SAVE')
        self.statuses[modulator_id].source_state = None if config.audio_mode == "ANALOG" else "IDLE"
        if config.audio_mode != old.audio_mode:
            self.statuses[modulator_id].input_saturated = None
        return saved

    def save_and_resume(self, modulator_id, patch):
        """One control-executor operation; temporary OFF never changes desired enabled."""
        self.require_available(modulator_id)
        config = self._edited_config(modulator_id, patch)  # Validate before RF action.
        old = self.get(modulator_id)
        if self._rds_only(old, config, patch):
            saved = self.update(modulator_id, patch)
            return dict(saved=True, stop_confirmed=False, restarted=False, failed_stage=None,
                        message='RDS configuration saved; RF and audio unchanged.', configuration=saved)
        if self.recovery:
            self.recovery.require_ready()
            if modulator_id in self.recovery.pending:
                raise DomainError(409, 'MODULATOR_UNCERTAIN', 'Target TX reconciliation pending')
        resume = self.statuses[modulator_id].tx_state == TxState.ON and self.desired_on(modulator_id)
        ignored = {'id', 'timestamp', 'rds_override_profile_id', 'input_source_id'}
        if (resume and isinstance(self.mux, DirectMux) and old.audio_mode == 'DIGITAL'
                and old.input_source_id != config.input_source_id
                and old.model_dump(exclude=ignored) == config.model_dump(exclude=ignored)):
            return self._save_source_assignment(modulator_id, old.input_source_id, config)
        if not resume:
            self._require_configurable(modulator_id)
        result = dict(saved=False, stop_confirmed=not resume, restarted=False,
                      failed_stage=None, message='', configuration=None)
        stage = 'stop' if resume else 'save'
        retry_budget = [1]
        try:
            if resume:
                # Also fences reconciliation after an ambiguous temporary OFF.
                # On failure an explicit Start/Start All may release this hold.
                self.bulk_stopped.add(modulator_id)
                self._record_intent(modulator_id, 'SAVE')
                stopped = self._stop(modulator_id, retry_budget)
                result['stop_confirmed'] = True
                stage = 'save'
                if stopped.last_error:
                    raise DomainError(503, 'CLEANUP_PENDING', stopped.last_error)
            result['configuration'] = self.update(modulator_id, patch)
            result['saved'] = True
            if resume:
                stage = 'start'
                self.bulk_stopped.discard(modulator_id)  # Only our temporary hold.
                started = self._start(modulator_id, retry_budget)
                result['restart_pending'] = started.start_pending
                result['restarted'] = not started.start_pending
                if started.start_pending:
                    result['intent_revision'] = started.intent_revision
                    result['message'] = 'Configuration saved — Preparing audio…'
                    return result
            result['message'] = 'Configuration saved and TX restarted.' if resume else 'Configuration saved; TX remains stopped.'
        except (DomainError, DependencyUnavailable, ValueError, sqlite3.Error, OSError) as exc:
            result['failed_stage'] = stage
            prefix = {'stop': 'STOP failed; configuration not saved; RF OFF is not confirmed',
                      'save': 'Saving failed; TX is stopped; configuration not saved',
                      'start': 'Configuration saved, but TX restart failed'}[stage]
            result['message'] = f'{prefix}: {exc}'
            logger.warning('TX%s save-and-resume: %s', modulator_id, result['message'])
        return result

    def _save_source_assignment(self, channel, old_source, config):
        source = config.input_source_id
        result = dict(saved=False, stop_confirmed=False, restarted=False,
                      failed_stage='start', configuration=None)
        if (self.statuses[channel].hardware_tx_state != 'ON' or not self.recovery.capability_current
                or self.active_sources.get(channel) != old_source):
            return dict(result, message='The existing RF/audio assignment is not confirmed; source not changed.')
        try:
            try:
                self.sources.acquire(source, channel)
            except SourcePending:
                # Startup may retain the consumer without producing audio yet.
                # Cleanup of an earlier generation may instead leave no owner.
                if channel not in self.sources.consumers.get(source, ()):
                    raise
            result['failed_stage'] = 'save'
            saved = self.database.save('modulators', config, channel)
            self._record_intent(channel, 'SAVE')
        except (DomainError, DependencyUnavailable, ValueError, sqlite3.Error, OSError) as exc:
            self.sources.release(source, channel)
            return dict(result, message=f'Source assignment not changed: {exc}')

        # The control executor serializes this with STOP and subsequent saves.
        # DirectMux revokes the previous lease before granting the saved source.
        self.active_sources[channel] = source
        self.mux.select_source(channel, source)
        self.sources.release(old_source, channel)
        if self.rds:
            self.rds.profile_changed(channel)
        logger.info('TX%s changed audio source from %s to %s', channel, old_source, source)
        return dict(saved=True, stop_confirmed=False, restarted=False, failed_stage=None,
                    message='Source assignment saved. RF remains on.', configuration=saved)

    def start(self, modulator_id):
        self.require_available(modulator_id)
        current = self.get(modulator_id)
        config = ModulatorConfig.model_validate(current.model_dump(exclude={"id", "timestamp", "rds_override_profile_id"}) | {"enabled": True})
        self.validate(config)
        if self.global_stopped:
            try:
                self.database.select_single_modulator(modulator_id)
            except sqlite3.Error as exc:
                raise DomainError(503, "GLOBAL_STATE_SAVE_FAILED",
                                  "New TX selection could not be saved; command not accepted") from exc
            self.global_stopped = False
            self.bulk_stopped.clear()
        else:
            self._save_enabled(config, modulator_id)
            self.bulk_stopped.discard(modulator_id)
        self._record_intent(modulator_id, 'START')
        return self._start(modulator_id)

    def _save_enabled(self, config, modulator_id):
        try:
            self.database.save('modulators', config, modulator_id)
        except sqlite3.Error as exc:
            raise DomainError(503, 'TX_STATE_SAVE_FAILED', 'TX state could not be saved; command not accepted') from exc

    def _record_intent(self, channel, action):
        # A new user decision supersedes prior UI feedback. Recovery and
        # terminal startup failure still belong to the current decision.
        row = self.statuses[channel]
        row.intent_revision += 1
        row.intent_action = action
        row.start_error = None

    def lifecycle_status(self, status):
        return status.model_copy(update={'desired_on': self.desired_on(status.id)})

    def _lifecycle_command(self, command, operation, retry_budget):
        """One session reacquisition, only with proof the requested write never began."""
        try:
            return operation()
        except DependencyUnavailable as exc:
            failed_command = getattr(exc, 'command', None)
            session_failure = (
                failed_command == Command.GET_INFO or
                (failed_command == command and getattr(exc, 'stage', None) in ('open', 'backoff'))
            )
            if (not retry_budget[0] or not session_failure or
                    isinstance(exc, (RpResponseError, IncompatibleProtocol)) or
                    execution_outcome(exc, command) != Outcome.NOT_SENT):
                raise
            retry_budget[0] -= 1
            delay = max(getattr(self.rp, 'retry_delay', 0.0),
                        self.recovery.retry_delay if self.recovery else 0.0)
            # The existing shared client/coordinator backoff is 250 ms. Do not
            # bypass a longer hold or turn this into an unbounded request wait.
            if delay > 0.25:
                raise
            logger.info('%s not sent; reacquiring control session once', command.name)
            if delay:
                time.sleep(delay)
        return operation()  # Errors on the second attempt propagate unchanged.

    @staticmethod
    def _command_error(command, exc):
        if command is None:
            return str(exc)
        certainty = execution_outcome(exc, command)
        if certainty == Outcome.NOT_SENT:
            failed_command = getattr(exc, 'command', None)
            if failed_command is not None and not isinstance(exc, RpResponseError):
                return f'{command.name} not sent: control/session unavailable during {failed_command.name}: {exc}'
            return f'{command.name} not sent: {exc}'
        if certainty == Outcome.AMBIGUOUS:
            return f'{command.name} outcome unknown (AMBIGUOUS): {exc}'
        return str(exc)

    @staticmethod
    def _rf_outcome(command, exc):
        if command not in (Command.SET_TX_ENABLED, Command.SET_TX_CONFIG):
            return 'REJECTED'
        certainty = execution_outcome(exc, command)
        if certainty == Outcome.AMBIGUOUS or (isinstance(exc, RpResponseError) and
                certainty == Outcome.RESPONSE and exc.rp_status in (Status.QN_ERROR, Status.INTERNAL_ERROR)):
            return 'UNCERTAIN'
        return 'REJECTED'

    def _start(self, modulator_id, retry_budget=None):
        self.require_available(modulator_id)
        retry_budget = [1] if retry_budget is None else retry_budget
        if self.recovery:
            self.recovery.require_ready()
            if modulator_id in self.recovery.pending:
                raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', 'Target TX reconciliation pending')
        config = self.get(modulator_id)
        status = self.statuses[modulator_id]
        if status.tx_state == TxState.ON and (config.audio_mode == "ANALOG" or modulator_id in self.active_sources):
            return status
        if modulator_id in self.active_sources and not (
                self.recovery and modulator_id in self.recovery.audio_pending
                and self.active_sources[modulator_id] == config.input_source_id):
            stopped = self._stop(modulator_id, retry_budget)
            if stopped.last_error:
                raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', stopped.last_error)
        analog = config.audio_mode == "ANALOG"
        source = None if analog else self.database.get("sources", config.input_source_id)
        previous_state = status.tx_state
        status.tx_state, status.last_error = TxState.STARTING, None
        enable_attempted = False
        command = None
        try:
            if not analog:
                self.sources.acquire(source.id, modulator_id)
                self.active_sources[modulator_id] = source.id
                self.mux.attach(modulator_id, source.id)
            command = Command.SET_TX_CONFIG
            self._lifecycle_command(command, lambda: self.rp.set_tx_config(modulator_id, TxConfig.from_modulator(config)), retry_budget)
            if self.recovery:
                command = Command.RDS_CLEAR
                self._lifecycle_command(command, lambda: self.recovery.boundary(modulator_id), retry_budget)
            enable_attempted = True
            command = Command.SET_TX_ENABLED
            self._lifecycle_command(command, lambda: self.rp.set_tx_enabled(modulator_id, True), retry_budget)
        except (DependencyUnavailable, ValueError) as exc:
            if isinstance(exc, SourcePending):
                self.active_sources[modulator_id] = source.id
                status.tx_state = previous_state
                if self.recovery:
                    self.recovery.defer_audio(modulator_id, exc)
                    return status
                raise DomainError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
            status.start_pending, status.pending_message = False, None
            # An uncertain ON is retried as ON, never converted to an OFF cycle.
            rf_off = command is None or (not enable_attempted and execution_outcome(exc, command) != Outcome.AMBIGUOUS)
            if rf_off:
                self.mux.detach(modulator_id)
                if source is not None:
                    self.sources.release(source.id, modulator_id)
                self.active_sources.pop(modulator_id, None)
            rejected = isinstance(exc, RpResponseError) and exc.command == command
            source_failure = command is None and isinstance(exc, SourceUnavailable)
            status.tx_state = previous_state if source_failure else (TxState.ERROR if command is None or rejected else previous_state)
            status.last_error = None if source_failure else self._command_error(command, exc)
            status.last_error_scope = error_scope(exc)
            if source_failure:
                status.source_state, status.source_last_error = 'ERROR', str(exc)
                status.source_error_scope = 'SOURCE'
                if isinstance(exc, SourceStartupFailed):
                    self.fail_audio_start(modulator_id, exc)
                elif self.recovery:
                    self.recovery.defer_audio(modulator_id, exc)
            if self.recovery and command is not None:
                self.recovery.command_failed(modulator_id, command, exc)
            raise DomainError(503, "DEPENDENCY_UNAVAILABLE", status.last_error or str(exc),
                              rf_outcome=self._rf_outcome(command, exc),
                              intent_revision=status.intent_revision) from exc
        status.tx_state = TxState.ON
        status.start_pending, status.pending_message = False, None
        status.source_last_error = None
        if self.recovery:
            self.recovery.pending.pop(modulator_id, None)
            self.recovery.audio_pending.pop(modulator_id, None)
        status.source_state = None if analog else self.sources.statuses[source.id].state
        logger.info("modulator %s started at %s kHz", modulator_id, config.frequency_khz)
        return status

    def stop(self, modulator_id):
        self.require_available(modulator_id)
        current = self.get(modulator_id)
        config = ModulatorConfig.model_validate(current.model_dump(exclude={"id", "timestamp", "rds_override_profile_id"}) | {"enabled": False})
        self._save_enabled(config, modulator_id)
        self._record_intent(modulator_id, 'STOP')
        return self._stop(modulator_id)

    def _stop(self, modulator_id, retry_budget=None):
        self.require_available(modulator_id)
        retry_budget = [1] if retry_budget is None else retry_budget
        if self.recovery:
            self.recovery.require_ready()
        status = self.statuses[modulator_id]
        status.start_pending, status.pending_message = False, None
        try:
            self._lifecycle_command(Command.SET_TX_ENABLED, lambda: self.rp.set_tx_enabled(modulator_id, False), retry_budget)
        except DependencyUnavailable as exc:
            status.last_error = self._command_error(Command.SET_TX_ENABLED, exc)
            status.last_error_scope = error_scope(exc)
            if isinstance(exc, RpResponseError) and exc.command == Command.SET_TX_ENABLED:
                status.tx_state = TxState.ERROR
            if self.recovery:
                self.recovery.command_failed(modulator_id, Command.SET_TX_ENABLED, exc)
            raise DomainError(503, "DEPENDENCY_UNAVAILABLE", status.last_error,
                              rf_outcome=self._rf_outcome(Command.SET_TX_ENABLED, exc),
                              intent_revision=status.intent_revision) from exc
        # The CRC-protected ACK confirms RF OFF independently of cleanup.
        status.tx_state, status.last_error = TxState.OFF, None
        if self.rds is not None:
            self.rds.stop(modulator_id)
        if self.recovery:
            self.recovery.pending.pop(modulator_id, None)
            self.recovery.audio_pending.pop(modulator_id, None)
        cleanup_errors = []
        try:
            self.mux.detach(modulator_id)
        except (DependencyUnavailable, OSError) as exc:
            cleanup_errors.append(f'mux detach: {exc}')
        source_id = self.active_sources.get(modulator_id)
        if source_id is not None:
            try:
                self.sources.release(source_id, modulator_id)
            except (DependencyUnavailable, OSError) as exc:
                cleanup_errors.append(f'source release: {exc}')
            else:
                self.active_sources.pop(modulator_id, None)
        if modulator_id not in self.active_sources:
            status.source_state = None if self.get(modulator_id).audio_mode == "ANALOG" else "IDLE"
        if cleanup_errors:
            status.last_error = 'RF OFF confirmed; cleanup pending: ' + '; '.join(cleanup_errors)
            status.last_error_scope = 'TX'
            logger.warning('modulator %s: %s', modulator_id, status.last_error)
        logger.info("modulator %s stopped", modulator_id)
        return status

    def desired_on(self, channel, *, config=None):
        return (channel in self.physical_ids and channel not in self.bulk_stopped
                and (config if config is not None else self.get(channel)).enabled)

    def all(self, start):
        if start:
            self.require_available(0)
        # Persist accepted global intent before changing holds or issuing commands.
        # Per-TX enabled settings remain the existing resume selection.
        config = self.database.get_system().model_copy(update={"global_stopped": not start})
        try:
            self.database.save_system(config)
        except sqlite3.Error as exc:
            raise DomainError(503, "GLOBAL_STATE_SAVE_FAILED",
                              "Global control state could not be saved; command not accepted") from exc
        self.global_stopped = not start
        results = []
        retry_budget = [1]  # One extra reacquisition for the entire bulk request.
        if not start:
            # Recovery, including startup, must not undo STOP ALL.
            self.bulk_stopped.update(range(8))
        for modulator_id in self.physical_ids:
            if modulator_id not in self.physical_ids:
                continue  # An implicit handshake may have reduced capacity in this loop.
            try:
                config = self.get(modulator_id)
                if start:
                    if not config.enabled or config.frequency_khz is None or (config.audio_mode == "DIGITAL" and config.input_source_id is None):
                        results.append(self.lifecycle_status(self.statuses[modulator_id]))
                        continue
                    self.validate(config)
                    self.bulk_stopped.discard(modulator_id)
                    self._record_intent(modulator_id, 'START')
                    result = self._start(modulator_id, retry_budget)
                else:
                    self._record_intent(modulator_id, 'STOP')
                    result = self._stop(modulator_id, retry_budget)
                results.append(self.lifecycle_status(result))
            except DomainError as exc:
                if exc.rf_outcome is None:
                    exc.rf_outcome = 'REJECTED'
                results.append({"id": modulator_id, **exc.payload()})
        return results

    def shutdown(self):
        # Shutdown changes actual state only, preserving the desired boot state.
        for modulator_id in self.physical_ids:
            try:
                self._stop(modulator_id, [0])
            except DomainError:
                logger.exception("modulator %s shutdown failed", modulator_id)


    def restore_audio(self, config):
        self.require_available(config.id)
        if config.audio_mode == "ANALOG":
            self.release_audio(config.id)
            self.statuses[config.id].source_state = None
            return
        source = self.database.get('sources', config.input_source_id)
        existing = self.active_sources.get(config.id)
        if existing != source.id:
            self.release_audio(config.id)
        try:
            self.sources.acquire(source.id, config.id)
        except SourcePending:
            self.active_sources[config.id] = source.id
            raise
        self.active_sources[config.id] = source.id
        self.mux.attach(config.id, source.id)
        self.statuses[config.id].source_state = self.sources.statuses[source.id].state

    def fail_audio_start(self, channel, error):
        """Cancel terminal LOCAL startup without sending an RF enable command."""
        self.release_audio(channel)
        config = self.get(channel)
        self.database.save("modulators", ModulatorConfig.model_validate(
            config.model_dump(exclude={"id", "timestamp", "rds_override_profile_id"}) | {"enabled": False}), channel)
        if self.recovery:
            self.recovery.audio_pending.pop(channel, None)
        row = self.statuses[channel]
        row.start_pending, row.pending_message = False, None
        row.source_state, row.source_last_error, row.source_error_scope = 'ERROR', str(error), 'SOURCE'
        row.start_error = str(error)
        logger.error('TX%s source %s LOCAL startup failed; enable intent cleared: %s',
                     channel, config.input_source_id, error)

    def release_audio(self, channel):
        self.mux.detach(channel)
        source = self.active_sources.get(channel)
        if source is not None:
            try:
                self.sources.release(source, channel)
            except (DependencyUnavailable, OSError):
                logger.exception('source cleanup failed for channel %s', channel)
                return False  # Retain ownership so capacity cleanup can retry safely.
            self.active_sources.pop(channel, None)
        self.statuses[channel].source_state = None if self.get(channel).audio_mode == 'ANALOG' else 'IDLE'
        self.statuses[channel].source_last_error = None
        return True
