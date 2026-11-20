import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from fmheadendd import podcast_browser
from fmheadendd.app import create_app
from fmheadendd.errors import DomainError
from fmheadendd.podcast_browser import PodcastBrowserClient
from fmheadendd.tests.profile_support import profile_settings

SEARCH = '/api/v1/podcast-browser/search'
ROW = {
    'kind': 'podcast',
    'collectionName': ' Example Show ',
    'artistName': ' Example Publisher ',
    'primaryGenreName': 'News',
    'trackCount': 0,
    'artworkUrl100': 'https://artwork.example.test/cover.jpg',
    'feedUrl': 'https://feeds.example.test/Show?token=One%2FTwo',
    'releaseDate': '2026-11-05T09:00:00Z',
    'collectionId': 123,
}


def apple_response(rows=()):
    return httpx.Response(200, json={'resultCount': len(rows), 'results': list(rows)})


def make_client(handler, **kwargs):
    return PodcastBrowserClient(transport=httpx.MockTransport(handler), **kwargs)


def test_search_route_normalizes_results_without_source_mutations(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return apple_response([
            ROW,
            ROW | {'collectionName': 'Duplicate'},
            ROW | {'feedUrl': 'http://feeds.example.test/other', 'collectionName': '',
                   'trackName': 'Fallback', 'artistName': [], 'primaryGenreName': None,
                   'genres': [None, 'Podcasts', 'Local History'], 'trackCount': True,
                   'artworkUrl100': 'http://artwork.example.test/cover.jpg'},
            ROW | {'feedUrl': None},
            ROW | {'feedUrl': 'https://name:password@feeds.example.test/show'},
            ROW | {'feedUrl': 'https://feeds.example.test:99999/show'},
            ROW | {'feedUrl': 'https://feeds.example.test/bad path'},
            ROW | {'feedUrl': 'ftp://feeds.example.test/show'},
            ROW | {'feedUrl': 'https://feeds.example.test/' + 'a' * 8192},
            ROW | {'feedUrl': 'https://feeds.example.test/no-title', 'collectionName': ' '},
            ROW | {'feedUrl': 'https://feeds.example.test/episode', 'kind': 'podcast-episode'},
            'bad row',
        ])

    client = make_client(handler)
    with TestClient(create_app(profile_settings(tmp_path / 'podcasts.db'), podcast_browser_client=client)) as api:
        before = api.get('/api/v1/sources').json()
        for params in ({}, {'name': ''}, {'name': '   '}, {'name': 'a' * 201}):
            invalid = api.get(SEARCH, params=params)
            assert invalid.status_code == 422
            assert invalid.json()['error'] == 'VALIDATION_ERROR'
        assert seen == []
        result = api.get(SEARCH, params={'name': '  Árvíz & hírek  ', 'country': 'US', 'limit': 1})
        assert result.status_code == 200
        assert result.headers['cache-control'] == 'no-store'
        assert result.json() == {'podcasts': [
            {'title': 'Example Show', 'publisher': 'Example Publisher', 'category': 'News',
             'episode_count': 0, 'artwork_url': ROW['artworkUrl100'], 'feed_url': ROW['feedUrl']},
            {'title': 'Fallback', 'publisher': None, 'category': 'Local History',
             'episode_count': None, 'artwork_url': None, 'feed_url': 'http://feeds.example.test/other'},
        ]}
        assert api.get(SEARCH, params={'name': 'Árvíz & hírek'}).json() == result.json()
        assert api.get('/api/v1/sources').json() == before
    assert client.http.is_closed
    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.scheme, request.url.host, request.url.path) == (
        'GET', 'https', 'itunes.apple.com', '/search',
    )
    assert dict(request.url.params) == dict(term='Árvíz & hírek', media='podcast', entity='podcast',
                                           attribute='titleTerm', country='HU', limit='200')
    assert 'authorization' not in request.headers


def test_search_accepts_200_results_in_apple_order():
    rows = [ROW | {'feedUrl': f'https://feeds.example.test/{i}'} for i in range(200)]

    async def run():
        client = make_client(lambda request: apple_response(rows))
        try:
            result = await client.search('show')
            assert [podcast.feed_url for podcast in result.podcasts] == [row['feedUrl'] for row in rows]
        finally:
            await client.close()

    asyncio.run(run())


class OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(podcast_browser.MAX_BYTES // 16384):
            yield b' ' * 16384
        yield b' '
        pytest.fail('The client continued reading past its response limit')


@pytest.mark.parametrize('failure', [
    'network', 'read_timeout', 'deadline', '5xx', 'redirect', 'bad_json', 'shape',
    'wrong_count', 'too_many', 'oversized', 'compressed',
])
def test_failures_are_sanitized_bounded_and_not_cached(failure, monkeypatch):
    attempts = 0
    too_many = [ROW] * 201
    if failure == 'deadline':
        monkeypatch.setattr(podcast_browser, 'REQUEST_TIMEOUT', 0.01)

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return apple_response()
        if failure == 'network':
            raise httpx.ConnectError('private upstream detail', request=request)
        if failure == 'read_timeout':
            raise httpx.ReadTimeout('private upstream detail', request=request)
        if failure == 'deadline':
            await asyncio.Event().wait()
        if failure == 'oversized':
            return httpx.Response(200, stream=OversizedStream())
        return {
            '5xx': lambda: httpx.Response(500, text='private upstream detail'),
            'redirect': lambda: httpx.Response(302, headers={'Location': 'https://artwork.example.test'}),
            'bad_json': lambda: httpx.Response(200, text='private upstream detail'),
            'shape': lambda: httpx.Response(200, json=[]),
            'wrong_count': lambda: httpx.Response(200, json={'resultCount': 1, 'results': []}),
            'too_many': lambda: apple_response(too_many),
            'compressed': lambda: httpx.Response(200, stream=httpx.ByteStream(b'bad gzip'),
                                                headers={'Content-Encoding': 'gzip'}),
        }[failure]()

    async def run():
        client = make_client(handler)
        try:
            with pytest.raises(DomainError) as error:
                await client.search('show')
            assert (error.value.status, error.value.error) == (503, 'PODCAST_BROWSER_UNAVAILABLE')
            assert 'private upstream detail' not in str(error.value)
            assert attempts == 1
            assert (await client.search('show')).podcasts == ()
            assert attempts == 2
        finally:
            await client.close()

    asyncio.run(run())


def test_cache_expires_and_evicts_least_recently_used_query(monkeypatch):
    monkeypatch.setattr(podcast_browser, 'CACHE_SIZE', 2)
    calls = []
    now = 0

    def handler(request):
        calls.append(request.url.params['term'])
        return apple_response()

    async def run():
        nonlocal now
        client = make_client(handler, clock=lambda: now)
        try:
            for query in ('one', 'two', ' one ', 'three', 'one', 'two'):
                assert (await client.search(query)).podcasts == ()
            assert calls == ['one', 'two', 'three', 'two']
            now = podcast_browser.CACHE_TTL - 1
            await client.search('one')
            assert calls.count('one') == 1
            now += 1
            await client.search('one')
            assert calls.count('one') == 2
        finally:
            await client.close()

    asyncio.run(run())


def test_rolling_rate_limit_applies_to_upstream_starts(tmp_path):
    calls = []
    now = 0

    def handler(request):
        calls.append(request.url.params['term'])
        return apple_response()

    client = make_client(handler, clock=lambda: now)
    with TestClient(create_app(profile_settings(tmp_path / 'rate.db'), podcast_browser_client=client)) as api:
        for index in range(15):
            assert api.get(SEARCH, params={'name': f'show {index}'}).status_code == 200
        now = 23
        limited = api.get(SEARCH, params={'name': 'extra'})
        assert limited.status_code == 429
        assert limited.json()['error'] == 'PODCAST_BROWSER_RATE_LIMITED'
        assert limited.headers['retry-after'] == '37'
        assert limited.headers['cache-control'] == 'no-store'
        assert api.get(SEARCH, params={'name': 'show 0'}).status_code == 200
        assert len(calls) == 15
        now = 60
        assert api.get(SEARCH, params={'name': 'extra'}).status_code == 200
        assert len(calls) == 16


@pytest.mark.parametrize(('retry_after', 'seconds'), [
    ('12', 12),
    (format_datetime(datetime.fromtimestamp(1020, timezone.utc), usegmt=True), 20),
    ('invalid', 60),
    (None, 60),
])
def test_apple_throttling_pauses_other_queries(retry_after, seconds):
    calls = []
    now = 0

    def handler(request):
        query = request.url.params['term']
        calls.append(query)
        if query == 'limited':
            headers = {} if retry_after is None else {'Retry-After': retry_after}
            return httpx.Response(429, headers=headers)
        return apple_response()

    async def run():
        nonlocal now
        client = make_client(handler, clock=lambda: now, wall_clock=lambda: 1000)
        try:
            await client.search('cached')
            with pytest.raises(DomainError) as error:
                await client.search('limited')
            assert error.value.status == 429
            assert error.value.headers['Retry-After'] == str(seconds)
            now = seconds - 1
            with pytest.raises(DomainError) as error:
                await client.search('new')
            assert error.value.headers['Retry-After'] == '1'
            await client.search('cached')
            assert calls == ['cached', 'limited']
            now = seconds
            await client.search('new')
            assert calls == ['cached', 'limited', 'new']
        finally:
            await client.close()

    asyncio.run(run())


def test_shared_search_survives_caller_cancellation_and_limits_concurrency():
    async def run():
        entered = asyncio.Queue()
        release = asyncio.Event()

        async def handler(request):
            entered.put_nowait(request.url.params['term'])
            await release.wait()
            return apple_response([ROW])

        client = make_client(handler)
        try:
            first = asyncio.create_task(client.search('show'))
            assert await entered.get() == 'show'
            other = asyncio.create_task(client.search('other'))
            assert await entered.get() == 'other'
            shared = asyncio.create_task(client.search(' show '))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(DomainError) as error:
                await client.search('saturated')
            assert error.value.status == 429
            release.set()
            assert (await shared).podcasts[0].title == 'Example Show'
            assert (await other).podcasts
            assert entered.empty()
            assert (await client.search('saturated')).podcasts
            assert entered.get_nowait() == 'saturated'
        finally:
            await client.close()

    asyncio.run(run())


def test_close_cancels_inflight_upstream_search():
    async def run():
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        client = make_client(handler)
        pending = asyncio.create_task(client.search('show'))
        await entered.wait()
        await client.close()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert stopped.is_set()
        assert client.http.is_closed

    asyncio.run(run())
