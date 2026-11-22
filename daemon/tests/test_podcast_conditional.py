"""Conditional GETs reuse only a Source's last valid RSS representation."""
import random
from datetime import timedelta
from unittest.mock import Mock
from urllib.parse import urljoin

import httpx
import pytest

from fmheadendd.models.configuration import PodcastConfig
from fmheadendd.sources.podcast import EpisodeCatalog, eligible_episodes, parse_feed
from fmheadendd.sources.podcast_fetch import FeedFetcher, FeedResponse, probe_podcast
from fmheadendd.sources.podcast_history import EpisodeHistory
from fmheadendd.sources.podcast_schedule import PodcastScheduler
from fmheadendd.sources.podcast_service import PodcastFeedService
from fmheadendd.tests.test_podcast import NOW, OTHER, URL, config, item, rss


LAST_MODIFIED = 'Sun, 04 Oct 2026 10:00:00 GMT'


def conditions(request):
    return {header: request.headers[header] for header in ('if-none-match', 'if-modified-since')
            if header in request.headers}


def reply(status, headers=None, body=b''):
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


@pytest.mark.parametrize(('headers', 'expected'), [
    ({'etag': '"v1"'}, {'if-none-match': '"v1"'}),
    ({'last-modified': LAST_MODIFIED}, {'if-modified-since': LAST_MODIFIED}),
    ({'last-modified': 'Sunday, 04-Oct-26 10:00:00 GMT'}, {'if-modified-since': LAST_MODIFIED}),
    ({'etag': 'W/"v1"', 'last-modified': LAST_MODIFIED},
     {'if-none-match': 'W/"v1"', 'if-modified-since': LAST_MODIFIED}),
])
def test_refresh_sends_available_validators_and_skips_body_and_parser_on_304(headers, expected):
    class UnreadableBody(httpx.AsyncByteStream):
        def __aiter__(self):
            pytest.fail('304 body was read')

    def handler(request):
        if not conditions(request):
            return reply(200, headers, rss(item()))
        assert conditions(request) == expected
        return httpx.Response(304, stream=UnreadableBody())

    sent = Mock(side_effect=handler)
    transport = httpx.MockTransport(sent)
    parse = Mock(wraps=parse_feed)
    catalog = EpisodeCatalog(parse)
    first = catalog.refresh(config(), FeedFetcher(transport=transport), NOW)
    later = NOW + timedelta(minutes=30)
    second = catalog.refresh(config(), FeedFetcher(transport=transport), later)
    assert [conditions(call.args[0]) for call in sent.call_args_list] == [{}, expected]
    assert parse.call_count == 1
    assert second.feeds[0] is first.feeds[0]
    assert second.results == first.results and second.results[0].error is None
    assert second.refreshed_at == later and not catalog.refresh_due(config(), later)


def test_unchanged_feed_does_not_stop_refresh_and_changed_200_clears_missing_validators():
    feed_headers = [
        (URL, {}, 200, {'etag': '"one"', 'last-modified': LAST_MODIFIED}, 'One'),
        (OTHER, {}, 200, {'etag': '"two"'}, 'Two'),
        (URL, {'if-none-match': '"one"', 'if-modified-since': LAST_MODIFIED}, 304, {}, ''),
        (OTHER, {'if-none-match': '"two"'}, 200, {'etag': '"three"'}, 'New title'),
        (URL, {'if-none-match': '"one"', 'if-modified-since': LAST_MODIFIED}, 304, {}, ''),
        (OTHER, {'if-none-match': '"three"'}, 200, {}, 'No validators'),
        (URL, {'if-none-match': '"one"', 'if-modified-since': LAST_MODIFIED}, 304, {}, ''),
        (OTHER, {}, 200, {}, 'No validators'),
    ]

    def handler(request):
        url, expected, status, headers, title = feed_headers.pop(0)
        assert str(request.url) == url and conditions(request) == expected
        return reply(status, headers, rss(item(title=title)) if status == 200 else b'')

    parse = Mock(wraps=parse_feed)
    catalog = EpisodeCatalog(parse)
    cfg = PodcastConfig(feeds=[{'url': URL}, {'url': OTHER}])
    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    first, changed, missing, ordinary = [catalog.refresh(cfg, fetch, NOW + timedelta(hours=i)) for i in range(4)]
    assert not feed_headers and parse.call_count == 5
    assert all(view.feeds[0] is first.feeds[0] for view in (changed, missing, ordinary))
    original, new = first.feeds[1].episodes[0], changed.feeds[1].episodes[0]
    assert new.title == 'New title' and new.key == original.key and new.discovered_at == original.discovered_at
    assert new.last_seen_at > original.last_seen_at
    assert ordinary.feeds[1].episodes[0].title == 'No validators'


