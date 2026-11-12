"""Portable worker policy, transfer and parent persistence tests."""
from dataclasses import replace
from threading import Event
from unittest.mock import Mock
import json

import pytest

from fmheadendd.sources import podcast_worker as wire
from fmheadendd.sources.audio_ipc import ProtocolError, envelope, pack
from fmheadendd.sources.podcast_runtime import PLAYED_BYTES
from fmheadendd.sources.worker_recovery import WorkerRecovery
from fmheadendd.tests.test_podcast_runtime import runtime, NOW
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
    assert r.history.snapshot()[episode.key].play_count == 0
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


def test_refresh_does_not_undo_optimistic_history_or_prefetch(pair):
    r, parent, worker = pair
    p = worker.playback(r.p.config, Event())
    deliver(parent, worker)
    p.initial(); p.stream_start(); p.pcm(PLAYED_BYTES)
    episode = p.rendering.context
    old = p.identity_metadata()
    p.handoff(Mock())
    assert p.identity_metadata() == old
    # The event remains unpersisted while a newer feed snapshot arrives.
    r.seconds[0] = 60
    r.p.feed_service.io_step(r.history)
    deliver(parent, worker)
    assert p.history_state[episode.key].play_count == 1
    assert p.identity_metadata() == old


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


@pytest.mark.parametrize('mutation', ['url', 'count', 'date', 'feed'])
def test_snapshot_validation(pair, mutation):
    r, parent, worker = pair
    value = json.loads(wire.encode(r.p.feed_service.snapshot()))
    e = value['catalog']['feeds'][0]['episodes'][0]
    if mutation == 'url': e['media_url'] = 'file:///etc/passwd'
    if mutation == 'count': value['history'][0][2]['play_count'] = -1
    if mutation == 'date': e['discovered_at'] = '2026-01-01'
    if mutation == 'feed': e['feed_id'] = 'wrong'
    with pytest.raises(ProtocolError):
        wire.decode(json.dumps(value), 1, r.p.config)


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
    assert all(h.play_count == 0 for h in r.history.snapshot().values())


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
        history.snapshot.return_value = {}
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
    manager.worker_recovery.policy = RecoveryPolicy(backoff=0)
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
