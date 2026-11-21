"""Portable worker policy, transfer and parent persistence tests."""
from dataclasses import replace
from datetime import timedelta
from threading import Event
from unittest.mock import Mock
import json

import pytest

from fmheadendd.sources import podcast_worker as wire
from fmheadendd.sources.audio_ipc import ProtocolError, envelope, pack
from fmheadendd.models.configuration import PodcastConfig
from fmheadendd.sources.podcast import CatalogSnapshot, FeedRefreshResult, eligible_episodes, feed_identity
from fmheadendd.sources.podcast_fetch import FeedResponse
from fmheadendd.sources.podcast_runtime import PLAYED_BYTES
from fmheadendd.sources.podcast_schedule import PodcastScheduler
from fmheadendd.sources.podcast_service import PodcastFeedSnapshot
from fmheadendd.sources.worker_recovery import WorkerRecovery
from fmheadendd.tests.test_podcast_runtime import runtime, rss, NOW
from fmheadendd.tests.test_audio_worker import simulated_process, until, finish


@pytest.fixture
def pair(tmp_path):
    r = runtime(tmp_path)
    parent = wire.ParentPodcast(r.p.feed_service)
    r.p.feed_service.io_step(r.history)
    worker = wire.WorkerPodcast(r.p.config)
    yield r, parent, worker
    r.p.close()
    r.db.close()


def deliver(parent, worker):
    messages = []
    while (data := parent.poll()) is not None:
        assert len(pack(envelope(1, 'a' * 32, len(messages)+1, 'podcast_snapshot', data))) < 65536
        worker.accept(data)
        messages.append(data)
    return messages


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_worker_scheduler_threshold_parent_persistence(pair, monkeypatch, mode):
    r, parent, worker = pair
    worker.config = r.p.config.model_copy(update={'schedule_mode': mode})
    def forbidden(*args, **kwargs):
        raise AssertionError('Child attempted feed/database I/O')
    monkeypatch.setattr('fmheadendd.sources.podcast_runtime.PodcastFeedService', forbidden)
    p = worker.playback(worker.config, Event())
    p.start()
    assert p.initial() is None
    deliver(parent, worker)
    assert p.initial()
    p.stream_start()
    episode = p.rendering.context
    p.pcm(PLAYED_BYTES-4)
    assert worker.poll() is None
    p.pcm(4)
    p.pcm(PLAYED_BYTES)
    notification = worker.poll()
    assert notification and worker.poll() is None
    assert p.history_state[episode.key].play_count == 1
    assert not r.db.connection.execute('SELECT * FROM podcast_history').fetchall()
    parent.accept(notification)
    r.p.feed_service._flush_marks(r.history)
    assert r.history.snapshot()[episode.key].play_count == 1
    r.seconds[0] = 60
    r.p.feed_service.io_step(r.history)
    deliver(parent, worker)
    assert p.history_state[episode.key].play_count == 1
    p.service(Mock())
    assert p.prepared.context.key != episode.key
    p.close()


def test_chunks_atomic_stale_and_bad_offsets(pair, monkeypatch):
    r, parent, worker = pair
    monkeypatch.setattr(wire, 'CHUNK', 100)
    first = parent.poll()
    worker.accept(first)
    assert worker.snapshot is None
    with pytest.raises(ProtocolError):
        worker.accept(first)
    messages = deliver(parent, worker)
    assert len(messages) > 1 and worker.snapshot.revision == 1
    old = worker.snapshot
    worker.accept(first)
    assert worker.snapshot is old


@pytest.mark.parametrize('departed', [False, True])
def test_play_during_refresh_preserves_optimistic_and_durable_history(pair, departed):
    r, parent, worker = pair
    service = r.p.feed_service
    p = worker.playback(r.p.config, Event())
    deliver(parent, worker)
    p.initial(); p.stream_start()
    episode = p.rendering.context
    old = p.identity_metadata()
    p.handoff(Mock())
    assert p.identity_metadata() == old

    def fetch_with_play_event(url):
        p.pcm(PLAYED_BYTES)
        parent.accept(worker.poll())
        return FeedResponse(rss(('C',)) if departed else r.response[0], url)

    # A mark queued after the I/O turn has drained the queue is persisted next turn.
    fetch_factory = service.fetch_factory
    service.fetch_factory = lambda: fetch_with_play_event
    r.seconds[0] = 60
    service.io_step(r.history)
    deliver(parent, worker)
    assert not r.history.snapshot() and not worker.snapshot.history
    assert p.history_state[episode.key].play_count == 1
    assert p.identity_metadata() == old
    assert (episode.key in p.eligible_keys) is not departed

    service.fetch_factory = fetch_factory
    replacement = wire.ParentPodcast(service)
    service.io_step(r.history)
    replacement_worker = wire.WorkerPodcast(r.p.config)
    deliver(replacement, replacement_worker)
    assert len(r.calls) == 1
    assert r.history.snapshot()[episode.key].play_count == 1
    if departed:
        assert not replacement_worker.snapshot.history
        r.seconds[0] = 120
        service.io_step(r.history)
        deliver(replacement, replacement_worker)
    assert replacement_worker.snapshot.history[episode.key].play_count == 1