@pytest.mark.parametrize(('good_headers', 'condition', 'bad_status', 'bad_headers', 'expected_error'), [
    ({'etag': '"good"'}, {'if-none-match': '"good"'}, 503, {'etag': '"unusable"'}, 'PODCAST_FETCH_FAILED'),
    ({'etag': '"good"'}, {'if-none-match': '"good"'}, 200, {'etag': '"unusable"'}, 'PODCAST_FEED_INVALID'),
    ({'etag': '"good"'}, {'if-none-match': '"good"'}, 304, {'etag': '"unusable"'}, 'PODCAST_FETCH_FAILED'),
    ({'last-modified': LAST_MODIFIED}, {'if-modified-since': LAST_MODIFIED}, 304,
     {'last-modified': 'Sun, 04 Oct 2026 11:00:00 GMT'}, 'PODCAST_FETCH_FAILED'),
])
def test_unsuccessful_representation_keeps_catalog_and_its_validators(
        good_headers, condition, bad_status, bad_headers, expected_error):
    responses = iter([(200, good_headers, rss(item())), (bad_status, bad_headers, b'not XML'), (304, {}, b'')])
    sent = []

    def handler(request):
        sent.append(conditions(request))
        status, headers, data = next(responses)
        return reply(status, headers, data)

    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    catalog = EpisodeCatalog()
    first, failed, recovered = [catalog.refresh(config(), fetch, NOW + timedelta(hours=i)) for i in range(3)]
    assert failed.results[0].error == expected_error and recovered.results[0].error is None
    assert failed.feeds[0] is recovered.feeds[0] is first.feeds[0]
    assert sent == [{}, condition, condition]


def test_unconditional_304_is_safe_failure_on_first_refresh_and_next_refresh_recovers():
    responses = iter([reply(304, {'etag': '"unusable"'}), reply(200, body=rss(item()))])
    sent = Mock(side_effect=lambda request: next(responses))
    parse = Mock(wraps=parse_feed)
    catalog = EpisodeCatalog(parse)
    fetch = FeedFetcher(transport=httpx.MockTransport(sent))
    failed = catalog.refresh(config(), fetch, NOW)
    parse.assert_not_called()
    assert failed.feeds == () and failed.results[0].error == 'PODCAST_FETCH_FAILED'
    recovered = catalog.refresh(config(), fetch, NOW + timedelta(hours=1))
    assert recovered.results[0].error is None and len(recovered.feeds[0].episodes) == 1
    assert [conditions(call.args[0]) for call in sent.call_args_list] == [{}, {}]
    result = EpisodeCatalog(parse).refresh(config(), lambda url: FeedResponse(None, url), NOW)
    assert result.feeds == () and result.results[0].error == 'PODCAST_FETCH_FAILED'


