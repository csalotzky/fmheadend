"""Feed/history boundary: real SQLite, deterministic refresh and playback races."""
from dataclasses import replace
from threading import Event, Thread, get_ident
from unittest.mock import Mock

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.sources.podcast import parse_feed
from fmheadendd.sources.podcast_history import PodcastHistory
from fmheadendd.sources.podcast_runtime import PLAYED_BYTES
from fmheadendd.sources.podcast_service import PodcastFeedService
from fmheadendd.tests.test_podcast_runtime import runtime, begin, NOW


@pytest.fixture
def rt(tmp_path):
    r = runtime(tmp_path)
    yield r
    r.p.close()
    r.db.close()


def test_complete_detached_snapshot_and_v1_history(rt):
    begin(rt)
    service = rt.p.feed_service
    first = service.snapshot()
    e = rt.p.rendering.context
    assert first.revision == 1
    assert set(first.history) == {e.key for f in first.catalog.feeds for e in f.episodes}
    assert first.history[e.key].play_count == 0
    with pytest.raises(TypeError):
        first.history[e.key] = None
    rt.p.pcm(PLAYED_BYTES)
    service._flush_marks(rt.history)
    rt.seconds[0] = 60
    service.io_step(rt.history)
    second = service.snapshot()
    assert second.revision == 2 and second.history[e.key].play_count == 1
    assert first.history[e.key].play_count == 0
    assert rt.db.connection.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_late_snapshot_preserves_optimistic_threshold_and_schedule(tmp_path, mode):
    r = runtime(tmp_path, mode=mode)
    try:
        begin(r)
        service = r.p.feed_service
        before = service.snapshot()
        episode = r.p.rendering.context
        r.p.pcm(PLAYED_BYTES - 4)
        assert r.p.history_state[episode.key].play_count == 0
        r.p.pcm(4)
        optimistic = r.p.history_state[episode.key]
        assert optimistic.play_count == 1
        assert r.history.snapshot()[episode.key].play_count == 0
        r.p.apply_snapshot(replace(before, revision=before.revision + 1))
        assert r.p.history_state[episode.key] == optimistic
        r.p.service(Mock())
        assert r.p.prepared.context.key != episode.key
        # A duplicate or reordered snapshot cannot change the playback revision.
        revision = r.p.revision
        r.p.apply_snapshot(before)
        assert r.p.revision == revision
        service._flush_marks(r.history)
        r.seconds[0] = 60
        service.io_step(r.history)
        assert r.p.history_state[episode.key].play_count == 1
    finally:
        r.p.close()
        r.db.close()


def test_refresh_snapshot_racing_pcm_has_no_lock_inversion(rt):
    begin(rt)
    service = rt.p.feed_service
    ready, release = Event(), Event()
    original = service.on_snapshot

    def delayed(snapshot):
        ready.set()
        assert release.wait(5)
        original(snapshot)

    service.on_snapshot = delayed
    # The read is detached before the PCM callback updates optimistic history.
    history = Mock()
    history.snapshot.return_value = rt.history.snapshot()
    rt.seconds[0] = 60
    thread = Thread(target=lambda: service.io_step(history))
    thread.start()
    try:
        assert ready.wait(5)
        rt.p.pcm(PLAYED_BYTES)
        episode = rt.p.rendering.context
        assert rt.p.history_state[episode.key].play_count == 1
        # Snapshot polling also works while the publication callback is blocked.
        assert service.snapshot().revision == 2
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert rt.p.history_state[episode.key].play_count == 1


def test_owned_database_thread_stop_drains_and_closes(rt, monkeypatch):
    from fmheadendd.sources import podcast_service as module
    path = rt.db.connection.execute('PRAGMA database_list').fetchone()[2]
    calls = []
    ready = Event()

    class OwnedDatabase(Database):
        def __init__(self, path):
            calls.append(('open', get_ident()))
            super().__init__(path)

        def close(self):
            calls.append(('close', get_ident()))
            super().close()

    monkeypatch.setattr(module, 'Database', OwnedDatabase)
    service = PodcastFeedService(rt.p.config, database_path=path, source_id=rt.source.id,
        parser=parse_feed, fetch_factory=rt.p.feed_service.fetch_factory,
        now=lambda: NOW, on_snapshot=lambda s: ready.set())
    try:
        service.start()
        assert ready.wait(5)
        episode = service.snapshot().catalog.feeds[0].episodes[0]
        assert service.played(episode, NOW)
    finally:
        service.close()
    assert not service._io_thread.is_alive()
    assert calls[0][0] == 'open' and calls[1] == ('close', calls[0][1])
    assert calls[0][1] != get_ident()
    assert rt.history.snapshot()[episode.key].play_count == 1
    assert not service.played(episode, NOW)
    with pytest.raises(DependencyUnavailable):
        service.start()


def test_failed_write_is_reported_without_retry(rt):
    begin(rt)
    episode = rt.p.rendering.context
    history = Mock()
    history.mark_played.side_effect = RuntimeError('private URL')
    failed = Event()
    errors = []
    service = PodcastFeedService(rt.p.config, history_factory=lambda: history,
        on_error=lambda error: (errors.append(error), failed.set()))
    assert service.played(episode, NOW)
    try:
        service.start()
        assert failed.wait(5)
    finally:
        service.close()
    assert errors == ['PODCAST_HISTORY_IO_ERROR']
    history.mark_played.assert_called_once_with(episode, NOW)


def test_notification_backlog_is_bounded(rt):
    begin(rt)
    episode = rt.p.rendering.context
    service = rt.p.feed_service
    for _ in range(64):
        assert service.played(episode, NOW)
    assert not service.played(episode, NOW)
    rt.p.pcm(PLAYED_BYTES)
    assert rt.p.history_error == 'PODCAST_HISTORY_BACKLOG'
    assert not rt.p.rendering.marked
    assert rt.p.history_state[episode.key].play_count == 0


def test_closed_runtime_rejects_snapshot_and_pcm(rt):
    begin(rt)
    snapshot = rt.p.feed_service.snapshot()
    rt.p.close()
    rt.p.apply_snapshot(replace(snapshot, revision=10))
    rt.p.pcm(PLAYED_BYTES)
    assert not rt.p.catalog_view.feeds and not rt.p.history_state
