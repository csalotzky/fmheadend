"""Deterministic RSS, eligibility, scheduling, transport and durable history."""
import gzip
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSourceConfig, PodcastConfig
from fmheadendd.sources.podcast import (EpisodeCatalog, CatalogSnapshot, PodcastFeed,
    parse_feed, eligible_episodes, feed_identity)
from fmheadendd.sources.podcast_fetch import FeedFetcher, FeedResponse
from fmheadendd.sources.podcast_history import PodcastHistory, EpisodeHistory
from fmheadendd.sources.podcast_schedule import PodcastScheduler

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
URL = 'https://example.org/rss'
OTHER = 'https://example.net/rss'


def config(**kwargs):
    return PodcastConfig(feeds=[{'url': URL}], **kwargs)


def item(id='one', *, title='Episode', date='Sun, 04 Oct 2026 10:00:00 GMT',
         media='https://cdn.example.org/download?id=1&amp;token=secret', mime='audio/mpeg'):
    return (f'<item><title>{title}</title>' + (f'<guid isPermaLink="false">{id}</guid>' if id else '') +
            (f'<pubDate>{date}</pubDate>' if date else '') +
            f'<enclosure url="{media}" type="{mime}"/></item>')


def rss(*items):
    return ('<rss version="2.0"><channel><title>Show &amp; Co</title>' + ''.join(items) +
            '</channel></rss>').encode()


def episodes(count=3):
    return parse_feed(rss(*(item(str(i), media=f'https://cdn.example.org/{i}') for i in range(count))), URL, NOW).episodes


def snapshot(entries):
    return CatalogSnapshot((PodcastFeed(feed_identity(URL), URL, 'Show', tuple(entries)),))


def test_rss_titles_enclosure_identity_and_media_item():
    e, = parse_feed(rss(item(title='&lt;b&gt;Actual&lt;/b&gt; &amp; title')), URL, NOW).episodes
    assert e.feed_title == 'Show & Co' and e.title == 'Actual & title'
    assert e.media_url == 'https://cdn.example.org/download?id=1&token=secret'
    assert e.media_item().locator == e.media_url
    assert e.published_at == NOW.replace(hour=10)
    assert e.discovered_at == e.last_seen_at == NOW
    changed, = parse_feed(rss(item(title='Changed', media='https://cdn.example.org/new')), URL, NOW).episodes
    assert e.key == changed.key
    other, = parse_feed(rss(item()), OTHER, NOW).episodes
    assert e.episode_id == other.episode_id and e.key != other.key


