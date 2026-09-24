"""Scoped recovery on the existing control worker; reconnect is not a reset."""
import logging
import time
from .errors import DependencyUnavailable, DomainError
from .models.runtime_status import TxState
from .rp2354.client import TxConfig
from .rp2354.protocol import Command, IncompatibleProtocol, Outcome, RpResponseError, execution_outcome, PROTOCOL_VERSION

logger = logging.getLogger(__name__)


class Recovery:
    def __init__(self, modulators, rds, clock=time.monotonic):
        self.modulators, self.rds, self.rp = modulators, rds, modulators.rp
        self.clock = clock
        # Hardware synchronization and communication health are independent.
        self.ready = False
        self.retry_at = 0.0
        self.error = DependencyUnavailable('RP startup synchronization pending')
        self.uptime = None
        self.stopping = False
        self.pending = {}  # channel -> next idempotent reconciliation step
        self._startup_step = 0
        self._startup_enabled = {}  # Last positively acknowledged startup state.
        self._startup = [('info', None)] + [('off', i) for i in range(8)]
        for i in range(8):
            self._startup.extend([('config', i), ('clear', i)])
        self._startup.extend(('enable', i) for i in range(8))

    @property
    def retry_delay(self):
        return max(0.0, self.retry_at - self.clock(), getattr(self.rp, 'retry_delay', 0.0))

    def can_communicate(self):
        return not self.stopping and self.retry_delay == 0

    def communication_failed(self, error):
        """Coalesce bus failures; never invalidate QN state or discard RDS queues."""
        if isinstance(error, (RpResponseError, ValueError)):
            return
        if str(error) != str(self.error):
            logger.warning('RP control unavailable; retaining RF/audio state: %s', error)
        self.error = error
        # Real client owns its 250 ms backoff. Mocks share the same minimum.
        # Repeated calls during that interval do not extend it.
        if self.clock() >= self.retry_at:
            self.retry_at = self.clock() + max(.25, getattr(self.rp, 'retry_delay', 0.0))

    def require_ready(self):
        if not self.ready or self.stopping:
            raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', str(self.error or 'RP stopping'))

    def boundary(self, channel):
        self.rds.fence(channel)
        self.rp.clear_rds(channel)
        self.rds.reset_channel(channel)

    def command_failed(self, channel, command, error):
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

    def _apply(self, step, channel):
        if step == 'info':
            info = self.rp.get_info()
            if info['rp_protocol_version'] != PROTOCOL_VERSION or info['modulator_count'] != 8:
                raise IncompatibleProtocol('Unsupported RP protocol version or modulator count')
            return
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
                self.modulators.restore_audio(config)
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
        """Resume only explicitly uncertain TX writes, before normal refill."""
        if not self.ready or not self.can_communicate():
            return
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
            raise self.error or DependencyUnavailable('RP reconnect backoff')
        try:
            if not self.ready:
                return self._restore()
            status = self.rp.get_status()
            # V1 uptime wraps and can be corrupted; a decrease is not proof of reboot.
            # Valid mismatches/faults are reported, not globally replayed.
            self.uptime = status.get('uptime_ms')
            self.error = None
            return status
        except (DependencyUnavailable, DomainError, ValueError) as exc:
            error = exc if isinstance(exc, DependencyUnavailable) else DependencyUnavailable(str(exc))
            self.communication_failed(error)
            if isinstance(exc, RpResponseError):
                self.error = exc
                self.retry_at = self.clock() + .25
            raise error

    def _restore(self):
        # Startup only. Keep progress so a bad final read cannot repeat OFF/config.
        while self._startup_step < len(self._startup):
            self._apply(*self._startup[self._startup_step])
            self._startup_step += 1
        # A user can change desired state while the final observation is pending.
        # Revisit only targets whose confirmed startup write is now obsolete.
        for channel, enabled in list(self._startup_enabled.items()):
            if enabled != self.modulators.desired_on(channel):
                self._apply('enable', channel)
        observed = self.rp.get_status()
        if observed['state'] != 'OK' or any(
            c['tx_state'] != ('ON' if self.modulators.desired_on(c['id']) else 'OFF')
            for c in observed['modulators']
        ):
            raise DependencyUnavailable('RP startup read-back does not match desired TX state')
        self.ready = True
        self.error = None
        self.uptime = observed.get('uptime_ms')
        self.rds.suspended = False
        logger.info('RP startup hardware synchronization completed')
        return observed

    def shutdown(self):
        self.stopping = True
        self.rds.suspend()
        logger.info('fmheadendd shutdown: disabling RF; preserving desired configuration')
        # Explicit OFF pass, no recovery loop; never claim OFF after a failed ACK.
        for channel in range(8):
            try:
                self.rp.set_tx_enabled(channel, False)
            except DependencyUnavailable as exc:
                logger.warning('RF shutdown could not be confirmed: %s', exc)
                break
        for channel in range(8):
            self.modulators.release_audio(channel)
