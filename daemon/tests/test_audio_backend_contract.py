"""Exercise the manager contract without a GstPipeline implementation."""
import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable, SourcePending, SourceStartupFailed
from fmheadendd.models.configuration import InputSourceConfig, SourceTestResult
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.pipeline import MediaPipeline
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_profiles import FakePipeline


class PublicBackend(FakePipeline):
    """Only public contract state; deliberately no _startup_pending or GI."""
    startup_pending = True
    fatal_error = None
    radio_url = None

    def start(self):
        super().start()
        self.check_startup()

    def check_startup(self):
        if self.fatal_error:
            raise SourceStartupFailed(self.fatal_error)
        if self.startup_pending:
            raise SourcePending('catalog pending')

    def pcm_snapshot(self):
        return dict(staging={}, consumers=self.output.snapshot())

    def snapshot(self):
        if self.fatal_error:
            return dict(state='ERROR', last_error=self.fatal_error, error_scope='SOURCE')
        return super().snapshot()

    def request_telemetry(self, enabled, hz=50.0):
        self.telemetry_request = enabled, hz

    def telemetry_snapshot(self):
        return dict(telemetry_available=False, generation=0)

    def probe_result(self):
        return SourceTestResult(ok=self.running, mocked=False)

    def update_source_identity(self, source):
        self.source = source


@pytest.fixture
def rig(tmp_path):
    db = Database(tmp_path / 'backend.db')
    source = db.save('sources', InputSourceConfig(name='Feed', type='HTTP',
                    config={'uri': 'https://example.test/live'}))
    manager = SourceManager(db, pipeline_factory=PublicBackend,
                            profile_files=profile_store(tmp_path))
    yield manager, source
    for backend in manager.pipelines.values():
        backend.fail_stop = False
    manager.close()
    db.close()


def test_public_pending_sharing_and_replacement_output_identity(rig):
    manager, source = rig
    with pytest.raises(SourcePending):
        manager.acquire(source.id, 0)
    backend = manager.pipelines[source.id]
    assert isinstance(backend, MediaPipeline)
    assert not hasattr(backend, '_startup_pending')
    assert manager.consumers[source.id] == {0}
    with pytest.raises(SourcePending):
        manager.acquire(source.id, 1)
    assert manager.pipelines[source.id] is backend
    assert manager.consumers[source.id] == {0, 1}
    backend.startup_pending = False
    manager.acquire(source.id, 0)
    output = manager.output_for(source.id)
    output.publish(b'\x01\x00\x02\x00')
    assert output.read(0) == output.read(1) == b'\x01\x00\x02\x00'
    manager.set_telemetry(True, 25)
    assert backend.telemetry_request == (True, 25)
    assert manager.telemetry_snapshot()[0]['consumer_tx_ids'] == [0, 1]
    # Replacement retains output and ownership even when its startup is pending.
    pending = manager._restart_audio(source)
    assert not pending.applied and pending.pending == 'catalog pending' and pending.error is None
    replacement = manager.pipelines[source.id]
    assert replacement is not backend and not backend.running
    assert replacement.output is output
    assert manager.consumers[source.id] == {0, 1}
    assert replacement.telemetry_request == (True, 25)
    manager.release(source.id, 0)
    assert replacement.running and manager.consumers[source.id] == {1}
    manager.release(source.id, 1)
    assert source.id not in manager.pipelines and not replacement.running


def test_failed_stop_retains_backend_output_and_consumers(rig):
    manager, source = rig
    with pytest.raises(SourcePending):
        manager.acquire(source.id, 0)
    backend = manager.pipelines[source.id]
    backend.fail_stop = True
    output = backend.output
    with pytest.raises(DependencyUnavailable, match='audio stop failed'):
        manager.release(source.id, 0)
    assert manager.pipelines[source.id] is backend
    assert manager.consumers[source.id] == {0}
    failure = manager._restart_audio(source)
    assert not failure.applied and failure.error == 'audio stop failed' and failure.pending is None
    assert manager.pipelines[source.id] is backend and backend.output is output


def test_public_pending_failure_preserved_on_release(rig):
    manager, source = rig
    with pytest.raises(SourcePending):
        manager.acquire(source.id, 0)
    backend = manager.pipelines[source.id]
    backend.fatal_error = 'discovery failed'
    with pytest.raises(SourceStartupFailed):
        manager.acquire(source.id, 1)
    # The pending backend's check_startup owns classification, not a concrete
    # implementation field read in the manager's shared-consumer branch.
    manager.release(source.id, 0)
    manager.release(source.id, 1)
    assert source.id not in manager.pipelines
    assert manager.statuses[source.id].state == 'ERROR'
    assert manager.statuses[source.id].last_error == 'discovery failed'
    assert manager.statuses[source.id].error_scope == 'SOURCE'


def test_gst_contract_observations_do_not_load_native_code(monkeypatch):
    def forbidden():
        raise AssertionError('observation attempted native initialization')
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst', forbidden)
    source = InputSourceConfig(name='Feed', type='HTTP', config={'uri': 'https://example.test/live'})
    backend = GstPipeline(source)
    backend.audio_profile_id = 'test-profile'
    assert isinstance(backend, MediaPipeline)
    assert backend.startup_pending is False
    backend._startup_pending = True
    assert backend.startup_pending is True
    backend._startup_pending = False
    output = backend.output
    snapshot = backend.snapshot()
    snapshot['state'] = 'ERROR'
    assert backend.snapshot()['state'] == 'IDLE'
    backend.request_telemetry(True, 25)
    assert backend.telemetry_snapshot()['telemetry_available'] is False
    assert backend.pcm_snapshot()['consumers'] == {}
    assert backend.probe_result().ok is False
    updated = source.model_copy(update={'config': {'uri': 'https://example.test/new'}})
    backend.update_source_identity(updated)
    assert backend.source is updated and backend.output is output
    assert backend._http_uri == source.config['uri']  # identity update is not reconfiguration
    assert backend._thread is None and backend._pipeline is None
