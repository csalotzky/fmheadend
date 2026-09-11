"""Small RP lifecycle coordinator, called only on the existing control worker."""
import logging
import time
from .errors import DependencyUnavailable, DomainError
from .models.configuration import SourceType
from .models.runtime_status import TxState
from .rp2354.client import TxConfig
from .rp2354.protocol import IncompatibleProtocol, PROTOCOL_VERSION

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
            row.hardware_tx_state = row.rf_output_dbuv = None
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

    def observe(self):
        if not self.ready and self.clock() < self.retry_at:
            raise self.error
        try:
            if not self.ready:
                self._restore()
            status = self.rp.get_status()
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
            source = self.modulators.database.get('sources', config.input_source_id) if config.input_source_id is not None else None
            if config.frequency_khz is not None and source is not None:
                self.rp.set_tx_config(config.id, TxConfig.from_modulator(config, source.type == SourceType.ANALOG))
            self.boundary(config.id)
        for config in self.modulators.list():
            if config.enabled:
                self.modulators.restore_audio(config)
                self.rp.set_tx_enabled(config.id, True)
            else:
                self.modulators.release_audio(config.id)
        observed = self.rp.get_status()
        if observed['state'] != 'OK' or any(
            channel['tx_state'] != ('ON' if self.modulators.get(channel['id']).enabled else 'OFF')
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
