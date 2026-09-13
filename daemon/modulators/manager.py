import logging
from ..errors import DependencyUnavailable, DomainError
from ..models.configuration import ModulatorConfig
from ..models.runtime_status import ModulatorStatus, TxState
from ..rp2354.client import TxConfig

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

    def list(self):
        return self.database.list("modulators")

    def get(self, modulator_id):
        return self.database.get("modulators", modulator_id)

    def validate(self, config):
        if config.frequency_khz is None:
            if config.enabled:
                raise DomainError(409, "FREQUENCY_REQUIRED", "Configure a transmitter frequency before enabling a modulator")
        elif not self.settings.fm_band_min_khz <= config.frequency_khz <= self.settings.fm_band_max_khz:
            raise DomainError(422, "INVALID_FREQUENCY", "Frequency must be inside the configured FM band")
        if config.input_source_id is not None:
            self.database.get("sources", config.input_source_id)
        elif config.enabled and config.audio_mode == "DIGITAL":
            raise DomainError(409, "SOURCE_REQUIRED", "Assign an input source before enabling a modulator")

    def update(self, modulator_id, patch):
        old = self.get(modulator_id)
        config = ModulatorConfig.model_validate(old.model_dump(exclude={"id", "timestamp"}) | patch.model_dump(exclude_unset=True))
        self.validate(config)
        saved = self.database.save("modulators", config, modulator_id)
        self.statuses[modulator_id].source_state = None if config.audio_mode == "ANALOG" else "IDLE"
        if config.audio_mode != old.audio_mode:
            self.statuses[modulator_id].input_saturated = None
        # Commit desired config first; failed apply leaves it available for recovery.
        if modulator_id in self.active_sources or self.statuses[modulator_id].tx_state != TxState.OFF:
            self._stop(modulator_id)
            if config.enabled:
                self._start(modulator_id)
        elif config.enabled and "enabled" in patch.model_fields_set:
            self._start(modulator_id)
        return saved

    def start(self, modulator_id):
        current = self.get(modulator_id)
        config = ModulatorConfig.model_validate(current.model_dump(exclude={"id", "timestamp"}) | {"enabled": True})
        self.validate(config)
        self.database.save("modulators", config, modulator_id)
        return self._start(modulator_id)

    def _start(self, modulator_id):
        if self.recovery:
            self.recovery.require_ready()
        config = self.get(modulator_id)
        status = self.statuses[modulator_id]
        if status.tx_state == TxState.ON and (config.audio_mode == "ANALOG" or modulator_id in self.active_sources):
            return status
        if modulator_id in self.active_sources:
            self._stop(modulator_id)
        analog = config.audio_mode == "ANALOG"
        source = None if analog else self.database.get("sources", config.input_source_id)
        status.tx_state, status.last_error = TxState.STARTING, None
        enable_attempted = False
        rp_attempted = False
        try:
            if not analog:
                self.sources.acquire(source.id, modulator_id)
                self.active_sources[modulator_id] = source.id
                self.mux.attach(modulator_id, source.id)
            rp_attempted = True
            self.rp.set_tx_config(modulator_id, TxConfig.from_modulator(config))
            if self.recovery:
                self.recovery.boundary(modulator_id)
            enable_attempted = True
            self.rp.set_tx_enabled(modulator_id, True)
        except (DependencyUnavailable, ValueError) as exc:
            # A failed enable may have reached hardware. Keep audio if RF OFF
            # cannot be confirmed; failures before enable can release it safely.
            rf_off = not enable_attempted
            if enable_attempted:
                try:
                    self.rp.set_tx_enabled(modulator_id, False)
                    rf_off = True
                except DependencyUnavailable:
                    pass
            if rf_off:
                self.mux.detach(modulator_id)
                if source is not None:
                    self.sources.release(source.id, modulator_id)
                self.active_sources.pop(modulator_id, None)
            status.tx_state, status.last_error = TxState.ERROR, str(exc)
            if self.recovery and rp_attempted:
                self.recovery.lost(exc)
            raise DomainError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
        status.tx_state = TxState.ON
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
            status.tx_state, status.last_error = TxState.ERROR, str(exc)
            if self.recovery:
                self.recovery.lost(exc)
            raise DomainError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
        if self.rds is not None:
            self.rds.stop(modulator_id)
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

    def all(self, start):
        results = []
        for modulator_id in range(8):
            try:
                results.append((self.start if start else self.stop)(modulator_id).model_dump(mode="json"))
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
