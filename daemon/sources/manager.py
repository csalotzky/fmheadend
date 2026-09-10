import logging
from ..errors import DependencyUnavailable, DomainError
from ..models.configuration import InputSourceConfig, SourceType
from ..models.runtime_status import InputSourceStatus, SourceState
from .pipeline import MockPipeline, probe

logger = logging.getLogger(__name__)


class SourceManager:
    def __init__(self, database, pipeline_factory=MockPipeline):
        self.database = database
        self.pipeline_factory = pipeline_factory
        self.pipelines = {}
        self.consumers: dict[int, set[int]] = {}
        self.statuses: dict[int, InputSourceStatus] = {}

    def status(self, source_id):
        self.database.get("sources", source_id)
        return self.statuses.setdefault(source_id, InputSourceStatus(source_id=source_id))

    def acquire(self, source_id, modulator_id):
        source = self.database.get("sources", source_id)
        if not source.enabled:
            raise DomainError(409, "SOURCE_DISABLED", "The assigned input source is disabled")
        if source.type == SourceType.ANALOG:
            return
        consumers = self.consumers.setdefault(source_id, set())
        if modulator_id in consumers:
            return
        status = self.status(source_id)
        if not consumers:
            status.state = SourceState.CONNECTING
            pipeline = self.pipeline_factory(source)
            try:
                pipeline.start()
            except DependencyUnavailable as exc:
                pipeline.stop()
                status.state, status.last_error = SourceState.ERROR, str(exc)
                raise
            self.pipelines[source_id] = pipeline
            status.state, status.last_error = SourceState.RUNNING, None
            status.codec = "MOCK"
            status.input_sample_rate, status.input_channels = 48000, 2
            logger.info("source %s started (mock)", source_id)
        consumers.add(modulator_id)

    def release(self, source_id, modulator_id):
        consumers = self.consumers.get(source_id)
        if not consumers:
            return
        consumers.discard(modulator_id)
        if not consumers:
            self.pipelines.pop(source_id).stop()
            self.consumers.pop(source_id)
            self.statuses[source_id] = InputSourceStatus(source_id=source_id)
            logger.info("source %s stopped (mock)", source_id)

    def test(self, source: InputSourceConfig):
        return probe(source)
