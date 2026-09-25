from .models.configuration import InputSourceConfig, SystemConfig


class ConfigurationService:
    def __init__(self, database, sources, modulators):
        self.database, self.sources, self.modulators = database, sources, modulators

    def list(self, table):
        return self.database.list(table)

    def get(self, table, record_id):
        return self.database.get(table, record_id)

    def create(self, table, config):
        if table == 'sources':
            self.sources.validate_config(config)
        return self.database.save(table, config)

    def update(self, table, record_id, patch):
        current = self.get(table, record_id)
        model = {"sources": InputSourceConfig}[table]
        config = model.model_validate(current.model_dump(exclude={"id", "timestamp"}) | patch.model_dump(exclude_unset=True))
        if table == "sources":
            self.sources.validate_config(config)
        saved = self.database.save(table, config, record_id)
        return self.sources.apply_saved_source(current, saved)

    def delete(self, table, record_id):
        self.database.delete(table, record_id)
        if table == "sources":
            self.sources.statuses.pop(record_id, None)

    def get_system(self):
        return self.database.get_system()

    def update_system(self, patch):
        config = SystemConfig.model_validate(self.get_system().model_dump() | patch.model_dump(exclude_unset=True))
        return self.database.save_system(config)