def test_restart_snapshot_reads_latest_history_without_rss_fetch(pair):
    r, parent, worker = pair
    deliver(parent, worker)
    episode = worker.snapshot.catalog.feeds[0].episodes[0]
    r.history.mark_played(episode, NOW)
    next_parent = wire.ParentPodcast(r.p.feed_service)
    assert next_parent.poll() is None
    r.p.feed_service.io_step(r.history)
    next_worker = wire.WorkerPodcast(r.p.config)
    deliver(next_parent, next_worker)
    assert len(r.calls) == 1
    assert next_worker.snapshot.history[episode.key].play_count == 1


def test_large_catalog_projects_age_and_count_limits_with_original_order(pair):
    r, _, _ = pair
    original = r.p.feed_service.snapshot().catalog.feeds[0]
    template = original.episodes[0]
    feeds = []
    for i in range(32):
        url = f'https://example.org/feed/{i}'
        fid = feed_identity(url)
        episodes = tuple(replace(template, feed_id=fid, feed_url=url, episode_id=str(j),
                                 published_at=NOW - timedelta(days=j))
                         for j in reversed(range(313)))
        feeds.append(replace(original, feed_id=fid, url=url, episodes=episodes))
    # Even feeds are limited by age (2), odd feeds by count (1).
    config = PodcastConfig(feeds=[dict(url=f.url, max_age_days=1, max_episodes=3 if i % 2 == 0 else 1)
                                  for i, f in enumerate(feeds)])
    results = tuple(FeedRefreshResult(f.feed_id, None, len(f.episodes), f.title) for f in feeds)
    catalog = CatalogSnapshot(tuple(feeds), results, NOW)
    service = Mock(config=config, now=lambda: NOW, error=None)
    service.snapshot.side_effect = [PodcastFeedSnapshot(), PodcastFeedSnapshot(1, catalog)]
    parent, worker = wire.ParentPodcast(service), wire.WorkerPodcast(config)
    deliver(parent, worker)
    projected = worker.snapshot.catalog
    assert [f.feed_id for f in projected.feeds] == [f.feed_id for f in feeds]
    assert [len(f.episodes) for f in projected.feeds] == [2, 1] * 16
    assert eligible_episodes(projected, config, NOW) == eligible_episodes(catalog, config, NOW)
    assert projected.results == results
    assert sum(len(f.episodes) for f in catalog.feeds) == 10016


def test_sqlite_history_is_projected_and_returns_when_episode_is_eligible(pair):
    r, _, _ = pair
    service = r.p.feed_service
    catalog = service.snapshot().catalog
    feed = catalog.feeds[0]
    a, b = feed.episodes
    when = NOW - timedelta(days=2)
    for episode, count in ((a, 2), (b, 4)):
        for _ in range(count):
            r.history.mark_played(episode, when)
    with r.db.connection:
        r.db.connection.executemany(
            'INSERT INTO podcast_history (source_id, feed_id, episode_id, play_count, last_played_at, last_seen_at) '
            'VALUES (?, ?, ?, 1, ?, ?)',
            ((r.source.id, a.feed_id, f'old-{i}', when.isoformat(), when.isoformat()) for i in range(10000)))

    config = r.p.config.model_copy(update={'schedule_mode': 'SMART'})
    service.config = config
    parent, worker = wire.ParentPodcast(service), wire.WorkerPodcast(config)
    p = worker.playback(config, Event())
    service._publish(replace(catalog, feeds=(replace(feed, episodes=(a,)),)), r.history)
    deliver(parent, worker)
    assert set(service.snapshot().history) == {a.key}
    assert set(worker.snapshot.history) == {a.key}
    assert p.initial() == a.media_url
    p.stream_start()

    service._publish(catalog, r.history)
    deliver(parent, worker)
    assert set(service.snapshot().history) == {a.key, b.key}
    assert set(worker.snapshot.history) == {a.key, b.key}
    assert worker.snapshot.history[b.key].last_played_at == when
    assert {key: h.play_count for key, h in p.history_state.items()} == {a.key: 2, b.key: 4}
    p.service(Mock())
    assert p.prepared.context.key == a.key
    full_history = r.history.snapshot()
    assert len(full_history) == 10002
    targeted, baseline = PodcastScheduler(), PodcastScheduler()
    for _ in range(3):
        assert targeted.choose_next((a, b), service.snapshot().history, NOW) == (
            baseline.choose_next((a, b), full_history, NOW))


