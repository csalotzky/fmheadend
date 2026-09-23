import logging
from ..regions import HARDWARE_MIN_KHZ, HARDWARE_MAX_KHZ, HARDWARE_STEP_KHZ
from ..errors import DependencyUnavailable, DomainError
from ..models.configuration import ModulatorConfig
from ..models.runtime_status import ModulatorStatus, TxState
from ..rp2354.client import TxConfig
from ..rp2354.protocol import Command, Outcome, RpResponseError, execution_outcome

logger = logging.getLogger(__name__)


class ModulatorManager:
    def __init__(self, database, sources, rp, mux, settings):
        self.database, self.sources, self.rp, self.mux = database, sources, rp, mux
        self.settings = settings
        self.rds = None
        self.recovery = None
        self.statuses = {config.id: ModulatorStatus(id=config.id, source_state=None if config.audio_mode == "ANALOG" else "IDLE")
                         for config in database.list("modulators")}
        self.active_sources: dict[int, int] = {}
        self.bulk_stopped: set[int] = set()

    def list(self):
        return self.database.list("modulators")

    def get(self, modulator_id):
        return self.database.get("modulators", modulator_id)

    def validate(self, config):
        if config.frequency_khz is None:
            if config.enabled:
                raise DomainError(409, "FREQUENCY_REQUIRED", "Configure a transmitter frequency before enabling a modulator")
        elif not HARDWARE_MIN_KHZ <= config.frequency_khz <= HARDWARE_MAX_KHZ or config.frequency_khz % HARDWARE_STEP_KHZ:
            raise DomainError(422, "INVALID_FREQUENCY", "Frequency must be 76000..108000 kHz on the 50 kHz hardware raster")
        if config.input_source_id is not None:
            self.database.get("sources", config.input_source_id)
        elif config.enabled and config.audio_mode == "DIGITAL":
            raise DomainError(409, "SOURCE_REQUIRED", "Assign an input source before enabling a modulator")

    def update(self, modulator_id, patch):
        old = self.get(modulator_id)
        status = self.statuses[modulator_id]
        if old.enabled or modulator_id in self.active_sources or status.tx_state in (TxState.ON, TxState.STARTING):
            raise DomainError(409, "MODULATOR_ACTIVE", "Stop the transmitter before editing its configuration")
        config = ModulatorConfig.model_validate(old.model_dump(exclude={"id", "timestamp"}) | patch.model_dump(exclude_unset=True))
        self.validate(config)
        if config.enabled != old.enabled:
            raise DomainError(409, "LIFECYCLE_REQUIRED", "Use the explicit Start or Stop operation to change TX state")
        saved = self.database.save("modulators", config, modulator_id)
        if self.recovery and modulator_id in self.recovery.pending:
            # An edit supersedes any partially completed target reconciliation.
            self.recovery.pending[modulator_id] = 'off'
            self.rds.fence(modulator_id)
        self.statuses[modulator_id].source_state = None if config.audio_mode == "ANALOG" else "IDLE"
        if config.audio_mode != old.audio_mode:
            self.statuses[modulator_id].input_saturated = None
        return saved

    def start(self, modulator_id):
        current = self.get(modulator_id)
        config = ModulatorConfig.model_validate(current.model_dump(exclude={"id", "timestamp"}) | {"enabled": True})
        self.validate(config)
        self.database.save("modulators", config, modulator_id)
        self.bulk_stopped.discard(modulator_id)
        return self._start(modulator_id)

    def _start(self, modulator_id):
        if self.recovery:
            self.recovery.require_ready()
            if modulator_id in self.recovery.pending:
                raise DomainError(503, 'DEPENDENCY_UNAVAILABLE', 'Target TX reconciliation pending')
        config = self.get(modulator_id)
        status = self.statuses[modulator_id]
        if status.tx_state == TxState.ON and (config.audio_mode == "ANALOG" or modulator_id in self.active_sources):
            return status
        if modulator_id in self.active_sources:
            self._stop(modulator_id)
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
            self.rp.set_tx_config(modulator_id, TxConfig.from_modulator(config))
            if self.recovery:
                command = Command.RDS_CLEAR
                self.recovery.boundary(modulator_id)
            enable_attempted = True
            command = Command.SET_TX_ENABLED
            self.rp.set_tx_enabled(modulator_id, True)
        except (DependencyUnavailable, ValueError) as exc:
            # An uncertain ON is retried as ON, never converted to an OFF cycle.
            rf_off = command is None or (not enable_attempted and execution_outcome(exc, command) != Outcome.AMBIGUOUS)
            if rf_off:
                self.mux.detach(modulator_id)
                if source is not None:
                    self.sources.release(source.id, modulator_id)
                self.active_sources.pop(modulator_id, None)
            status.tx_state = TxState.ERROR if command is None or isinstance(exc, RpResponseError) else previous_state
            status.last_error = str(exc)
            if self.recovery and command is not None:
                self.recovery.command_failed(modulator_id, command, exc)
            raise DomainError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
        status.tx_state = TxState.ON
        if self.recovery:
            self.recovery.pending.pop(modulator_id, None)
        status.source_state = None if analog else "RUNNING"
        logger.info("modulator %s started at %s kHz", modulator_id, config.frequency_khz)
        return status

    def stop(self, modulator_id):
        current = self.get(modulator_id)
        config = ModulatorConfig.model_validate(current.model_dump(exclude={"id", "timestamp"}) | {"enabled": False})
        self.database.save("modulators", config, modulator_id)
        return self._stop(modulator_id)

    def _stop(self, modulator_id):
        if self.recovery:
            self.recovery.require_ready()
        status = self.statuses[modulator_id]
        try:
            self.rp.set_tx_enabled(modulator_id, False)
        except DependencyUnavailable as exc:
            status.last_error = str(exc)
            if isinstance(exc, RpResponseError):
                status.tx_state = TxState.ERROR
            if self.recovery:
                self.recovery.command_failed(modulator_id, Command.SET_TX_ENABLED, exc)
            raise DomainError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
        if self.rds is not None:
            self.rds.stop(modulator_id)
        if self.recovery:
            self.recovery.pending.pop(modulator_id, None)
        self.mux.detach(modulator_id)
        source_id = self.active_sources.get(modulator_id)
        if source_id is not None:
            try:
                self.sources.release(source_id, modulator_id)
            except DependencyUnavailable as exc:
                status.tx_state, status.last_error = TxState.ERROR, str(exc)
                raise DomainError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
            self.active_sources.pop(modulator_id, None)
        status.tx_state, status.source_state, status.last_error = TxState.OFF, (None if self.get(modulator_id).audio_mode == "ANALOG" else "IDLE"), None
        logger.info("modulator %s stopped", modulator_id)
        return status

    def desired_on(self, channel):
        return self.get(channel).enabled and channel not in self.bulk_stopped

    def all(self, start):
        results = []
        if not start:
            # Session-only operational hold; recovery must not undo STOP ALL.
            self.bulk_stopped.update(range(8))
        for modulator_id in range(8):
            try:
                config = self.get(modulator_id)
                if start:
                    if not config.enabled or config.frequency_khz is None or (config.audio_mode == "DIGITAL" and config.input_source_id is None):
                        results.append(self.statuses[modulator_id].model_dump(mode="json"))
                        continue
                    self.validate(config)
                    self.bulk_stopped.discard(modulator_id)
                    result = self._start(modulator_id)
                else:
                    result = self._stop(modulator_id)
                results.append(result.model_dump(mode="json"))
            except DomainError as exc:
                results.append({"id": modulator_id, "error": exc.error, "message": exc.message})
        return results

    def shutdown(self):
        # Shutdown changes actual state only, preserving the desired boot state.
        for modulator_id in range(8):
            try:
                self._stop(modulator_id)
            except DomainError:
                logger.exception("modulator %s shutdown failed", modulator_id)


    def restore_audio(self, config):
        if config.audio_mode == "ANALOG":
            self.release_audio(config.id)
            self.statuses[config.id].source_state = None
            return
        source = self.database.get('sources', config.input_source_id)
        existing = self.active_sources.get(config.id)
        if existing != source.id:
            self.release_audio(config.id)
            self.sources.acquire(source.id, config.id)
            self.active_sources[config.id] = source.id
        self.mux.attach(config.id, source.id)
        self.statuses[config.id].source_state = 'RUNNING'

    def release_audio(self, channel):
        self.mux.detach(channel)
        source = self.active_sources.pop(channel, None)
        if source is not None:
            try:
                self.sources.release(source, channel)
            except DependencyUnavailable:
                logger.exception('source cleanup failed for channel %s', channel)
        self.statuses[channel].source_state = None if self.get(channel).audio_mode == 'ANALOG' else 'IDLE'