@pytest.mark.parametrize('headers', [
    [('etag', '*'), ('last-modified', 'yesterday')],
    [('etag', 'unquoted'), ('last-modified', 'Sun, 40 Oct 2026 10:00:00 GMT')],
    [('etag', '"good"'), ('etag', '"duplicate"'), ('last-modified', LAST_MODIFIED), ('last-modified', LAST_MODIFIED)],
    [('etag', '"line\r\nbreak"'), ('last-modified', LAST_MODIFIED + '\r\nInjected: yes')],
    [('etag', '"' + 'x' * 1024 + '"')],
])
def test_malformed_or_oversized_validators_are_not_reused(headers):
    def handler(request):
        assert conditions(request) == {}
        return reply(200, headers, rss(item()))

    catalog = EpisodeCatalog()
    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    for i in range(2):
        assert catalog.refresh(config(), fetch, NOW + timedelta(hours=i)).results[0].error is None


def test_304_can_supply_a_weak_equivalent_etag_and_preserves_omitted_validators():
    requests = []

    def handler(request):
        requests.append(conditions(request))
        if not requests[-1]:
            return reply(200, {'etag': '"tag"', 'last-modified': LAST_MODIFIED}, rss(item()))
        return httpx.Response(304, headers={'etag': 'W/"tag"'})

    catalog = EpisodeCatalog()
    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    for i in range(3):
        assert catalog.refresh(config(), fetch, NOW + timedelta(hours=i)).results[0].error is None
    assert requests == [{}, {'if-none-match': '"tag"', 'if-modified-since': LAST_MODIFIED},
                        {'if-none-match': 'W/"tag"', 'if-modified-since': LAST_MODIFIED}]


@pytest.mark.parametrize('target', ['https://cdn.example.org/rss?new=1', 'https://other.example.org/rss'])
def test_redirect_validators_are_scoped_to_exact_effective_url(target):
    old_target = 'https://cdn.example.org/rss?old=1'
    redirects = iter([old_target, old_target, target, target])
    requests = []

    def handler(request):
        url = str(request.url)
        requests.append((url, conditions(request)))
        if url == URL:
            return httpx.Response(302, headers={'location': next(redirects), 'etag': '"redirect"'})
        if conditions(request):
            return httpx.Response(304)
        return reply(200, {'etag': '"same"'}, rss(item(media='episode.mp3')))

    catalog = EpisodeCatalog()
    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    first, old, changed, new = [catalog.refresh(config(), fetch, NOW + timedelta(hours=i)) for i in range(4)]
    assert requests == [(URL, {}), (old_target, {}), (URL, {}), (old_target, {'if-none-match': '"same"'}),
                        (URL, {}), (target, {}), (URL, {}), (target, {'if-none-match': '"same"'})]
    assert first.feeds[0] is old.feeds[0] and changed.feeds[0] is new.feeds[0]
    assert first.feeds[0].episodes[0].media_url == urljoin(old_target, 'episode.mp3')
    assert changed.feeds[0].episodes[0].media_url == urljoin(target, 'episode.mp3')
    assert first.feeds[0].feed_id == changed.feeds[0].feed_id


def test_feed_removal_drops_validators_before_readdition():
    sent = []

    def handler(request):
        sent.append((str(request.url), conditions(request)))
        if conditions(request):
            return httpx.Response(304)
        return reply(200, {'etag': '"v1"'}, rss(item()))

    catalog = EpisodeCatalog()
    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    both = PodcastConfig(feeds=[{'url': URL}, {'url': OTHER}])
    other = PodcastConfig(feeds=[{'url': OTHER}])
    catalog.refresh(both, fetch, NOW)
    catalog.refresh(other, fetch, NOW + timedelta(hours=1))
    catalog.refresh(both, fetch, NOW + timedelta(hours=2))
    assert sent == [(URL, {}), (OTHER, {}), (OTHER, {'if-none-match': '"v1"'}),
                    (URL, {}), (OTHER, {'if-none-match': '"v1"'})]


