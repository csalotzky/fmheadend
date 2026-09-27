"""Runtime buffer sizes without GI, hardware or wall-clock timing."""
import sys
from types import SimpleNamespace

import pytest

from fmheadendd.app import argument_parser, create_app
from fmheadendd.config import Settings
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.output import PcmOutput, PcmQueue, pcm_capacity_bytes


def source():
    return InputSourceConfig(name='buffer test', type='HTTP', config={'uri': 'http://localhost/test'})


@pytest.mark.parametrize('duration,capacity', [(200, 38400), (7, 1344)])
def test_staging_capacity_does_not_change_age(duration, capacity):
    pipeline = GstPipeline(source(), pcm_staging_buffer_ms=duration)
    queue = pipeline.staging
    assert (queue.max_bytes, queue.max_age_s) == (capacity, .1)
    now = [0.0]
    queue.clock = lambda: now[0]
    assert queue.put(b'x' * capacity)
    assert pipeline._audio_ready.is_set()
    now[0] = .101
    assert queue.pop() is None
    assert queue.snapshot()['stale_dropped_bytes'] == capacity


def test_defaults_and_unchanged_general_queue():
    pipeline = GstPipeline(source())
    assert (pipeline.staging.max_bytes, pipeline.staging.max_age_s) == (38400, .1)
    assert (PcmQueue().max_bytes, PcmQueue().max_age_s) == (19200, .1)
    args = argument_parser().parse_args([])
    assert (args.pcm_staging_buffer_ms, args.pcm_consumer_buffer_ms) == (200, 300)


@pytest.mark.parametrize('duration', [300, 17])
def test_consumer_duration_controls_both_capacity_and_age(duration):
    output = PcmOutput() if duration == 300 else PcmOutput(duration)
    output.attach(0)
    output.attach(1)
    queue = output._queues[0]
    assert (queue.max_bytes, queue.max_age_s) == (duration * 192, duration / 1000)
    now = [0.0]
    for q in output._queues.values():
        q.clock = lambda: now[0]
    output.publish(b'a' * (duration * 192))
    assert output.read(1) == b'a' * (duration * 192)
    now[0] = duration / 1000 + .001
    assert output.read(0) is None
    assert output.snapshot()[1]['stale_dropped_bytes'] == 0


@pytest.mark.parametrize('flag', ['--pcm-staging-buffer-ms', '--pcm-consumer-buffer-ms'])
@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'abc', '1_000', str(sys.maxsize)])
def test_cli_invalid_buffer(flag, value):
    with pytest.raises(SystemExit) as exc:
        argument_parser().parse_args([flag, value])
    assert exc.value.code == 2


def test_cli_overrides_and_conversion_bounds():
    args = argument_parser().parse_args(['--pcm-staging-buffer-ms', '123', '--pcm-consumer-buffer-ms', '456'])
    assert (args.pcm_staging_buffer_ms, args.pcm_consumer_buffer_ms) == (123, 456)
    assert pcm_capacity_bytes(sys.maxsize // 192) <= sys.maxsize
    for value in (0, -1, True, 1.5, sys.maxsize // 192 + 1):
        with pytest.raises(ValueError):
            pcm_capacity_bytes(value)
        with pytest.raises(ValueError):
            Settings(pcm_staging_buffer_ms=value)
        with pytest.raises(ValueError):
            Settings(pcm_consumer_buffer_ms=value)


def test_manager_rebuild_preserves_output_and_buffer_configuration():
    manager = SourceManager(None, GstPipeline, pcm_staging_buffer_ms=123, pcm_consumer_buffer_ms=456)
    first = manager._new_pipeline(source())
    first.output.attach(0)
    replacement = manager._new_pipeline(source(), output=first.output)
    assert replacement.output is first.output
    assert replacement.staging.max_bytes == first.staging.max_bytes == 123 * 192
    assert replacement.output._queues[0].max_bytes == 456 * 192
    assert replacement.output._queues[0].max_age_s == .456


def test_application_passes_settings_to_source_manager(tmp_path):
    from fastapi.testclient import TestClient
    with TestClient(create_app(Settings(tmp_path / 'buffers.db', pcm_staging_buffer_ms=123,
                                       pcm_consumer_buffer_ms=456))) as client:
        manager = client.app.state.services.sources
        assert (manager.pcm_staging_buffer_ms, manager.pcm_consumer_buffer_ms) == (123, 456)
