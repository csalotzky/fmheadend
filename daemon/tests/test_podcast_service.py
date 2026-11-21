"""Feed/history boundary: real SQLite, deterministic refresh and playback races."""
from dataclasses import replace
from threading import Event, Thread, get_ident
from unittest.mock import Mock

import httpx
import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable, DomainError
from fmheadendd.models.configuration import PodcastConfig
from fmheadendd.sources.podcast import eligible_episodes, parse_feed
from fmheadendd.sources.podcast_fetch import FeedFetcher, FeedResponse
from fmheadendd.sources.podcast_runtime import PLAYED_BYTES
from fmheadendd.sources.podcast_service import PodcastFeedService
from fmheadendd.tests.test_podcast import item, rss
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
    assert not first.history
    with pytest.raises(TypeError):
        first.history[e.key] = None
    rt.p.pcm(PLAYED_BYTES)
    service._flush_marks(rt.history)
    rt.seconds[0] = 60
    service.io_step(rt.history)
    second = service.snapshot()
    assert second.revision == 2 and second.history[e.key].play_count == 1
    assert not first.history
    assert rt.db.connection.execute('PRAGMA user_version').fetchone()[0] == 1


def test_large_refresh_keeps_full_catalog_without_history_writes(rt):
    url = rt.p.config.feeds[0].url
    config = PodcastConfig(feeds=[dict(url=url, max_age_days=1, max_episodes=1)])
    body = rss(item('old', date='Thu, 01 Oct 2026 10:00:00 GMT'),
               *(item(str(i)) for i in range(2000)))
    service = PodcastFeedService(config, now=lambda: NOW, parser=parse_feed,
                                 fetch_factory=lambda: lambda url: FeedResponse(body, url))
    service.io_step(rt.history)
    view = service.snapshot()
    assert len(view.catalog.feeds[0].episodes) == view.catalog.results[0].discovered == 2001
    assert len(eligible_episodes(view.catalog, config, NOW)) == 1
    assert not view.history
    assert not rt.db.connection.execute('SELECT * FROM podcast_history').fetchall()


def test_failed_refresh_preserves_played_history_and_catalog(rt):
    begin(rt)
    service = rt.p.feed_service
    first = service.snapshot().catalog
    rt.p.pcm(PLAYED_BYTES)
    service._flush_marks(rt.history)
    before = rt.history.snapshot()
    assert len(before) == 1
    rt.seconds[0] = 60
    rt.response[0] = DomainError(503, 'PODCAST_FETCH_TIMEOUT', 'Timeout')
    service.io_step(rt.history)
    view = service.snapshot()
    assert view.catalog.results[0].error == 'PODCAST_FETCH_TIMEOUT'
    assert view.catalog.feeds == first.feeds
    assert view.history == rt.history.snapshot() == before


def test_long_background_refresh_attempts_every_feed_and_retains_failed_catalog():
    urls = [f'https://example.org/feed/{i}' for i in range(32)]
    config = PodcastConfig(feeds=[{'url': url} for url in urls])
    seconds = 0
    attempts = []
    fail = False

    def download(request):
        nonlocal seconds
        url = str(request.url)
        attempts.append(url)
        assert request.extensions['timeout']['read'] == 10.0
        seconds += 2
        if fail and url == urls[13]:
            raise httpx.ReadTimeout('temporary failure')
        episode_id = 'second' if fail else 'first'
        return httpx.Response(200, stream=httpx.ByteStream(rss(item(episode_id))))

    def parse(body, url, now, *, base_url):
        nonlocal seconds
        seconds += 3
        return parse_feed(body, url, now, base_url=base_url)

    service = PodcastFeedService(config, clock=lambda: seconds, now=lambda: NOW, parser=parse,
        fetch_factory=lambda: FeedFetcher(clock=lambda: seconds, transport=httpx.MockTransport(download)))
    history = Mock()
    history.snapshot.return_value = {}
    service.io_step(history)
    first = service.snapshot().catalog
    assert seconds == 160
    assert attempts == urls
    assert len(first.feeds) == len(first.results) == 32
    assert all(result.error is None for result in first.results)

    fail = True
    seconds += config.refresh_interval_minutes * 60
    attempts.clear()
    service.io_step(history)
    second = service.snapshot().catalog
    assert attempts == urls
    assert len(second.feeds) == len(second.results) == 32
    assert [r.error for r in second.results if r.error] == ['PODCAST_FETCH_TIMEOUT']
    assert second.results[13].error == 'PODCAST_FETCH_TIMEOUT'
    assert second.feeds[13] is first.feeds[13]
    assert second.feeds[-1].episodes[0].episode_id != first.feeds[-1].episodes[0].episode_id


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_late_snapshot_preserves_optimistic_threshold_and_schedule(tmp_path, mode):
    r = runtime(tmp_path, mode=mode)
    try:
        begin(r)
        service = r.p.feed_service
        before = service.snapshot()
        episode = r.p.rendering.context
        r.p.pcm(PLAYED_BYTES - 4)
        assert episode.key not in r.p.history_state
        r.p.pcm(4)
        optimistic = r.p.history_state[episode.key]
        assert optimistic.play_count == 1
        assert not r.history.snapshot()
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
    assert episode.key not in rt.p.history_state


def test_closed_runtime_rejects_snapshot_and_pcm(rt):
    begin(rt)
    snapshot = rt.p.feed_service.snapshot()
    rt.p.close()
    rt.p.apply_snapshot(replace(snapshot, revision=10))
    rt.p.pcm(PLAYED_BYTES)
    assert not rt.p.catalog_view.feeds and not rt.p.history_state
