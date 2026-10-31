"""Real spawn-process coverage; no hardware, network or GStreamer required."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event, Thread
import time

import pytest

from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import PodcastConfig
from fmheadendd.sources.podcast import EpisodeCatalog, parse_feed
from fmheadendd.sources.podcast_fetch import FeedResponse
from fmheadendd.sources import podcast_parser as module
from fmheadendd.sources.podcast_runtime import PodcastRuntime

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
URL = "https://example.org/feed"
RSS = b'<rss version="2.0"><channel><title>Show</title><item><guid isPermaLink="false">one</guid><title>&lt;b&gt;Episode&lt;/b&gt;</title><enclosure url="audio.mp3" type="audio/mpeg"/></item></channel></rss>'
ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Show</title><entry><id>urn:one</id><title>Episode</title><published>2026-10-04T00:00:00Z</published><link rel="enclosure" type="audio/ogg" href="audio.ogg"/></entry></feed>'


def pid_parse(data, url, now, base_url):
    return (os.getpid(), parse_feed(data, url, now, base_url=base_url)), None


def crash_parse(*args):
    os._exit(7)


def slow_parse(*args):
    time.sleep(60)


def delayed_parse(*args):
    time.sleep(0.6)
    return pid_parse(*args)


@pytest.fixture
def parser():
    value = module.PodcastParser(timeout=10)
    yield value
    value.close()


def test_parse_runs_in_shared_spawn_pid_and_matches_sync(parser, monkeypatch):
    monkeypatch.setattr(module, '_parse', pid_parse)
    pids = []
    for data in (RSS, ATOM):
        pid, feed = parser(data, URL, NOW, base_url='https://cdn.example.org/show/feed')
        pids.append(pid)
        assert feed == parse_feed(data, URL, NOW, base_url='https://cdn.example.org/show/feed')
    assert pids[0] == pids[1] != os.getpid()
    assert parser._executor._mp_context.get_start_method() == 'spawn'


def test_sources_share_parser_and_source_close_does_not_close_pool():
    module.close_shared_parser()
    config = PodcastConfig(feeds=[dict(url=URL)])
    first = PodcastRuntime(config, Event())
    second = PodcastRuntime(config, Event())
    try:
        assert first._parser is second._parser is module.shared_parser()
        first.catalog.refresh(config, lambda url: FeedResponse(RSS, url), NOW)
        executor = first._parser._executor
        first.close()
        second.catalog.refresh(config, lambda url: FeedResponse(ATOM, url), NOW)
        assert second._parser._executor is executor
        children = list(executor._processes.values())
    finally:
        second.close()
        module.close_shared_parser()
    assert all(not child.is_alive() for child in children)


def test_invalid_feed_preserves_catalog_and_does_not_break_worker(parser):
    config = PodcastConfig(feeds=[dict(url=URL)])
    catalog = EpisodeCatalog(parser)
    good = catalog.refresh(config, lambda url: FeedResponse(RSS, url), NOW)
    executor = parser._executor
    failed = catalog.refresh(config, lambda url: FeedResponse(b'not a feed', url), NOW)
    assert failed.feeds == good.feeds
    assert failed.results[0].error == 'PODCAST_FEED_INVALID'
    assert parser._executor is executor
    assert parser(RSS, URL, NOW) == good.feeds[0]


@pytest.mark.parametrize('worker,code', [(crash_parse, 'PODCAST_PARSE_FAILED'),
                                        (slow_parse, 'PODCAST_PARSE_TIMEOUT')])
def test_crash_timeout_and_recovery(parser, monkeypatch, worker, code):
    original = module._parse
    config = PodcastConfig(feeds=[dict(url=URL)])
    catalog = EpisodeCatalog(parser)
    good = catalog.refresh(config, lambda url: FeedResponse(RSS, url), NOW)
    monkeypatch.setattr(module, '_parse', worker)
    children = list(parser._executor._processes.values())
    parser.timeout = 1 if worker is slow_parse else 10
    failed = catalog.refresh(config, lambda url: FeedResponse(RSS, url), NOW)
    assert failed.results[0].error == code
    assert failed.feeds == good.feeds
    assert parser._executor is None
    assert all(not child.is_alive() for child in children)
    monkeypatch.setattr(module, '_parse', original)
    parser.timeout = 10
    assert parser(RSS, URL, NOW) == parse_feed(RSS, URL, NOW)


def test_close_interrupts_pending_parse_and_rejects_new_work(parser, monkeypatch):
    # Warm the pool so a slow Windows spawn is not mistaken for a blocked parse.
    parser(RSS, URL, NOW)
    children = list(parser._executor._processes.values())
    monkeypatch.setattr(module, '_parse', slow_parse)
    submitted = Event()
    original_submit = parser._executor.submit
    def submit(*args):
        future = original_submit(*args)
        submitted.set()
        return future
    monkeypatch.setattr(parser._executor, 'submit', submit)
    errors = []
    def call():
        try:
            parser(RSS, URL, NOW)
        except DomainError as error:
            errors.append(error.error)
    thread = Thread(target=call)
    thread.start()
    assert submitted.wait(timeout=5)
    parser.close()
    thread.join(timeout=5)
    assert not thread.is_alive() and errors
    assert all(not child.is_alive() for child in children)
    with pytest.raises(DomainError, match='closed'):
        parser(RSS, URL, NOW)
    parser.close()


def test_worker_sanitizes_unexpected_parser_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError('https://user:secret@example.org/feed')
    monkeypatch.setattr(module, 'parse_feed', fail)
    assert module._parse(RSS, URL, NOW, None) == (None, 'PODCAST_PARSE_FAILED')


def test_concurrent_feeds_get_separate_execution_budgets(parser, monkeypatch):
    parser(RSS, URL, NOW)  # Exclude startup from this timing regression.
    monkeypatch.setattr(module, '_parse', delayed_parse)
    parser.timeout = 1.5
    with ThreadPoolExecutor(max_workers=3) as callers:
        futures = [callers.submit(parser, RSS, URL, NOW) for _ in range(3)]
        results = [future.result(timeout=10) for future in futures]
    assert len({pid for pid, feed in results}) == 1
    assert all(feed == parse_feed(RSS, URL, NOW) for pid, feed in results)


def test_queue_expiry_does_not_discard_active_worker_and_close_wakes_waiters(parser, monkeypatch):
    parser(RSS, URL, NOW)
    executor = parser._executor
    submitted = Event()
    original_submit = executor.submit
    def submit(*args):
        future = original_submit(*args)
        submitted.set()
        return future
    monkeypatch.setattr(executor, 'submit', submit)
    monkeypatch.setattr(module, '_parse', slow_parse)
    with ThreadPoolExecutor(max_workers=2) as callers:
        running = callers.submit(parser, RSS, URL, NOW)
        assert submitted.wait(timeout=5)
        parser.queue_timeout = 0.05
        with pytest.raises(DomainError) as error:
            parser(RSS, URL, NOW)
        assert error.value.error == 'PODCAST_PARSE_BUSY'
        assert parser._executor is executor
        parser.queue_timeout = 120
        queued = Event()
        original_wait = parser._lock.wait
        def wait(*args, **kwargs):
            queued.set()
            return original_wait(*args, **kwargs)
        monkeypatch.setattr(parser._lock, 'wait', wait)
        waiting = callers.submit(parser, RSS, URL, NOW)
        assert queued.wait(timeout=5)
        parser.close()
        for future in (running, waiting):
            with pytest.raises(DomainError):
                future.result(timeout=5)


def test_completed_result_is_rejected_if_close_wins_race(parser, monkeypatch):
    parser(RSS, URL, NOW)
    original_submit = parser._executor.submit
    def submit(*args):
        future = original_submit(*args)
        original_result = future.result
        def result(*args, **kwargs):
            value = original_result(*args, **kwargs)
            parser.close()
            return value
        monkeypatch.setattr(future, 'result', result)
        return future
    monkeypatch.setattr(parser._executor, 'submit', submit)
    with pytest.raises(DomainError) as error:
        parser(RSS, URL, NOW)
    assert error.value.error == 'PODCAST_PARSE_CLOSED'


def test_daemon_lifespan_closes_shared_worker(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from fmheadendd.app import create_app
    from fmheadendd.sources.pipeline import MockPipeline
    from fmheadendd.tests.profile_support import profile_settings

    app = create_app(profile_settings(tmp_path / 'parser.db'), pipeline_factory=MockPipeline)
    with TestClient(app):
        parser = module.shared_parser()
        assert parser(RSS, URL, NOW) == parse_feed(RSS, URL, NOW)
        children = list(parser._executor._processes.values())
        close_sources = app.state.services.sources.close
        def close():
            assert parser._closed  # Do not join I/O threads behind queued parses.
            assert all(not child.is_alive() for child in children)
            close_sources()
        monkeypatch.setattr(app.state.services.sources, 'close', close)
    assert parser._closed
    assert all(not child.is_alive() for child in children)
    assert module._shared is None
