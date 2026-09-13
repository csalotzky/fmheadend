from .errors import DomainError
from .models.runtime_status import TxState
from .models.configuration import AudioProfileConfig, InputSourceConfig, SystemConfig


class ConfigurationService:
    def __init__(self, database, sources, modulators):
        self.database, self.sources, self.modulators = database, sources, modulators

    def list(self, table):
        return self.database.list(table)

    def get(self, table, record_id):
        return self.database.get(table, record_id)

    def create(self, table, config):
        return self.database.save(table, config)

    def update(self, table, record_id, patch):
        current = self.get(table, record_id)
        if table == "audio_profiles":
            source_ids = {source.id for source in self.database.list("sources")
                          if source.audio_profile_id == record_id}
            if any(modulator.input_source_id in source_ids
                   and self.modulators.statuses[modulator.id].tx_state == TxState.ON
                   for modulator in self.database.list("modulators")):
                raise DomainError(409, "PROFILE_ACTIVE", "Stop all transmitters using this profile before saving changes")
        model = {"sources": InputSourceConfig, "audio_profiles": AudioProfileConfig}[table]
        config = model.model_validate(current.model_dump(exclude={"id", "timestamp"}) | patch.model_dump(exclude_unset=True))
        if table == "sources":
            # Preserve explicit stop-before-edit semantics; apply changes on next start.
            if record_id in self.modulators.active_sources.values():
                raise DomainError(409, "SOURCE_ACTIVE", "Stop consumers before editing this source")
        return self.database.save(table, config, record_id)

    def delete(self, table, record_id):
        self.database.delete(table, record_id)
        if table == "sources":
            self.sources.statuses.pop(record_id, None)

    def get_system(self):
        return self.database.get_system()

    def update_system(self, patch):
        config = SystemConfig.model_validate(self.get_system().model_dump() | patch.model_dump(exclude_unset=True))
        return self.database.save_system(config)
