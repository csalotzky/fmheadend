"""Small RP lifecycle coordinator, called only on the existing control worker."""
import logging
import time
from .errors import DependencyUnavailable, DomainError
from .models.runtime_status import TxState
from .rp2354.client import TxConfig
from .rp2354.protocol import Command, IncompatibleProtocol, MalformedResponse, PROTOCOL_VERSION

logger = logging.getLogger(__name__)
RETRY_SECONDS = 2.0


class Recovery:
    def __init__(self, modulators, rds, clock=time.monotonic):
        self.modulators, self.rds, self.rp = modulators, rds, modulators.rp
        self.clock = clock
        self.ready = False
        self.retry_at = 0.0
        self.error = DependencyUnavailable('RP startup synchronization pending')
        self.uptime = None
        self.stopping = False
        self._announced = False

    def lost(self, error):
        if not isinstance(error, DependencyUnavailable):
            error = DependencyUnavailable(str(error))
        if self.ready or str(error) != str(self.error):
            logger.warning('RP unavailable/resynchronization failed: %s', error)
            self._announced = False
        self.ready = False
        self.error = error
        self.retry_at = self.clock() + RETRY_SECONDS
        self.rds.suspend()
        for row in self.modulators.statuses.values():
            row.tx_state = TxState.ERROR
            row.hardware_tx_state = row.rf_attenuation_db = row.input_saturated = None
            row.audio_underrun_count = row.rds_underrun_count = row.rds_fifo_free = None
            row.hardware_last_error = row.last_error = str(error)
        close = getattr(self.rp, 'close', None)
        if close:
            close()

    def require_ready(self):
        if not self.ready or self.stopping:
            raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', str(self.error))

    def boundary(self, channel):
        self.rp.clear_rds(channel)
        self.rds.reset_channel(channel)

    def _verify_malformed_read(self, error):
        """One fresh, read-only check. Never recurse or replay an ambiguous write.

        Called on the existing control worker, before lost()/RDS suspension.
        A valid new snapshot is mandatory; cached status is never returned.
        """
        if not self.ready or self.stopping or self.uptime is None or error.command not in (
            Command.GET_STATUS, Command.GET_RDS_FIFO_FREE
        ):
            raise error
        info = self.rp.get_info()
        if info['rp_protocol_version'] != PROTOCOL_VERSION or info['modulator_count'] != 8:
            raise IncompatibleProtocol('Unsupported RP protocol version or modulator count')
        status = self.rp.get_status()  # Failure escapes; no second verification loop.
        uptime = status.get('uptime_ms')
        if uptime is None or uptime < self.uptime:
            raise DependencyUnavailable('RP uptime decreased/missing during read verification; reset requires resynchronization')
        if status['state'] != 'OK':
            raise DependencyUnavailable('RP active hardware fault during read verification')
        for channel in status['modulators']:
            config = self.modulators.get(channel['id'])
            expected = 'ON' if self.modulators.desired_on(channel['id']) else 'OFF'
            if channel['tx_state'] != expected or (
                # An OFF channel may have edited configuration not applied yet.
                expected == 'ON' and
                channel['rf_attenuation_db'] != config.rf_attenuation_db
            ):
                raise DependencyUnavailable(f"RP TX{channel['id']} state/configuration mismatch during read verification")
        self.uptime = uptime
        logger.info('RP malformed %s cleared by fresh compatible status; retaining TX/RDS state', error.command.name)
        return status

    def verify_fifo_read(self, error):
        status = self._verify_malformed_read(error)
        return [channel['rds_fifo_free'] for channel in status['modulators']]

    def observe(self):
        if not self.ready and self.clock() < self.retry_at:
            raise self.error
        try:
            if not self.ready:
                self._restore()
            try:
                status = self.rp.get_status()
            except MalformedResponse as exc:
                status = self._verify_malformed_read(exc)
            uptime = status.get('uptime_ms')
            if self.uptime is not None and uptime is not None and uptime < self.uptime:
                raise DependencyUnavailable('RP uptime decreased; reset requires resynchronization')
            self.uptime = uptime
            return status
        except (DependencyUnavailable, DomainError, ValueError) as exc:
            error = exc if isinstance(exc, DependencyUnavailable) else DependencyUnavailable(str(exc))
            self.lost(error)
            raise error

    def _restore(self):
        info = self.rp.get_info()
        if info['rp_protocol_version'] != PROTOCOL_VERSION or info['modulator_count'] != 8:
            raise IncompatibleProtocol('Unsupported RP protocol version or modulator count')
        if not self._announced:
            logger.info('RP connected and compatible; hardware resynchronization started')
            self._announced = True
        self.uptime = None
        # Disable every physical channel before restoring any desired ON state.
        for channel in range(8):
            self.rp.set_tx_enabled(channel, False)
        for config in self.modulators.list():
            self.modulators.validate(config)
            if config.frequency_khz is not None:
                self.rp.set_tx_config(config.id, TxConfig.from_modulator(config))
            self.boundary(config.id)
        for config in self.modulators.list():
            if self.modulators.desired_on(config.id):
                self.modulators.restore_audio(config)
                self.rp.set_tx_enabled(config.id, True)
            else:
                self.modulators.release_audio(config.id)
        observed = self.rp.get_status()
        if observed['state'] != 'OK' or any(
            channel['tx_state'] != ('ON' if self.modulators.desired_on(channel['id']) else 'OFF')
            for channel in observed['modulators']
        ):
            raise DependencyUnavailable('RP restore read-back does not match desired TX state')
        self.ready = True
        self.error = None
        self.rds.suspended = False
        for row in self.modulators.statuses.values():
            row.last_error = row.hardware_last_error = None
        logger.info('RP hardware resynchronization completed')

    def shutdown(self):
        self.stopping = True
        self.rds.suspend()
        logger.info('fmheadendd shutdown: disabling RF; preserving desired configuration')
        # One bounded pass, no recovery/retry loop. On communication failure stop
        # issuing more commands; hardware OFF cannot then be guaranteed.
        for channel in range(8):
            try:
                self.rp.set_tx_enabled(channel, False)
            except DependencyUnavailable as exc:
                logger.warning('RF shutdown could not be confirmed: %s', exc)
                break
        for channel in range(8):
            self.modulators.release_audio(channel)
