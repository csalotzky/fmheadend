"""Scoped recovery on the existing control worker; reconnect is not a reset."""
import logging
import time
import sqlite3
from .diagnostics import error_scope
from .errors import DependencyUnavailable, DomainError, SourceUnavailable
from .models.runtime_status import TxState, ModulatorStatus
from .rp2354.client import TxConfig
from .rp2354.protocol import Command, IncompatibleProtocol, Outcome, RpResponseError, execution_outcome, PROTOCOL_VERSION, frequency_capabilities
from .rp2354.linux_client import Rp2354Client
from .mux.client import validate_stream_status

logger = logging.getLogger(__name__)


class Recovery:
    def __init__(self, modulators, rds, clock=time.monotonic):
        self.modulators, self.rds, self.rp = modulators, rds, modulators.rp
        self.clock = clock
        # Hardware synchronization and communication health are independent.
        self.ready = False
        self.retry_at = 0.0
        self.error = DependencyUnavailable('Controller startup synchronization pending')
        self.uptime = None
        self.stopping = False
        self.pending = {}  # channel -> next idempotent reconciliation step
        self.audio_pending = {}  # channel -> next local audio-start attempt
        self._audio_enabled = {}
        self._audio_config = {}
        self.hardware_error = None
        self.verified_info = None
        self.availability_ready = False
        self.capability_transition = False
        self.mux_retry_at = 0.0
        if isinstance(self.rp, Rp2354Client):
            self.rp.on_info = self._record_info
            self.rp.before_command = self._guard_command
        self._startup_step = 0
        self._startup_enabled = {}  # Last positively acknowledged startup state.
        self._startup = []  # Construct only after a verified GET_INFO.

    def _record_info(self, info):
        frequency_capabilities(info)
        count = info.get('modulator_count')
        if info.get('rp_protocol_version') != PROTOCOL_VERSION or type(count) is not int or not 1 <= count <= 8:
            raise IncompatibleProtocol('Unsupported Controller protocol version or modulator count')
        previous = self.verified_info['modulator_count'] if self.verified_info else None
        if previous is not None and previous != count:
            self.capability_transition = True
        self.verified_info = dict(info)
        self.availability_ready = False
        # Drop only unfinished work for absent hardware; retain completed progress.
        self._startup = [(step, channel) for step, channel in self._startup[self._startup_step:]
                         if channel < count]
        self._startup_step = 0
        added = range(count) if previous is None else range(previous, count)
        self._startup.extend(('off', channel) for channel in added)
        for channel in added:
            self._startup.extend([('config', channel), ('clear', channel)])
        self._startup.extend(('enable', channel) for channel in added)
        if self._startup:
            self.ready = False
        cleanup_pending = False
        for channel in range(count, 8):
            for pending in (self.pending, self.audio_pending, self._audio_enabled,
                            self._audio_config, self._startup_enabled):
                pending.pop(channel, None)
            self.rds.reset_channel(channel)
            self.rds.radiotext.overrides.pop(channel, None)
            cleanup_pending |= not self.modulators.release_audio(channel)
            self.modulators.statuses[channel] = ModulatorStatus(id=channel)
        try:
            self.modulators.database.disable_absent_modulators(count)
        except sqlite3.Error as exc:
            error = DependencyUnavailable(f'Physical TX availability could not be persisted: {exc}')
            error.outcome = Outcome.NOT_SENT
            raise error from exc
        if cleanup_pending:
            error = DependencyUnavailable('Removed TX audio cleanup pending')
            error.outcome = Outcome.NOT_SENT
            raise error
        self.availability_ready = True

    @property
    def capability_current(self):
        return (self.verified_info is not None and self.availability_ready and self.error is None and
                (not isinstance(self.rp, Rp2354Client) or self.rp.info == self.verified_info))

    def _guard_command(self, command, target):
        # Also runs after the client's implicit reconnect handshake, before I/O.
        if target != 0xff and (self.capability_transition or not self.availability_ready or
                              self.verified_info is None or target >= self.verified_info['modulator_count']):
            error = DependencyUnavailable('TX command blocked: unavailable target or capability coordination pending; not sent')
            error.outcome = Outcome.NOT_SENT
            raise error

    def reconcile_mux(self, status):
        """Existing control worker only; at most one mutation per status tick.

        No recurring GET_INFO: silent board replacement needs explicit reinitialization.
        """
        validate_stream_status(status)
        if not self.capability_current or self.stopping:
            return status
        count = self.verified_info['modulator_count']
        if status['stream_state'] == 'STREAMING' and status['tx_count'] == count:
            self.capability_transition = False
            return status
        if self.clock() < self.mux_retry_at:
            raise DependencyUnavailable('Mux configuration retry pending')
        mux = self.modulators.mux
        try:
            state = status['stream_state']
            if state == 'STREAMING':
                mux.pause()
                expected_state, expected_count = 'PAUSED', status['tx_count']
            elif status['tx_count'] != count:
                mux.set_tx_count(count)
                expected_state, expected_count = ('STREAMING' if state == 'WAITING_FOR_CONFIG' else 'PAUSED'), count
            else:
                mux.resume()
                expected_state, expected_count = 'STREAMING', count
            observed = mux.get_status()
            validate_stream_status(observed)
            if observed['stream_state'] != expected_state or observed['tx_count'] != expected_count:
                raise DependencyUnavailable('Mux acknowledgement does not match applied configuration')
            if observed['stream_state'] == 'STREAMING' and observed['tx_count'] == count:
                self.capability_transition = False
            return observed
        except DependencyUnavailable:
            self.mux_retry_at = self.clock() + 1.0
            # Observe ambiguous outcomes, but never report this failed attempt healthy.
            try:
                mux.get_status()
            except DependencyUnavailable:
                pass
            raise

    @property
    def retry_delay(self):
        return max(0.0, self.retry_at - self.clock(), getattr(self.rp, 'retry_delay', 0.0))

    def can_communicate(self):
        return not self.stopping and self.retry_delay == 0

    def communication_failed(self, error):
        """Coalesce bus failures; never invalidate TX state or discard RDS queues."""
        if isinstance(error, (RpResponseError, ValueError)):
            return
        if str(error) != str(self.error):
            logger.warning('Controller unavailable; retaining RF/audio state: %s', error)
        self.error = error
        # Real client owns its 250 ms backoff. Mocks share the same minimum.
        # Repeated calls during that interval do not extend it.
        if self.clock() >= self.retry_at:
            self.retry_at = self.clock() + max(.25, getattr(self.rp, 'retry_delay', 0.0))

    def require_ready(self):
        if not self.ready or self.stopping or not self.availability_ready or self.capability_transition:
            raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', str(self.error or 'Controller stopping'))

    def boundary(self, channel):
        self.rds.fence(channel)
        self.rp.clear_rds(channel)
        self.rds.reset_channel(channel)

    def command_failed(self, channel, command, error):
        if channel not in self.modulators.physical_ids:
            return  # A reconnect may have removed the target during this command.
        certainty = execution_outcome(error, command)
        if certainty == Outcome.AMBIGUOUS:
            if command == Command.SET_TX_CONFIG:
                self.pending[channel] = 'off'
                self.rds.fence(channel)
            elif command == Command.SET_TX_ENABLED:
                self.pending.setdefault(channel, 'enable')
            elif command == Command.RDS_CLEAR:
                self.rds.fence(channel)
        if isinstance(error, DependencyUnavailable):
            self.communication_failed(error)

    def expected_on(self, channel):
        # A failed initial audio open must not turn RF on. Runtime input loss
        # leaves an already enabled transmitter alone (mux supplies silence).
        return self.modulators.desired_on(channel) and (
            channel not in self.audio_pending or self._audio_enabled.get(channel, False))

    def defer_audio(self, channel, error, *, configured=None):
        if channel not in self.audio_pending:
            self._audio_enabled[channel] = self.modulators.statuses[channel].tx_state == TxState.ON
        self.audio_pending[channel] = self.clock() + 5.0
        self._audio_config[channel] = configured
        row = self.modulators.statuses[channel]
        row.source_state = 'ERROR'
        row.source_last_error = str(error)
        row.source_error_scope = 'SOURCE' if isinstance(error, SourceUnavailable) else 'INTERNAL'

    def _prepare_audio(self, config):
        try:
            self.modulators.restore_audio(config)
        except Exception as exc:
            # This boundary contains only source/mux work, never RP commands.
            # Unexpected failures remain visible system faults with traceback.
            self.defer_audio(config.id, exc, configured=TxConfig.from_modulator(config))
            if not isinstance(exc, SourceUnavailable):
                logger.exception('TX%s internal audio startup failure', config.id)
            return False
        self.audio_pending.pop(config.id, None)
        row = self.modulators.statuses[config.id]
        row.source_last_error, row.source_error_scope = None, 'SOURCE'
        return True

    def _apply(self, step, channel):
        if step == 'info':
            info = self.rp.get_info()
            self._record_info(info)
            return
        self._guard_command(Command.SET_TX_CONFIG, channel)
        config = self.modulators.get(channel)
        if step == 'off':
            self.rp.set_tx_enabled(channel, False)
        elif step == 'config':
            self.modulators.validate(config)
            if config.frequency_khz is not None:
                self.rp.set_tx_config(channel, TxConfig.from_modulator(config))
        elif step == 'clear':
            self.boundary(channel)
        elif step == 'enable':
            enabled = self.modulators.desired_on(channel)
            if enabled:
                if not self._prepare_audio(config):
                    return
            else:
                self.audio_pending.pop(channel, None)
            self.rp.set_tx_enabled(channel, enabled)
            if not self.ready:
                self._startup_enabled[channel] = enabled
            if not enabled:
                self.rds.stop(channel)
                self.modulators.release_audio(channel)
            row = self.modulators.statuses[channel]
            row.tx_state = TxState.ON if enabled else TxState.OFF
            row.last_error = None

    def service(self):
        """Retry local audio starts and explicitly uncertain TX writes separately."""
        if not self.ready or not self.availability_ready or self.capability_transition or not self.can_communicate():
            return
        for channel, retry_at in list(self.audio_pending.items()):
            if channel in self.pending or self.clock() < retry_at:
                continue
            self.audio_pending[channel] = self.clock() + 5.0
            try:
                if self.modulators.desired_on(channel):
                    config = TxConfig.from_modulator(self.modulators.get(channel))
                    if self._audio_config[channel] == config:
                        self._apply('enable', channel)
                    else:
                        # A failed explicit START may precede configuration;
                        # edits while waiting also require the normal lifecycle.
                        self.modulators._start(channel, retry_budget=[0])
                else:
                    self.audio_pending.pop(channel, None)
            except DependencyUnavailable as exc:
                self.command_failed(channel, Command.SET_TX_ENABLED, exc)
                self.modulators.statuses[channel].last_error = str(exc)
            except DomainError:
                pass  # _start already recorded the precise source/control fault.
            return  # Bounded retries; healthy RDS/status work runs between them.
        following = {'off': 'config', 'config': 'clear', 'clear': 'enable'}
        for channel in list(self.pending):
            try:
                while channel in self.pending:
                    step = self.pending[channel]
                    self._apply(step, channel)
                    if step == 'enable':
                        del self.pending[channel]
                    else:
                        self.pending[channel] = following[step]
            except (DependencyUnavailable, DomainError, ValueError) as exc:
                self.modulators.statuses[channel].last_error = str(exc)
                self.modulators.statuses[channel].last_error_scope = error_scope(exc)
                if isinstance(exc, (RpResponseError, DomainError, ValueError)):
                    # A valid rejection/local error is not permission to replay.
                    del self.pending[channel]
                else:
                    self.communication_failed(exc)
            # At most one TX lifecycle per tick; allow healthy RDS refill between
            # config operations. Bus failures are shared, not eight retries.
            return

    def observe(self):
        if not self.can_communicate():
            raise self.error or DependencyUnavailable('Controller reconnect backoff')
        try:
            if self.verified_info is None or (isinstance(self.rp, Rp2354Client) and self.rp.info is None):
                self._record_info(self.rp.get_info())
            elif not self.availability_ready:
                self._record_info(self.verified_info)
            if self.capability_transition:
                status = self.rp.get_status()
                self.error = None
                return status  # Mux reconciliation runs independently below observation.
            if not self.ready:
                try:
                    return self._restore()
                except (DomainError, ValueError) as exc:
                    # Local configuration errors must not falsify bus health.
                    self.hardware_error = f'Internal startup failure: {exc}'
                    logger.exception('Internal startup failure')
                    status = self.rp.get_status()
                    self.error = None
                    return status
            status = self.rp.get_status()
            # V1 uptime wraps and can be corrupted; a decrease is not proof of reboot.
            # Valid mismatches/faults are reported, not globally replayed.
            self.uptime = status.get('uptime_ms')
            self.error = None
            return status
        except DependencyUnavailable as exc:
            self.communication_failed(exc)
            if isinstance(exc, RpResponseError):
                self.error = exc
                self.retry_at = self.clock() + .25
            raise

    def _restore(self):
        # Startup only. Keep progress so a bad final read cannot repeat OFF/config.
        while self._startup_step < len(self._startup):
            self._apply(*self._startup[self._startup_step])
            self._startup_step += 1
        # A user can change desired state while the final observation is pending.
        # Revisit only targets whose confirmed startup write is now obsolete.
        for channel, enabled in list(self._startup_enabled.items()):
            if channel not in self.audio_pending and enabled != self.modulators.desired_on(channel):
                self._apply('enable', channel)
        observed = self.rp.get_status()
        if observed['state'] != 'OK' or any(
            c['tx_state'] != ('ON' if self.expected_on(c['id']) else 'OFF')
            for c in observed['modulators'] if c['id'] in self.modulators.physical_ids
        ):
            self.hardware_error = 'Controller startup read-back does not match expected TX state'
            self.error = None
            return observed  # A valid observation is not a communication failure.
        self.hardware_error = None
        self.ready = True
        self.error = None
        self.uptime = observed.get('uptime_ms')
        self.rds.suspended = False
        logger.info('Controller startup hardware synchronization completed')
        return observed

    def shutdown(self):
        self.stopping = True
        self.rds.suspend()
        logger.info('fmheadendd shutdown: disabling RF; preserving desired configuration')
        # Explicit OFF pass, no recovery loop; never claim OFF after a failed ACK.
        count = self.verified_info['modulator_count'] if self.verified_info else 0
        for channel in range(count):
            try:
                self.rp.set_tx_enabled(channel, False)
            except DependencyUnavailable as exc:
                logger.warning('RF shutdown could not be confirmed: %s', exc)
                break
        for channel in range(8):
            self.modulators.release_audio(channel)