@pytest.mark.parametrize(('count', 'url_suffix', 'reason'), [
    (10001, '', 'exceeds 10,000 episodes'),
    (1100, 'x' * 8000, 'exceeds 8 MiB'),
])
def test_oversized_eligible_update_leaves_playback_running(pair, caplog, count, url_suffix, reason):
    r, parent, worker = pair
    service = r.p.feed_service
    p = worker.playback(worker.config, Event())
    deliver(parent, worker)
    accepted = worker.snapshot
    assert p.initial()
    p.stream_start()
    playing = p.rendering
    catalog = service.snapshot().catalog
    feed = catalog.feeds[0]
    episodes = tuple(replace(playing.context, episode_id=str(i),
                             media_url='https://example.org/' + url_suffix) for i in range(count))
    service._publish(replace(catalog, feeds=(replace(feed, episodes=episodes),)), r.history)

    assert parent.poll() is None
    assert parent.poll() is None
    assert caplog.text.count(reason) == 1
    assert worker.snapshot is accepted
    p.pcm(PLAYED_BYTES)
    assert p.rendering is playing and p.active is playing
    parent.accept(worker.poll())
    service._flush_marks(r.history)
    assert r.history.snapshot()[playing.context.key].play_count == 1
    p.service(Mock())
    player = Mock()
    p.handoff(player)
    player.set_property.assert_called_once()

    service._publish(catalog, r.history)
    deliver(parent, worker)
    assert worker.snapshot.revision == accepted.revision + 2
    assert worker.snapshot.history[playing.context.key].play_count == 1


@pytest.mark.parametrize('mutation', ['url', 'count', 'date', 'feed'])
def test_snapshot_validation(pair, mutation):
    r, _, _ = pair
    service = r.p.feed_service
    episode = service.snapshot().catalog.feeds[0].episodes[0]
    r.history.mark_played(episode, NOW)
    service.request_snapshot()
    service.io_step(r.history)
    snapshot = service.snapshot()
    value = json.loads(wire.encode(snapshot))
    e = value['catalog']['feeds'][0]['episodes'][0]
    if mutation == 'url': e['media_url'] = 'file:///etc/passwd'
    if mutation == 'count': value['history'][0][2]['play_count'] = -1
    if mutation == 'date': e['discovered_at'] = '2026-01-01'
    if mutation == 'feed': e['feed_id'] = 'wrong'
    with pytest.raises(ProtocolError):
        wire.decode(json.dumps(value), snapshot.revision, r.p.config)


def test_profile_reuses_service_but_source_change_does_not(pair):
    r, _, _ = pair
    old = Mock(source=r.source, _podcast_service=r.p.feed_service, _service_cleanup=None)
    assert WorkerRecovery._retain(old, None, True)
    assert WorkerRecovery._retain(old, r.source.model_copy(update={'input_gain_db': -6}), False)
    assert not WorkerRecovery._retain(old, None, False)
    changed = r.source.model_copy(update={'config': dict(r.source.config, schedule_mode='SMART')})
    assert not WorkerRecovery._retain(old, changed, False)


def test_unknown_feed_with_null_url_cannot_persist_played_event(pair):
    r, parent, _ = pair
    data = json.loads(wire.encode(r.p.feed_service.snapshot()))
    episode = data['catalog']['feeds'][0]['episodes'][0]
    episode.update(feed_id='unconfigured', feed_url=None)
    with pytest.raises(ProtocolError):
        parent.accept(dict(episode=episode, when=NOW.isoformat()))
    assert not r.p.feed_service._marks
    assert not r.db.connection.execute('SELECT * FROM podcast_history').fetchall()


def test_generation_fences_played_and_factory_selection(pair, monkeypatch):
    from fmheadendd.sources import process_pipeline as module
    r, _, _ = pair
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    p = module.ExperimentalAudioFactory()(r.source, podcast_service=r.p.feed_service)
    assert isinstance(p, module.ProcessPipeline)
    assert not isinstance(module.ExperimentalHttpFactory()(r.source), module.ProcessPipeline)
    p._accept('control', envelope(r.source.id, 'stale', 1, 'podcast_played', {'invalid': True}))
    assert p.stale_messages == 1 and not r.p.feed_service._marks
    sibling = p.fresh()
    assert sibling.output is p.output and sibling._podcast_service is p._podcast_service
    assert sibling.generation != p.generation


