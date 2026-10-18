from .models.configuration import InputSourceConfig, SystemConfig, SystemSaveResult


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
        changes = patch.model_dump(exclude_unset=True)
        # A link/unlink can be changed independently of the saved stream URI.
        # Other config replacements retain the existing Source API semantics.
        if (current.type == 'HTTP' and changes.get('type', current.type) == 'HTTP'
                and isinstance(changes.get('config'), dict)
                and set(changes['config']) == {'radio_browser_uuid'}):
            changes['config'] = current.config | changes['config']
        config = model.model_validate(current.model_dump(exclude={"id", "timestamp", "rds_profile_id"}) | changes)
        if table == "sources":
            self.sources.validate_config(config)
        saved = self.database.save(table, config, record_id)
        result = self.sources.apply_saved_source(current, saved)
        if current.rds_profile != saved.rds_profile:
            self.modulators.apply_rds_profiles(self.database.rds_followers(record_id))
        return result

    def delete(self, table, record_id):
        self.database.delete(table, record_id)
        if table == "sources":
            self.sources.radio_recovery.invalidate(record_id)
            self.sources.statuses.pop(record_id, None)

    def get_system(self):
        return self.database.get_system()

    def update_system(self, patch):
        previous = self.get_system()
        config = SystemConfig.model_validate(previous.model_dump() | patch.model_dump(exclude_unset=True))
        changed = config.default_audio_profile_id != previous.default_audio_profile_id
        if 'default_audio_profile_id' in patch.model_fields_set:
            profile = self.sources.profile_files.get(config.default_audio_profile_id).profile
            self.sources.validate_profile(profile)
        self.database.save_system(config)
        errors = (self.sources.apply_default_profile(retry_failed_only=not changed)
                  if 'default_audio_profile_id' in patch.model_fields_set else {})
        return SystemSaveResult(**config.model_dump(), applied=not errors, restart_errors=errors)
