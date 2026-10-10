"""Runtime content selection shared by the API and RDS scheduler."""
import time
from collections.abc import Mapping
from ..models.configuration import Modulator, RtStatus
from ..models.runtime_status import InputSourceStatus


class RadioTextService:
    def __init__(self, database, sources, clock=time.monotonic):
        self.database, self.sources, self.clock = database, sources, clock
        self.overrides: dict[int, tuple[str, float | None]] = {}

    def set(self, modulator_id, override):
        self.database.get("modulators", modulator_id)
        expiry = None if override.ttl_s is None else self.clock() + override.ttl_s
        self.overrides[modulator_id] = (override.text, expiry)
        return self.effective(modulator_id)

    def clear(self, modulator_id):
        self.database.get("modulators", modulator_id)
        self.overrides.pop(modulator_id, None)

    def expire(self):
        now = self.clock()
        for key, (_, expiry) in list(self.overrides.items()):
            if expiry is not None and now >= expiry:
                self.overrides.pop(key, None)

    def effective(self, modulator_id, *, config: Modulator | None = None,
                  source_statuses: Mapping[int, InputSourceStatus] | None = None):
        """Select text using optional observations from this publication only.

        Missing inputs retain the ordinary lookup path, including its errors.
        Supplied models are read only and are never retained by this service.
        """
        if config is None:
            config = self.database.get("modulators", modulator_id)
        self.expire()
        if modulator_id in self.overrides:
            text, expiry = self.overrides[modulator_id]
            return RtStatus(text=text, origin="override", remaining_ttl_s=None if expiry is None else max(0, expiry - self.clock()))
        if config.rds_metadata_to_rt and config.input_source_id is not None:
            source = None if source_statuses is None else source_statuses.get(config.input_source_id)
            if source is None:
                source = self.sources.status(config.input_source_id)
            metadata = source.stream_title
            if metadata:
                return RtStatus(text=metadata[:64], origin="metadata")
        return RtStatus(text=config.rds_rt, origin="fallback")
