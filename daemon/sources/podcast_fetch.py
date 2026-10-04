"""Bounded feed-only HTTP transport. No audio downloads or implicit retries."""
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from ..errors import DomainError
from .podcast import valid_http_url


@dataclass(frozen=True)
class FeedResponse:
    body: bytes
    url: str


class FeedFetcher:
    def __init__(self, *, timeout_s=5.0, budget_s=20.0, max_bytes=4 * 1024 * 1024,
                 clock=time.monotonic, transport=None):
        self.timeout_s, self.max_bytes = timeout_s, max_bytes
        self.clock, self.deadline = clock, clock() + budget_s
        self.transport = transport

    def __call__(self, url: str) -> FeedResponse:
        try:
            with httpx.Client(transport=self.transport, follow_redirects=False, trust_env=False,
                              headers={"User-Agent": "FMHeadend/0.1 Podcast catalog",
                                       "Accept-Encoding": "identity"}) as client:
                for redirect in range(6):
                    valid_http_url(url)
                    remaining = self.deadline - self.clock()
                    if remaining <= 0:
                        raise DomainError(503, "PODCAST_FETCH_TIMEOUT", "Feed refresh budget exhausted")
                    with client.stream("GET", url, timeout=min(self.timeout_s, remaining)) as response:
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
                        for chunk in response.iter_raw():
                            if self.clock() >= self.deadline:
                                raise DomainError(503, "PODCAST_FETCH_TIMEOUT", "Feed refresh budget exhausted")
                            wire_bytes += len(chunk)
                            if decoder:
                                chunk = decoder.decompress(chunk, self.max_bytes - len(body) + 1)
                            body.extend(chunk)
                            if len(body) > self.max_bytes or wire_bytes > self.max_bytes:
                                raise DomainError(503, "PODCAST_FEED_TOO_LARGE", "Feed exceeds response size limit")
                        if decoder and (not decoder.eof or decoder.unused_data):
                            raise DomainError(503, "PODCAST_ENCODING_ERROR", "Invalid compressed feed")
                        return FeedResponse(bytes(body), url)
        except httpx.TimeoutException as exc:
            raise DomainError(503, "PODCAST_FETCH_TIMEOUT", "Feed request timed out") from exc
        except (httpx.HTTPError, ValueError, zlib.error) as exc:
            raise DomainError(503, "PODCAST_FETCH_FAILED", "Feed request failed") from exc


def probe_podcast(config):
    """Independent catalog only: no shared scheduler, history, PCM or hardware."""
    from datetime import datetime, timezone
    from .podcast import EpisodeCatalog, eligible_episodes
    from ..models.configuration import SourceTestResult, PodcastProbe, PodcastFeedProbe

    now = datetime.now(timezone.utc)
    snapshot = EpisodeCatalog().refresh(config, FeedFetcher(), now)
    eligible = eligible_episodes(snapshot, config, now)
    diagnostics = PodcastProbe(
        feeds_attempted=len(snapshot.results),
        feeds_succeeded=sum(r.error is None for r in snapshot.results),
        discovered_episodes=sum(len(f.episodes) for f in snapshot.feeds),
        eligible_episodes=len(eligible),
        feeds=[PodcastFeedProbe(feed_id=r.feed_id, title=r.title, error=r.error,
                              discovered_episodes=r.discovered)
               for r in snapshot.results])
    return SourceTestResult(ok=bool(eligible), mocked=False, podcast=diagnostics,
        message=("Podcast catalog available; audio playback was not tested" if eligible
                 else "NO_PLAYABLE_EPISODES: no eligible podcast enclosures"))
