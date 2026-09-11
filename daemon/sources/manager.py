import logging
from ..errors import DependencyUnavailable, DomainError
from ..models.configuration import InputSourceConfig, SourceType, SourceTestResult
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
        status = self.statuses.setdefault(source_id, InputSourceStatus(source_id=source_id))
        pipeline = self.pipelines.get(source_id)
        if pipeline is not None and not getattr(pipeline, "mocked", True):
            status = InputSourceStatus(source_id=source_id, **pipeline.snapshot())
            self.statuses[source_id] = status
        return status

    def _new_pipeline(self, source, **kwargs):
        if getattr(self.pipeline_factory, "mocked", True):
            return self.pipeline_factory(source)
        profile = self.database.get("audio_profiles", source.audio_profile_id) if source.audio_profile_id else None
        return self.pipeline_factory(source, profile=profile, **kwargs)

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
            pipeline = None
            try:
                pipeline = self._new_pipeline(source)
                # Track ownership before a potentially slow native start. If
                # teardown fails, a future acquire must not create a duplicate.
                self.pipelines[source_id] = pipeline
                consumers.add(modulator_id)
                if not getattr(pipeline, "mocked", True):
                    pipeline.output.attach(modulator_id)
                pipeline.start()
            except DependencyUnavailable as exc:
                try:
                    if pipeline is not None:
                        self.release(source_id, modulator_id)
                    else:
                        self.consumers.pop(source_id, None)
                finally:
                    self.statuses[source_id] = InputSourceStatus(
                        source_id=source_id, state=SourceState.ERROR, last_error=str(exc))
                raise
            status.state, status.last_error = SourceState.RUNNING, None
            if getattr(pipeline, "mocked", True):
                status.codec = "MOCK"
                status.input_sample_rate, status.input_channels = 48000, 2
            logger.info("source %s started", source_id)
        else:
            pipeline = self.pipelines[source_id]
            if not getattr(pipeline, "mocked", True):
                if pipeline.snapshot()["state"] != "RUNNING":
                    raise DependencyUnavailable("Shared source is not currently producing usable audio")
                pipeline.output.attach(modulator_id)
        consumers.add(modulator_id)

    def release(self, source_id, modulator_id):
        consumers = self.consumers.get(source_id)
        if not consumers:
            return
        if modulator_id not in consumers:
            return
        pipeline = self.pipelines[source_id]
        if len(consumers) == 1:
            # Retain ownership if native teardown fails; never replace a worker
            # that might still be alive with a second pipeline for this source.
            pipeline.stop()
            self.pipelines.pop(source_id)
            self.consumers.pop(source_id)
            self.statuses[source_id] = InputSourceStatus(source_id=source_id)
            logger.info("source %s stopped", source_id)
        consumers.discard(modulator_id)
        if not getattr(pipeline, "mocked", True):
            pipeline.output.detach(modulator_id)

    def test(self, source: InputSourceConfig):
        if getattr(self.pipeline_factory, "mocked", True):
            return probe(source)
        if source.type == SourceType.ANALOG:
            return SourceTestResult(mocked=False, message="Analog bypass selected; no GStreamer pipeline or hardware probe.")
        # Testing an active saved source reads its existing pipeline: no duplicate
        # ingest or ALSA capture. An inactive/unsaved probe is temporary.
        active = self.pipelines.get(getattr(source, "id", None))
        if active is not None:
            return active.probe_result()
        pipeline = None
        try:
            pipeline = self._new_pipeline(source, reconnect=False)
            pipeline.start()
            return pipeline.probe_result()
        except DependencyUnavailable as exc:
            return SourceTestResult(ok=False, mocked=False, message=str(exc))
        finally:
            if pipeline is not None:
                pipeline.stop()

    def output_for(self, source_id):
        """Resolve on the control thread; the mux worker only retains this output."""
        pipeline = self.pipelines.get(source_id)
        return getattr(pipeline, "output", None)

    def close(self):
        for pipeline in list(self.pipelines.values()):
            try:
                pipeline.stop()
            except DependencyUnavailable:
                logger.exception("source worker cleanup failed")
        self.pipelines.clear()
        self.consumers.clear()