def test_service_start_and_close_are_serialized(pair):
    from threading import Barrier, Thread
    from fmheadendd.errors import DependencyUnavailable
    from fmheadendd.sources.podcast_service import PodcastFeedService
    r, _, _ = pair
    for _ in range(10):
        history = Mock()
        history.snapshot_for_episodes.return_value = {}
        service = PodcastFeedService(r.p.config, history_factory=lambda: history)
        # No RSS fetch is needed to exercise ownership publication.
        service._next_refresh = service.clock() + 3600
        gate = Barrier(3)
        def start():
            gate.wait()
            try: service.start()
            except DependencyUnavailable: pass  # STOP won the race.
        def close():
            gate.wait()
            service.close()
        a, b = Thread(target=start), Thread(target=close)
        a.start(); b.start(); gate.wait()
        a.join(5); b.join(5)
        assert not a.is_alive() and not b.is_alive()
        assert service.closed
        assert service.error is None
        assert service._io_thread is None or not service._io_thread.is_alive()


def test_shared_recovery_profile_apply_stop_and_sibling(simulated_process, tmp_path, monkeypatch):
    from fmheadendd.database import Database
    from fmheadendd.errors import SourcePending
    from fmheadendd.models.configuration import InputSourceConfig
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.sources.process_pipeline import ProcessPipeline
    from fmheadendd.sources.podcast import parse_feed
    from fmheadendd.sources.podcast_fetch import FeedResponse, FeedFetcher
    from fmheadendd.sources.worker_recovery import RecoveryPolicy
    from fmheadendd.tests.audiod_fixture import FakeDecoder
    from fmheadendd.tests.profile_support import profile_store
    from fmheadendd.tests.test_podcast_runtime import rss, URL
    from fmheadendd.tests.test_worker_recovery import service_until
    monkeypatch.setattr(FakeDecoder, 'local_wait_until', 0, raising=False)
    monkeypatch.setattr(FeedFetcher, '__call__', lambda self, url: FeedResponse(rss(), url))
    monkeypatch.setattr('fmheadendd.sources.podcast_service.shared_parser', lambda: parse_feed)
    db = Database(tmp_path/'managed.db')
    manager = SourceManager(db, pipeline_factory=ProcessPipeline, profile_files=profile_store(tmp_path))
    manager.worker_recovery.policy = RecoveryPolicy()
    podcast = db.save('sources', InputSourceConfig(name='pod', type='PODCAST',
        config={'feeds': [{'url': URL}]}))
    http = db.save('sources', InputSourceConfig(name='http', type='HTTP', config={'uri': 'http://localhost/a'}))
    owners = []
    try:
        for sid, tx in ((podcast.id, 0), (http.id, 2)):
            try: manager.acquire(sid, tx)
            except SourcePending: pass
        old, sibling = manager.pipelines[podcast.id], manager.pipelines[http.id]
        owners.extend((old, sibling))
        until(lambda: old.output.read(0))
        until(lambda: not old.startup_pending)
        manager.acquire(podcast.id, 1)
        service = old._podcast_service
        until(lambda: service.snapshot().revision > 0)
        thread = service._io_thread
        old.process.terminate()
        service_until(manager, lambda: manager.pipelines[podcast.id] is not old)
        new = manager.pipelines[podcast.id]; owners.append(new)
        until(lambda: new.output.read(0))
        assert old.process.poll() is not None
        assert new.output is old.output and new._podcast_service is service
        assert service._io_thread is thread and manager.consumers[podcast.id] == {0, 1}
        assert until(lambda: sibling.output.read(2))
        changed = podcast.model_copy(update={'input_gain_db': -6})
        manager._restart_audio(changed)
        service_until(manager, lambda: manager.pipelines[podcast.id] is not new)
        latest = manager.pipelines[podcast.id]; owners.append(latest)
        until(lambda: latest.output.read(0))
        assert latest._podcast_service is service and service._io_thread is thread
        manager.release(podcast.id, 0)
        manager.release(podcast.id, 1)
        service_until(manager, lambda: podcast.id not in manager.pipelines)
        assert not thread.is_alive() and service.closed
        assert manager.pipelines[http.id] is sibling
        assert until(lambda: sibling.output.read(2))
    finally:
        manager.close()
        for p in owners: finish(p)
        db.close()
