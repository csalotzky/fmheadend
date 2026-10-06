"""Non-live buffering controls the shared player's clock, never source ownership."""
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline


def pipeline(kind='PODCAST'):
    config = {'feeds': [{'url': 'https://example.org/rss'}]} if kind == 'PODCAST' else (
        {'mode': 'folder', 'path': '/media'} if kind == 'LOCAL' else {'uri': 'https://example.org/live'})
    p = GstPipeline(InputSourceConfig(name='test', type=kind, config=config))
    p.Gst = SimpleNamespace(State=SimpleNamespace(PAUSED='PAUSED', PLAYING='PLAYING', NULL='NULL'),
                            StateChangeReturn=SimpleNamespace(FAILURE='FAILURE'))
    p._pipeline = Mock()
    return p


def test_pause_resume_idempotent_and_preserves_pcm_ownership():
    p = pipeline()
    p.output.attach(0)
    p.staging.put(bytes(1920))
    p.output.publish(bytes(1920))
    before = p.pcm_snapshot()
    p._buffering(0)
    p._buffering(40)
    assert p.snapshot()['state'] == 'BUFFERING'
    p._buffering(100)
    p._buffering(100)
    assert p._pipeline.set_state.call_args_list == [call('PAUSED'), call('PLAYING')]
    assert p.pcm_snapshot() == before


@pytest.mark.parametrize('inactive', ['stop', 'idle', 'teardown'])
def test_completion_cannot_restart_inactive_source(inactive):
    p = pipeline()
    native = p._pipeline
    p._buffering(10)
    if inactive == 'stop': p._stop.set()
    elif inactive == 'idle': p._media_idle = True
    else: p._pipeline = None
    p._buffering(100)
    native.set_state.assert_called_once_with('PAUSED')


@pytest.mark.parametrize('kind', ['LOCAL', 'HTTP'])
def test_other_sources_keep_status_only_buffering(kind):
    p = pipeline(kind)
    p._buffering(50)
    assert p.snapshot()['state'] == 'BUFFERING'
    p._buffering(100)
    assert p.snapshot()['state'] == 'CONNECTING'
    p._pipeline.set_state.assert_not_called()


def test_failed_state_change_is_explicit():
    p = pipeline()
    p._pipeline.set_state.return_value = 'FAILURE'
    with pytest.raises(DependencyUnavailable, match='buffering state'):
        p._buffering(10)
    assert not p._buffering_paused


def test_eos_resets_buffering_pause_for_next_item():
    p = pipeline()
    p._bus = Mock()
    p._local = Mock()
    p._local.resume.return_value = 'https://example.org/next'
    p._buffering(10)
    p._local_resume(failed=False)
    assert not p._buffering_paused
    p._buffering(10)
    assert p._pipeline.set_state.call_args_list == [
        call('PAUSED'), call('NULL'), call('PLAYING'), call('PAUSED')]