def test_atom_enclosure_and_timestamp():
    xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
      <entry><id>urn:episode:1</id><title>Title</title><published>2026-10-04T12:00:00+02:00</published>
      <link rel="enclosure" type="audio/ogg" href="https://cdn.example.org/get?episode=1"/>
      </entry></feed>'''
    feed = parse_feed(xml, URL, NOW)
    assert feed.title == 'Atom' and len(feed.episodes) == 1
    assert feed.episodes[0].published_at == NOW.replace(hour=10)


def test_missing_id_and_duplicate_entries_are_stable():
    first = item(None)
    parsed = parse_feed(rss(first, first, item('different', title='Same title')), URL, NOW)
    assert len(parsed.episodes) == 2  # distinct GUID remains distinct, even same audio
    a, = parse_feed(rss(first), URL, NOW).episodes
    b, = parse_feed(rss(item(None, title='Renamed')), URL, NOW).episodes
    assert a.key == b.key


@pytest.mark.parametrize('date', [None, 'not a date'])
def test_missing_or_bad_date_is_not_now(date):
    e, = parse_feed(rss(item(date=date)), URL, NOW).episodes
    assert e.published_at is None
    assert eligible_episodes(snapshot([e]), config(), NOW) == (e,)
    c = PodcastConfig(feeds=[dict(url=URL, max_age_days=30)])
    assert eligible_episodes(snapshot([e]), c, NOW) == ()


def test_bad_entry_does_not_destroy_feed_and_audio_preferred():
    multi = '''<item><guid>multi</guid><enclosure url="https://x.org/video.mp3" type="video/mp4"/>
        <enclosure url="https://x.org/unknown"/><enclosure url="https://x.org/audio" type="audio/mpeg"/>
        </item>'''
    feed = parse_feed(rss(item(media='file:///tmp/no'), item(media=''), multi), URL, NOW)
    assert [e.media_url for e in feed.episodes] == ['https://x.org/audio']


@pytest.mark.parametrize('data', [b'not XML', b'<html>not a feed</html>', b'<rss><channel><item>'])
def test_malformed_feed_is_error(data):
    with pytest.raises(DomainError, match='usable RSS/Atom'):
        parse_feed(data, URL, NOW)


@pytest.mark.parametrize('age,count,expected', [(None, None, 4), (2, None, 2),
    (None, 2, 2), (2, 1, 1), (0, None, 1), (None, 0, 0)])
def test_combined_limits_and_exact_boundary(age, count, expected):
    es = [replace(e, published_at=NOW-timedelta(days=d)) for e, d in zip(episodes(4), [0, 2, 3, 30])]
    c = PodcastConfig(feeds=[dict(url=URL, max_age_days=age, max_episodes=count)])
    eligible = eligible_episodes(snapshot(es), c, NOW)
    assert len(eligible) == expected
    assert list(eligible) == es[:expected]


def test_undated_order_independent_of_feed_order_and_removed_feeds():
    es = [replace(e, published_at=None) for e in episodes()]
    c = PodcastConfig(feeds=[dict(url=URL, max_episodes=2)])
    assert eligible_episodes(snapshot(es), c, NOW) == eligible_episodes(snapshot(reversed(es)), c, NOW)
    assert not eligible_episodes(snapshot(es), PodcastConfig(feeds=[dict(url=OTHER)]), NOW)
    with pytest.raises(DomainError) as exc:
        eligible_episodes(snapshot(es), c.model_copy(update={'feeds': []}), NOW)
    assert exc.value.error == 'NO_PODCAST_FEEDS'


def test_refresh_partial_total_failure_retention_and_interval():
    c = PodcastConfig(feeds=[dict(url=URL), dict(url=OTHER)])
    catalog = EpisodeCatalog()
    data = {URL: rss(item()), OTHER: rss(item('two'))}
    def fetch(url):
        if data[url] is None:
            raise DomainError(503, 'PODCAST_FETCH_TIMEOUT', 'secret must not be logged')
        return FeedResponse(data[url], url)
    assert catalog.refresh_due(c, NOW)
    first = catalog.refresh(c, fetch, NOW)
    assert len(first.feeds) == 2
    assert not catalog.refresh_due(c, NOW+timedelta(minutes=29))
    assert catalog.refresh_due(c, NOW+timedelta(minutes=30))
    data[URL], data[OTHER] = rss(item('new')), None
    second = catalog.refresh(c, fetch, NOW+timedelta(hours=1))
    assert second.feeds[1] == first.feeds[1]
    assert second.feeds[0] != first.feeds[0]
    data[URL] = None
    third = catalog.refresh(c, fetch, NOW+timedelta(hours=2))
    assert third.feeds == second.feeds and all(r.error for r in third.results)
    assert first.feeds[0].episodes[0].title == 'Episode'
    removed = catalog.refresh(config(), fetch, NOW+timedelta(hours=3))
    assert len(removed.feeds) == 1


def test_empty_success_replaces_old_feed_and_discovery_time_survives():
    catalog = EpisodeCatalog()
    fetch = lambda url: FeedResponse(rss(item()), url)
    a = catalog.refresh(config(), fetch, NOW)
    b = catalog.refresh(config(), fetch, NOW+timedelta(days=1))
    assert b.feeds[0].episodes[0].discovered_at == NOW
    assert b.feeds[0].episodes[0].last_seen_at == NOW+timedelta(days=1)
    assert a.feeds[0].episodes[0].last_seen_at == NOW
    assert not catalog.refresh(config(), lambda u: FeedResponse(rss(), u), NOW).feeds[0].episodes


def test_random_bag_cycles_and_boundary():
    scheduler = PodcastScheduler('RANDOM', rng=random.Random(42))
    es = episodes(5)
    previous = None
    for _ in range(6):
        cycle = [scheduler.choose_next(es, {}, NOW) for _ in es]
        assert set(cycle) == set(es) and len(set(cycle)) == len(es)
        assert cycle[0] != previous
        previous = cycle[-1]


def test_random_refresh_prunes_removed_adds_next_cycle_and_updates_urls():
    es = episodes(4)
    s = PodcastScheduler('RANDOM', rng=random.Random(3))
    first = s.choose_next(es[:3], {}, NOW)
    remaining = [replace(e, media_url=e.media_url+'?new') for e in es[:3] if e != first]
    changed = remaining + [es[3]]
    # Fresh object values are used; additions wait until current remaining bag ends.
    cycle = [s.choose_next(changed, {}, NOW) for _ in remaining]
    assert set(cycle) == set(remaining)
    assert es[3] in [s.choose_next(changed, {}, NOW) for _ in changed]
    assert s.choose_next([es[3]], {}, NOW) == es[3]


@pytest.mark.parametrize('mode', ['RANDOM', 'SMART'])
def test_empty_and_single_scheduler(mode):
    s = PodcastScheduler(mode, rng=random.Random(1))
    with pytest.raises(DomainError) as exc:
        s.choose_next([], {}, NOW)
    assert exc.value.error == 'NO_PLAYABLE_EPISODES'
    e, = episodes(1)
    assert s.choose_next([e], {}, NOW) == s.choose_next([e], {}, NOW) == e


def test_smart_unplayed_newer_diversity_and_stable_ties():
    a, b, c = episodes()
    a = replace(a, published_at=NOW)
    b = replace(b, published_at=NOW-timedelta(hours=1), feed_id=feed_identity(OTHER))
    c = replace(c, published_at=NOW-timedelta(days=10))
    s = PodcastScheduler()
    assert s.choose_next([c, b, a], {}, NOW) == a
    assert s.choose_next([a, b, c], {}, NOW) == b  # similarly fresh, other feed
    history = {a.key: EpisodeHistory(1, NOW-timedelta(days=2))}
    assert PodcastScheduler().choose_next([a, c], history, NOW) == c
    assert PodcastScheduler().choose_next([b, a], {}, NOW) == a
    assert PodcastScheduler().choose_next([a, b], {}, NOW) == a


def test_smart_counts_then_oldest_play_then_newest():
    a, b = episodes(2)
    history = {a.key: EpisodeHistory(2, NOW-timedelta(days=5)), b.key: EpisodeHistory(1, NOW-timedelta(days=2))}
    assert PodcastScheduler().choose_next([a,b], history, NOW) == b
    history[b.key] = EpisodeHistory(2, NOW-timedelta(days=2))
    assert PodcastScheduler().choose_next([b,a], history, NOW) == a


def test_cooldown_soft_and_exact_boundary():
    a, b = episodes(2)
    history = {a.key: EpisodeHistory(1, NOW-timedelta(hours=1)), b.key: EpisodeHistory(2, NOW-timedelta(hours=24))}
    assert PodcastScheduler().choose_next([a,b], history, NOW) == b
    history[b.key] = EpisodeHistory(2, NOW-timedelta(hours=2))
    assert PodcastScheduler().choose_next([a,b], history, NOW) == a
    assert PodcastScheduler().choose_next([a,b], history, NOW, replay_cooldown_hours=0) == a


def test_history_reopen_retention_and_source_namespace(tmp_path):
    path = tmp_path/'podcast.db'
    db = Database(path)
    cfg = InputSourceConfig(name='Podcast', type='PODCAST', config=config().model_dump(mode='json'))
    sid = db.save('sources', cfg).id
    other_sid = db.save('sources', cfg).id
    h = PodcastHistory(db, sid)
    e, = episodes(1)
    h.observe([e], NOW)
    assert h.snapshot()[e.key] == EpisodeHistory(0, None, NOW)
    h.mark_played(e, NOW)
    h.mark_played(e, NOW+timedelta(hours=2))
    h.observe([], NOW+timedelta(days=1))
    assert not PodcastHistory(db, other_sid).snapshot()
    db.close()
    db = Database(path)
    h = PodcastHistory(db, sid)
    assert h.snapshot()[e.key] == EpisodeHistory(2, NOW+timedelta(hours=2), NOW)
    catalog = EpisodeCatalog()
    # Same GUID reconnects after absence; refresh observes even ineligible entries.
    c = PodcastConfig(feeds=[dict(url=URL, max_episodes=0)])
    catalog.refresh(c, lambda u: FeedResponse(rss(item('0', media='https://cdn.example.org/0')), u),
                    NOW+timedelta(days=3), history=h)
    assert h.snapshot()[e.key].play_count == 2
    assert h.snapshot()[e.key].last_seen_at == NOW+timedelta(days=3)
    assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 1
    db.delete('sources', sid)
    assert not h.snapshot()
    db.close()


def fetcher(handler, **kwargs):
    return FeedFetcher(transport=httpx.MockTransport(handler), **kwargs)


def test_transport_redirect_utf8_gzip_and_safe_request_headers():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={'location': '/final'})
        return httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=httpx.ByteStream(gzip.compress(rss(item()))))
    result = fetcher(handler)(URL)
    assert result.body == rss(item()) and result.url == 'https://example.org/final'
    assert calls[0].headers['user-agent'].startswith('FMHeadend/')


@pytest.mark.parametrize('kind', ['size', 'gzip_bomb', 'redirect_loop', 'redirect_file', 'timeout', 'http', 'encoding'])
def test_transport_failures_are_bounded_and_sanitized(kind):
    def handler(request):
        if kind == 'timeout':
            raise httpx.ReadTimeout('secret-token')
        if kind.startswith('redirect'):
            return httpx.Response(302, headers={'location': URL if kind == 'redirect_loop' else 'file:///etc/passwd'})
        if kind == 'http':
            return httpx.Response(500)
        if kind == 'encoding':
            return httpx.Response(200, headers={'content-encoding': 'br'}, content=b'data')
        body = b'x'*1000
        return httpx.Response(200, stream=httpx.ByteStream(gzip.compress(body) if kind == 'gzip_bomb' else body),
                              headers={'content-encoding': 'gzip' if kind == 'gzip_bomb' else 'identity'})
    with pytest.raises(DomainError) as exc:
        fetcher(handler, max_bytes=100)(URL+'?secret-token')
    assert 'secret-token' not in str(exc.value)


def test_expired_refresh_budget_does_not_issue_request():
    ticks = iter([0, 21])
    with pytest.raises(DomainError) as exc:
        fetcher(lambda r: pytest.fail('network after deadline'), clock=lambda: next(ticks))(URL)
    assert exc.value.error == 'PODCAST_FETCH_TIMEOUT'


def test_snapshot_is_not_partially_published_during_refresh():
    from threading import Event, Thread
    c = PodcastConfig(feeds=[dict(url=URL), dict(url=OTHER)])
    catalog = EpisodeCatalog()
    old = catalog.refresh(c, lambda u: FeedResponse(rss(item('old')), u), NOW)
    entered, release = Event(), Event()
    def fetch(url):
        if url == OTHER:
            entered.set()
            assert release.wait(3)
        return FeedResponse(rss(item('new')), url)
    thread = Thread(target=lambda: catalog.refresh(c, fetch, NOW))
    thread.start()
    try:
        assert entered.wait(3)
        assert catalog.snapshot() is old
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert all(f.episodes[0].episode_id != old.feeds[0].episodes[0].episode_id for f in catalog.snapshot().feeds)


def test_smart_fresh_programming_can_beat_unplayed_archives_and_undated_work():
    a, b = episodes(2)
    b = replace(b, published_at=NOW-timedelta(days=365))
    h = {a.key: EpisodeHistory(1, NOW-timedelta(days=2))}
    assert PodcastScheduler().choose_next([a, b], h, NOW) == a
    undated = [replace(e, published_at=None) for e in (a,b)]
    assert PodcastScheduler().choose_next(undated, h, NOW) == undated[1]


def test_random_fixed_seed_is_reproducible_and_new_smart_item_visible():
    es = episodes(5)
    def picks(seed):
        s = PodcastScheduler('RANDOM', rng=random.Random(seed))
        return [s.choose_next(es, {}, NOW).key for _ in range(10)]
    assert picks(7) == picks(7)
    s = PodcastScheduler()
    h = {e.key: EpisodeHistory(1, NOW-timedelta(days=2)) for e in es[:4]}
    s.choose_next(es[:4], h, NOW)
    assert s.choose_next(es, h, NOW) == es[4]


def test_timeout_while_streaming_and_truncated_gzip():
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'first'
            yield b'second'
    ticks = iter([0, 0, 1, 21])
    with pytest.raises(DomainError) as exc:
        fetcher(lambda r: httpx.Response(200, stream=Stream()), clock=lambda: next(ticks))(URL)
    assert exc.value.error == 'PODCAST_FETCH_TIMEOUT'
    with pytest.raises(DomainError) as exc:
        fetcher(lambda r: httpx.Response(200, stream=httpx.ByteStream(gzip.compress(b'body')[:-4]),
                                        headers={'content-encoding': 'gzip'}))(URL)
    assert exc.value.error == 'PODCAST_ENCODING_ERROR'


def test_relative_enclosure_uses_redirect_base_but_original_feed_identity():
    e, = parse_feed(rss(item(media='audio/1')), URL, NOW,
                   base_url='https://new.example.org/feed.xml').episodes
    assert e.media_url == 'https://new.example.org/audio/1'
    assert e.feed_id == feed_identity(URL)


def test_feed_warnings_do_not_log_credentials(caplog):
    catalog = EpisodeCatalog()
    def fetch(url):
        raise DomainError(503, 'PODCAST_FETCH_FAILED', 'https://user:password@host/?secret')
    catalog.refresh(config(), fetch, NOW)
    assert 'secret' not in caplog.text and 'password' not in caplog.text
    assert 'PODCAST_FETCH_FAILED' in caplog.text


def test_history_failed_refresh_does_not_change_last_seen(tmp_path):
    db = Database(tmp_path/'failed.db')
    sid = db.save('sources', InputSourceConfig(name='Podcast', type='PODCAST', config=config().model_dump(mode='json'))).id
    history = PodcastHistory(db, sid)
    catalog = EpisodeCatalog()
    catalog.refresh(config(), lambda u: FeedResponse(rss(item()),u), NOW, history=history)
    before = history.snapshot()
    def failed(url):
        raise DomainError(503, 'PODCAST_FETCH_TIMEOUT', 'Timeout')
    catalog.refresh(config(), failed, NOW+timedelta(days=1), history=history)
    assert history.snapshot() == before
    db.close()