def test_service_schedules_304_normally_and_validators_are_local_to_source_lifetime():
    sent = []
    clock = [0]

    def handler(request):
        sent.append(conditions(request))
        if sent[-1]:
            return httpx.Response(304)
        return reply(200, {'etag': '"v1"'}, rss(item()))

    transport = httpx.MockTransport(handler)
    parse = Mock(wraps=parse_feed)
    history = Mock()
    history.snapshot_for_episodes.return_value = {}

    def service(source_id):
        return PodcastFeedService(config(), source_id=source_id, parser=parse,
            fetch_factory=lambda: FeedFetcher(transport=transport), clock=lambda: clock[0],
            now=lambda: NOW + timedelta(seconds=clock[0]))

    first, second = service(1), service(2)
    try:
        first.io_step(history)
        second.io_step(history)
        clock[0] = 1800
        first.io_step(history)
        assert first.snapshot().revision == 2 and first.snapshot().catalog.results[0].error is None
        clock[0] = 3599
        first.io_step(history)
        assert first.snapshot().revision == 2
        clock[0] = 3600
        first.io_step(history)
        assert first.snapshot().revision == 3 and second.snapshot().revision == 1
    finally:
        first.close()
        second.close()
    restarted = service(1)
    try:
        restarted.io_step(history)
        assert restarted.snapshot().revision == 1 and restarted.snapshot().catalog.results[0].error is None
    finally:
        restarted.close()
    assert sent == [{}, {}, {'if-none-match': '"v1"'}, {'if-none-match': '"v1"'}, {}]
    assert parse.call_count == 3


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_304_preserves_rotation_and_rechecks_max_age_and_episode_limit(mode):
    body = rss(item('new', title='Newest', date='Sun, 04 Oct 2026 11:00:00 GMT'),
               item('old', title='Older', date='Sat, 03 Oct 2026 13:00:00 GMT'),
               item('older', title='Oldest', date='Sat, 03 Oct 2026 12:00:00 GMT'))
    transport = httpx.MockTransport(lambda request: reply(304) if conditions(request)
                                   else reply(200, {'etag': '"v1"'}, body))
    catalog = EpisodeCatalog()
    fetch = FeedFetcher(transport=transport)
    cfg = PodcastConfig(feeds=[{'url': URL, 'max_age_days': 1, 'max_episodes': 2}], schedule_mode=mode)
    first = catalog.refresh(cfg, fetch, NOW)
    assert [e.title for e in eligible_episodes(first, cfg, NOW)] == ['Newest', 'Older']
    later = NOW + timedelta(minutes=30)
    unchanged = catalog.refresh(cfg, fetch, later)
    assert unchanged.feeds[0] is first.feeds[0]
    original, verified = PodcastScheduler(mode, rng=random.Random(4)), PodcastScheduler(mode, rng=random.Random(4))
    history = {first.feeds[0].episodes[0].key: EpisodeHistory(1, NOW - timedelta(days=1))}
    for _ in range(4):
        keep = eligible_episodes(first, cfg, later)
        seen = eligible_episodes(unchanged, cfg, later)
        assert original.choose_next(keep, history, later) == verified.choose_next(seen, history, later)
    aged = NOW + timedelta(hours=2)
    assert [e.title for e in eligible_episodes(unchanged, cfg, aged)] == ['Newest']


def test_standalone_probes_start_with_an_empty_catalog(monkeypatch):
    def handler(request):
        assert conditions(request) == {}
        return reply(200, {'etag': '"v1"'}, rss(item()))

    fetch = FeedFetcher(transport=httpx.MockTransport(handler))
    parse = Mock(wraps=parse_feed)
    monkeypatch.setattr('fmheadendd.sources.podcast_fetch.FeedFetcher', lambda: fetch)
    monkeypatch.setattr('fmheadendd.sources.podcast_parser.shared_parser', lambda: parse)
    for _ in range(2):
        result = probe_podcast(config())
        assert result.ok and result.podcast.feeds_succeeded == result.podcast.discovered_episodes == 1
    assert parse.call_count == 2
