"""Bounded feed-only HTTP transport. No audio downloads or implicit retries."""
import asyncio
import re
import time
import zlib
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from .. import __version__
from ..errors import DomainError
from .podcast import FeedValidators, valid_http_url


GZIP_CHUNK_BYTES = 64 * 1024
_ETAG = re.compile(r'(?:W/)?"[!#-~]*"')
_HTTP_DATE = re.compile(
    r'(?:[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9:]{8} GMT'
    r'|[A-Za-z]+, [0-9]{2}-[A-Za-z]{3}-[0-9]{2} [0-9:]{8} GMT'
    r'|[A-Za-z]{3} [A-Za-z]{3} [ 0-9][0-9] [0-9:]{8} [0-9]{4})')


def _single_header(headers, name):
    values = headers.get_list(name)
    if len(values) == 1 and len(values[0]) <= 1024:
        return values[0].strip(' \t')
    return ''


def _response_validators(headers, url):
    tag = _single_header(headers, 'etag')
    etag = tag if _ETAG.fullmatch(tag) else None
    last_modified = None
    date = _single_header(headers, 'last-modified')
    if _HTTP_DATE.fullmatch(date):
        try:
            last_modified = format_datetime(parsedate_to_datetime(date).replace(tzinfo=timezone.utc), usegmt=True)
        except (ValueError, OverflowError):
            pass
    return FeedValidators(url, etag, last_modified) if etag is not None or last_modified is not None else None


@dataclass(frozen=True)
class FeedResponse:
    body: bytes | None  # None only for a validated HTTP 304 response.
    url: str
    validators: FeedValidators | None = None


