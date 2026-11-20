"""Bounded Apple Podcasts directory search."""
import asyncio
import json
import time
from collections import OrderedDict, deque
from email.utils import parsedate_to_datetime
from math import ceil
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from . import __version__
from .errors import DomainError
from .sources.podcast import valid_http_url

MAX_QUERY_LENGTH = 200
MAX_RESULTS = 200
MAX_BYTES = 2 * 1024 * 1024
MAX_CONCURRENT = 2
REQUEST_TIMEOUT = 12
RATE_LIMIT = 15
RATE_WINDOW = 60
CACHE_TTL = 300
CACHE_SIZE = 64


class PodcastResult(BaseModel, frozen=True):
    title: str
    publisher: str | None = None
    category: str | None = None
    episode_count: int | None = None
    artwork_url: str | None = None
    feed_url: str


class Podcasts(BaseModel, frozen=True):
    podcasts: tuple[PodcastResult, ...]


def _display_text(value):
    if not isinstance(value, str):
        return None
    # Keep invalid Unicode from breaking serialization of an otherwise usable row.
    text = value.strip()[:500]
    return text.encode('utf-8', errors='replace').decode('utf-8') or None


def _http_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192:
        return None
    try:
        value.encode('utf-8')
        return valid_http_url(value)
    except ValueError:
        return None


def _normalize(rows):
    podcasts = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') != 'podcast':
            continue
        feed_url = _http_url(row.get('feedUrl'))
        title = _display_text(row.get('collectionName')) or _display_text(row.get('trackName'))
        if not feed_url or not title or feed_url in podcasts:
            continue
        category = _display_text(row.get('primaryGenreName'))
        genres = row.get('genres')
        if not category and isinstance(genres, list):
            labels = map(_display_text, genres)
            category = next((label for label in labels if label and label.casefold() != 'podcasts'), None)
        artwork = _http_url(row.get('artworkUrl100'))
        count = row.get('trackCount')
        podcasts[feed_url] = PodcastResult(
            title=title,
            publisher=_display_text(row.get('artistName')),
            category=category,
            episode_count=count if type(count) is int and count >= 0 else None,
            artwork_url=artwork if artwork and urlsplit(artwork).scheme == 'https' else None,
            feed_url=feed_url,
        )
    return Podcasts(podcasts=tuple(podcasts.values()))


def _retry_seconds(value, wall_clock):
    if value:
        value = value.strip()
        if value.isascii() and value.isdecimal() and len(value) <= 9:
            return max(1, int(value))
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(1, ceil(date.timestamp() - wall_clock()))
        except (ValueError, OverflowError, OSError):
            pass
    return RATE_WINDOW


def _throttled(seconds):
    return DomainError(
        429, 'PODCAST_BROWSER_RATE_LIMITED', 'Podcast search is rate limited. Try again shortly.',
        headers={'Retry-After': str(max(1, ceil(seconds))), 'Cache-Control': 'no-store'},
    )


class PodcastBrowserClient:
    def __init__(self, *, transport=None, clock=time.monotonic, wall_clock=time.time):
        self._clock = clock
        self._wall_clock = wall_clock
        self._cache: OrderedDict[str, tuple[float, Podcasts]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[Podcasts]] = {}
        self._starts: deque[float] = deque()
        self._cooldown_until = float('-inf')
        self._closing = False
        self.http = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(5, connect=3, pool=2),
            limits=httpx.Limits(max_connections=MAX_CONCURRENT, max_keepalive_connections=MAX_CONCURRENT),
            headers={'User-Agent': f'FMheadend/{__version__}', 'Accept': 'application/json',
                     'Accept-Encoding': 'identity'},
        )

    async def close(self):
        self._closing = True
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()
        await self.http.aclose()

    async def search(self, name: str) -> Podcasts:
        name = name.strip()
        if not 1 <= len(name) <= MAX_QUERY_LENGTH:
            raise DomainError(422, 'VALIDATION_ERROR', 'Enter a podcast name of 1 to 200 characters.',
                              headers={'Cache-Control': 'no-store'})
        if self._closing:
            raise self._unavailable()

        now = self._clock()
        cached = self._cache.get(name)
        if cached is not None:
            expires, result = cached
            if now < expires:
                self._cache.move_to_end(name)
                return result
            del self._cache[name]

        # Admission has no await, so callers on the daemon's event loop share a single budget.
        task = self._inflight.get(name)
        if task is None:
            if now < self._cooldown_until:
                raise _throttled(self._cooldown_until - now)
            while self._starts and self._starts[0] <= now - RATE_WINDOW:
                self._starts.popleft()
            if len(self._starts) >= RATE_LIMIT:
                raise _throttled(self._starts[0] + RATE_WINDOW - now)
            if len(self._inflight) >= MAX_CONCURRENT:
                raise _throttled(1)
            self._starts.append(now)
            task = asyncio.create_task(self._search_and_cache(name))
            self._inflight[name] = task
            task.add_done_callback(lambda finished: self._finish(name, finished))
        # Cancelling one HTTP request must not cancel a search shared with other callers.
        return await asyncio.shield(task)

    def _finish(self, name, task):
        del self._inflight[name]
        if not task.cancelled():
            task.exception()  # A disconnected caller may have left the task without a waiter.

    async def _search_and_cache(self, name):
        result = await self._fetch(name)
        self._cache[name] = (self._clock() + CACHE_TTL, result)
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return result

    @staticmethod
    def _unavailable():
        return DomainError(
            503, 'PODCAST_BROWSER_UNAVAILABLE', 'Apple Podcasts search is temporarily unavailable.',
            headers={'Cache-Control': 'no-store'},
        )

    async def _fetch(self, name):
        params = dict(term=name, media='podcast', entity='podcast', attribute='titleTerm',
                      country='HU', limit=MAX_RESULTS)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self.http.stream('GET', 'https://itunes.apple.com/search', params=params) as response:
                    if response.status_code == 429:
                        now = self._clock()
                        retry = _retry_seconds(response.headers.get('Retry-After'), self._wall_clock)
                        self._cooldown_until = max(self._cooldown_until, now + retry)
                        raise _throttled(self._cooldown_until - now)
                    response.raise_for_status()
                    if response.headers.get('content-encoding', 'identity').strip().lower() != 'identity':
                        raise ValueError('Unexpected compressed response')
                    body = bytearray()
                    async for block in response.aiter_bytes():
                        if len(body) + len(block) > MAX_BYTES:
                            raise ValueError('Response too large')
                        body.extend(block)
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError('Invalid search envelope')
                rows = data.get('results')
                count = data.get('resultCount')
                if (not isinstance(rows, list) or type(count) is not int
                        or count != len(rows) or count > MAX_RESULTS):
                    raise ValueError('Invalid search result page')
                return _normalize(rows)
        except (httpx.HTTPError, OSError, ValueError, RecursionError):
            raise self._unavailable() from None
