from pathlib import Path
from threading import Event
from unittest.mock import Mock
import random

import pytest

from fmheadendd.errors import DomainError
from fmheadendd.sources.local_media import LocalMediaConfig, MediaItem
from fmheadendd.sources.local_playback import LocalPlayback, LocalPlaybackError


def items(*names):
    return [MediaItem('https://example.org/'+name) for name in names]


def make(tmp_path, candidates, **kwargs):
    resolver = Mock(return_value=candidates)
    playback = LocalPlayback(LocalMediaConfig('folder', tmp_path, order=kwargs.pop('order', 'sequential')),
                             Event(), resolver=resolver, **kwargs)
    return playback, resolver


def test_initial_uri_conversion_and_worker_resolution(tmp_path):
    path = tmp_path / 'space # percent % unicode \u00e9.wav'
    p, resolver = make(tmp_path, [MediaItem(path), *items('remote?token=secret')])
    assert p.initial() == path.as_uri()
    resolver.assert_called_once()
    player = Mock()
    p.handoff(player)
    player.set_property.assert_called_once_with('uri', 'https://example.org/remote?token=secret')
    resolver.assert_called_once()  # No scan on streaming thread.


def test_cycle_rescan_and_real_pcm_success(tmp_path):
    p, resolve = make(tmp_path, items('a', 'b'))
    assert p.initial().endswith('/a')
    p.stream_start()
    p.handoff(Mock())
    p.service(Mock())
    assert resolve.call_count == 1  # URI assignment/STREAM_START are not PCM.
    p.pcm()
    resolve.return_value = items('new')
    p.service(Mock())
    assert resolve.call_count == 2
    assert p.prepared.uri.endswith('/new')


def test_eos_fallback_keeps_unrendered_handoff_and_then_advances(tmp_path):
    p, _ = make(tmp_path, items('a', 'b', 'c'))
    p.initial(); p.stream_start(); p.pcm()
    p.handoff(Mock())
    assert p.resume(failed=False).endswith('/b')
    p.stream_start(); p.pcm()
    assert p.resume(failed=False).endswith('/c')


def test_failure_skips_each_item_once_then_stops(tmp_path):
    p, resolve = make(tmp_path, items('a', 'b', 'c'))
    assert p.initial().endswith('/a')
    assert p.resume(failed=True).endswith('/b')
    assert p.resume(failed=True).endswith('/c')
    with pytest.raises(LocalPlaybackError, match='PLAYBACK_EXHAUSTED'):
        p.resume(failed=True)
    assert resolve.call_count == 1


def test_previous_stream_pcm_cannot_mark_prefetched_cycle_success(tmp_path):
    p, _ = make(tmp_path, items('a'))
    p.initial(); p.stream_start(); p.pcm(); p.prepare()
    old = p.rendering.cycle
    p.handoff(Mock()); p.pcm()
    assert old.played and not p.cycle.played
    p.stream_start(); p.pcm()
    assert p.cycle.played


def test_success_prevents_false_exhaustion_and_reset_is_fresh(tmp_path):
    p, resolve = make(tmp_path, items('a', 'b'))
    p.initial(); p.stream_start(); p.pcm()
    assert p.resume(failed=True).endswith('/b')
    assert resolve.call_count == 2
    p.close()
    assert p.queue is None and not p.submitted and p.prepared is None
    player = Mock(); p.handoff(player); player.set_property.assert_not_called()
    fresh, _ = make(tmp_path, items('new'))
    assert fresh.initial().endswith('/new')


def test_empty_resolution_reports_safe_code_not_sensitive_path(tmp_path):
    p, resolve = make(tmp_path, [])
    resolve.side_effect = DomainError(409, 'NO_PLAYABLE_TRACKS', 'secret path')
    with pytest.raises(LocalPlaybackError, match='^NO_PLAYABLE_TRACKS$'):
        p.initial()


def test_random_keeps_core_shuffle_and_duplicates(tmp_path):
    candidates = items('a', 'b', 'a', 'c')
    p, resolve = make(tmp_path, candidates, order='random', rng=random.Random(42))
    uris = [p.initial()]
    for _ in range(3):
        uris.append(p.resume(failed=True))
    assert sorted(uris) == sorted(i.locator for i in candidates)
    with pytest.raises(LocalPlaybackError): p.resume(failed=True)
    assert resolve.call_count == 1


def test_handoff_never_waits_for_teardown_lock(tmp_path):
    from fmheadendd.models.configuration import InputSourceConfig
    from fmheadendd.sources.gst_pipeline import GstPipeline
    p = GstPipeline(InputSourceConfig(name='test', type='LOCAL', config=dict(mode='folder',path=str(tmp_path))))
    p._local = Mock()
    with p._uri_change_lock:
        p._local_handoff(Mock())
    p._local.handoff.assert_not_called()
    p._local_handoff(Mock())
    p._local.handoff.assert_called_once()
    p._stop.set(); p._local_handoff(Mock())
    p._local.handoff.assert_called_once()


def test_worker_never_assigns_uri_during_native_handoff(tmp_path):
    p, _ = make(tmp_path, items('one'))
    p.initial(); player=Mock()
    p.handoff(player)  # No preloaded next-cycle item before actual PCM.
    p.stream_start(); p.pcm(); p.service(player)
    player.set_property.assert_not_called()
    assert p.prepared is not None  # EOS fallback owns a safe later state change.

def test_eos_recovery_preserves_decoded_tail(tmp_path):
    from fmheadendd.models.configuration import InputSourceConfig
    from fmheadendd.sources.gst_pipeline import GstPipeline
    p = GstPipeline(InputSourceConfig(name='test', type='LOCAL', config=dict(mode='folder',path=str(tmp_path))))
    p.Gst = Mock()
    p._pipeline = Mock()
    p._bus = Mock()
    p._local = Mock()
    p._local.resume.return_value = 'https://example.org/next'
    p.staging.put(b'\x01\x00' * 8)
    p._local_resume(failed=False)
    assert p.staging.pop() == b'\x01\x00' * 8
    p._local.resume.assert_called_once_with(failed=False)