class FeedFetcher:
    def __init__(self, *, timeout_s=10.0, total_timeout_s=20.0, max_bytes=64 * 1024 * 1024,
                 clock=time.monotonic, transport=None):
        self.timeout_s, self.max_bytes = timeout_s, max_bytes
        self.clock, self.total_timeout_s = clock, total_timeout_s
        self.transport = transport

    def __call__(self, url: str, *, validators: FeedValidators | None = None) -> FeedResponse:
        deadline = self.clock() + self.total_timeout_s
        # The refresh and probe both run in synchronous worker threads. Closing
        # this loop without joining its executor lets a timed-out OS DNS call
        # finish separately, instead of delaying the next feed during teardown.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._fetch(url, deadline, validators))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise DomainError(503, "PODCAST_FETCH_TIMEOUT", "Feed request timed out") from exc
        except (httpx.HTTPError, ValueError, zlib.error) as exc:
            raise DomainError(503, "PODCAST_FETCH_FAILED", "Feed request failed") from exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def _remaining(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("Feed download deadline exceeded")
        return remaining

    async def _fetch(self, url: str, deadline: float, validators: FeedValidators | None) -> FeedResponse:
        # HTTPX limits each network operation. This outer timer also cancels
        # an awaited operation when the feed's remaining time runs out.
        async with asyncio.timeout(self._remaining(deadline)):
            async with httpx.AsyncClient(
                transport=self.transport, follow_redirects=False, trust_env=False,
                headers={"User-Agent": f"FMHeadend/{__version__} Podcast catalog",
                         "Accept-Encoding": "identity"},
            ) as client:
                for redirect in range(6):
                    valid_http_url(url)
                    timeout = min(self.timeout_s, self._remaining(deadline))
                    headers = {}
                    if validators is not None and validators.url == url:
                        if validators.etag is not None:
                            headers['If-None-Match'] = validators.etag
                        if validators.last_modified is not None:
                            headers['If-Modified-Since'] = validators.last_modified
                    async with client.stream("GET", url, timeout=timeout, headers=headers) as response:
                        self._remaining(deadline)
                        if response.status_code == 304:
                            if not headers:
                                raise DomainError(503, "PODCAST_FETCH_FAILED", "Unexpected not-modified feed response")
                            updated = _response_validators(response.headers, url)
                            if updated is not None:
                                if validators.etag is not None:
                                    mismatch = (updated.etag is not None and
                                                updated.etag.removeprefix('W/') != validators.etag.removeprefix('W/'))
                                else:
                                    mismatch = (updated.last_modified is not None and
                                                parsedate_to_datetime(updated.last_modified) >
                                                parsedate_to_datetime(validators.last_modified))
                                if mismatch:
                                    raise DomainError(503, "PODCAST_FETCH_FAILED", "Not-modified feed validator mismatch")
                                validators = FeedValidators(url, updated.etag or validators.etag,
                                                            updated.last_modified or validators.last_modified)
                            return FeedResponse(None, url, validators)
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if redirect == 5 or not location:
                                raise DomainError(503, "PODCAST_REDIRECT_ERROR", "Invalid/excessive feed redirects")
                            url = urljoin(url, location)
                            continue
                        response.raise_for_status()
                        encoding = response.headers.get("content-encoding", "identity").lower()
                        if encoding not in ("identity", "gzip"):
                            raise DomainError(503, "PODCAST_ENCODING_ERROR", "Unsupported HTTP content encoding")
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
                        body, wire_bytes = bytearray(), 0
                        async with aclosing(response.aiter_raw()) as chunks:
                            async for chunk in chunks:
                                self._remaining(deadline)
                                wire_bytes += len(chunk)
                                if wire_bytes > self.max_bytes:
                                    raise DomainError(503, "PODCAST_FEED_TOO_LARGE", "Feed exceeds response size limit")
                                if decoder is None:
                                    body.extend(chunk)
                                    continue
                                # A small input chunk can inflate to many MiB. Bound the
                                # temporary output separately from the full feed buffer.
                                while chunk:
                                    self._remaining(deadline)
                                    room = min(GZIP_CHUNK_BYTES, self.max_bytes - len(body) + 1)
                                    body.extend(decoder.decompress(chunk, room))
                                    if len(body) > self.max_bytes:
                                        raise DomainError(503, "PODCAST_FEED_TOO_LARGE", "Feed exceeds response size limit")
                                    if decoder.unused_data:
                                        raise DomainError(503, "PODCAST_ENCODING_ERROR", "Invalid compressed feed")
                                    chunk = decoder.unconsumed_tail
                        self._remaining(deadline)
                        if decoder and not decoder.eof:
                            raise DomainError(503, "PODCAST_ENCODING_ERROR", "Invalid compressed feed")
                        return FeedResponse(bytes(body), url, _response_validators(response.headers, url))


def probe_podcast(config):
    """Independent catalog only: no shared scheduler, history, PCM or hardware."""
    from datetime import datetime, timezone
    from .podcast import EpisodeCatalog, eligible_episodes
    from .podcast_parser import shared_parser
    from ..models.configuration import SourceTestResult, PodcastProbe, PodcastFeedProbe

    now = datetime.now(timezone.utc)
    snapshot = EpisodeCatalog(shared_parser()).refresh(config, FeedFetcher(), now)
    eligible = eligible_episodes(snapshot, config, now)
    # Per-feed counts are a grouping of the core's eligibility result, not a second filter.
    per_feed = Counter(e.feed_id for e in eligible)
    diagnostics = PodcastProbe(
        feeds_attempted=len(snapshot.results),
        feeds_succeeded=sum(r.error is None for r in snapshot.results),
        discovered_episodes=sum(len(f.episodes) for f in snapshot.feeds),
        eligible_episodes=len(eligible),
        feeds=[PodcastFeedProbe(feed_id=r.feed_id, title=r.title, error=r.error,
                              discovered_episodes=r.discovered,
                              eligible_episodes=None if r.error else per_feed[r.feed_id])
               for r in snapshot.results])
    return SourceTestResult(ok=bool(eligible), mocked=False, podcast=diagnostics,
        message=("Podcast catalog available; audio playback was not tested" if eligible
                 else "NO_PLAYABLE_EPISODES: no eligible podcast enclosures"))
