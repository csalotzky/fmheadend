"""Sequenced playback keeps one PCM-progress clock across item restarts."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from fmheadendd.sources.audio_ipc import ProtocolError, envelope
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.worker_recovery import WorkerRecovery
from fmheadendd.tests.test_audio_worker import PCM, adapter, pcm_message


def sequenced_pipeline():
    pipeline = GstPipeline.__new__(GstPipeline)
    pipeline._lock = threading.Lock()
    pipeline._uri_change_lock = threading.Lock()
    pipeline._snapshot = dict(state='RUNNING', title='previous')
    pipeline._snapshot_revision = 0
    pipeline._item_start_ns = 0
    pipeline._last_sample = 100.0
    pipeline._bus = Mock()
    pipeline._local = Mock(error='No playable media')
    pipeline._local.resume.return_value = 'file:///next.wav'
    pipeline.Gst = SimpleNamespace(State=SimpleNamespace(NULL=0, PLAYING=1),
                                   StateChangeReturn=SimpleNamespace(FAILURE=0))
    pipeline._native_state = Mock(return_value=1)
    pipeline._native_uri = Mock()
    pipeline._invalidate_telemetry = Mock()
    return pipeline


@pytest.mark.parametrize('source_type', ['LOCAL', 'PODCAST'])
def test_consecutive_item_starts_do_not_rearm_supervisor_pcm_deadline(source_type):
    pipeline = sequenced_pipeline()
    now = 110.0
    pcm_at = 100.0
    worker = SimpleNamespace(source=SimpleNamespace(type=source_type), _fail=Mock(), stop=Mock(),
        recovery_observation=lambda: (now, pcm_at, pipeline.snapshot()['state'], None, False))
    manager = SimpleNamespace(clock=lambda: now, pipelines={1: worker}, consumers={1: {0, 1}})
    supervisor = WorkerRecovery(manager)

    def accepted_start(state):
        if state == pipeline.Gst.State.PLAYING:
            assert pipeline.snapshot()['state'] == 'RUNNING'
        return 1

    pipeline._native_state.side_effect = accepted_start
    pipeline._local_resume(failed=True)
    assert pipeline._native_state.call_args_list == [call(0), call(1)]
    assert pipeline._last_sample == 0
    assert pipeline.snapshot()['title'] is None
    assert pipeline.snapshot()['state'] == 'CONNECTING'

    pipeline._native_state.side_effect = None
    for now in (115.001, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0, 190.0):
        pipeline._local_resume(failed=True)
        supervisor.service()
        worker._fail.assert_not_called()
    now = 190.001
    supervisor.service()
    worker._fail.assert_called_once_with('Worker PCM progress timeout')
    assert supervisor.attempts == {1: 1}


def test_rejected_start_does_not_publish_transition_progress():
    pipeline = sequenced_pipeline()
    pipeline._native_state.side_effect = (1, pipeline.Gst.StateChangeReturn.FAILURE)
    pipeline._local_resume(failed=True)
    assert pipeline.snapshot()['state'] == 'RUNNING'
    assert not pipeline._media_idle


def test_no_candidate_retains_existing_error_and_wait_path():
    pipeline = sequenced_pipeline()
    pipeline._local.resume.return_value = None
    pipeline._local_resume(failed=True)
    assert pipeline._media_idle
    assert pipeline.snapshot()['state'] == 'ERROR'
    assert pipeline.snapshot()['last_error'] == 'No playable media'
    pipeline._native_uri.assert_not_called()
    pipeline._native_state.assert_called_once_with(pipeline.Gst.State.NULL)


@pytest.mark.parametrize('source_type', ['LOCAL', 'PODCAST'])
@pytest.mark.parametrize('direct', [False, True], ids=['pcm1', 'direct'])
def test_source_state_before_matching_pcm_keeps_original_startup_limit(adapter, source_type, direct):
    adapter.source = SimpleNamespace(type=source_type)
    adapter._catalog = object()
    if direct:
        adapter.direct_runtime = 'unused'
    else:
        adapter.output.attach(0)
    item_start = time.monotonic_ns()
    anchor = adapter._last_pcm

    def status(state, sequence, marker=item_start, generation=adapter.generation):
        adapter._accept('control', envelope(adapter.source_id, generation, sequence, 'status',
            dict(status=dict(state=state), staging={}, fatal_error=None, local_waiting=False, item_start_ns=marker)))

    def pcm(produced, sequence):
        if direct:
            adapter._accept('control', envelope(adapter.source_id, adapter.generation, sequence, 'heartbeat',
                dict(blocks=sequence, bytes=sequence*len(PCM), produced_ns=produced, writer_at=time.monotonic())))
        else:
            adapter._accept('pcm', pcm_message(adapter, sequence, produced_ns=produced), PCM)

    status('CONNECTING', 1)
    assert adapter._last_pcm == anchor
    pcm(item_start-1, 2)
    assert adapter.snapshot()['state'] == 'CONNECTING'
    if not direct:
        assert adapter.snapshot()['stream_title'] == 'rendered'
        assert adapter.output.read(0) == PCM
    tail_anchor = adapter._last_pcm
    status('RUNNING', 3)
    assert adapter.snapshot()['state'] == 'RUNNING'
    assert adapter.recovery_observation()[1:3] == (tail_anchor, 'CONNECTING')
    status('RUNNING', 4, marker=0, generation='b'*32)
    assert adapter.recovery_observation()[1:3] == (tail_anchor, 'CONNECTING')
    with pytest.raises(ProtocolError, match='Invalid item start timestamp'):
        status('RUNNING', 4, marker=True)
    pcm(time.monotonic_ns(), 5)
    assert adapter._last_pcm >= tail_anchor
    assert adapter.recovery_observation()[2] == 'RUNNING'
