"""Podcast feed snapshots and eligibility. No player, timers or persistent queue."""
import calendar
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import urljoin, urlsplit

import feedparser

from ..errors import DomainError
from .local_media import MediaItem

logger = logging.getLogger(__name__)


class PodcastScheduleMode(StrEnum):
    RANDOM = "RANDOM"
    SMART = "SMART"


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Podcast timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def valid_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    _ = parsed.port  # Reject malformed/out-of-range ports too.
    return value


def feed_identity(url: str) -> str:
    # Configured URL, not redirect target: redirects must not reset history.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def display_title(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())[:500]


@dataclass(frozen=True)
class PodcastEpisode:
    feed_id: str
    feed_url: str
    feed_title: str
    episode_id: str
    title: str
    media_url: str
    published_at: datetime | None
    discovered_at: datetime
    last_seen_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        return self.feed_id, self.episode_id

    def media_item(self) -> MediaItem:
        return MediaItem(self.media_url)


@dataclass(frozen=True)
class PodcastFeed:
    feed_id: str
    url: str
    title: str
    episodes: tuple[PodcastEpisode, ...]


def parse_feed(data: bytes, url: str, now: datetime, *, base_url: str | None = None) -> PodcastFeed:
    """feedparser handles RSS/Atom, encodings and dates; it never fetches URLs here."""
    now = utc(now)
    parsed = feedparser.parse(data, response_headers={"content-location": base_url or url})
    # Tolerate parser warnings on usable feeds, but do not replace a good catalog
    # with a truncated/ill-formed XML document or a non-feed HTTP response.
    from xml.sax import SAXException
    if not parsed.version or isinstance(parsed.get("bozo_exception"), SAXException):
        raise DomainError(503, "PODCAST_FEED_INVALID", "Response is not a usable RSS/Atom feed")
    fid = feed_identity(url)
    title = display_title(parsed.feed.get("title", ""))
    episodes = {}
    for entry in parsed.entries:
        candidates = []
        for link in entry.get("links", []):
            if link.get("rel") != "enclosure":
                continue
            mime = link.get("type", "").split(";", 1)[0].lower()
            # Audio first, then an untyped/octet-stream enclosure. Never select
            # a known video/image link just because it has an audio-like suffix.
            rank = 0 if mime.startswith("audio/") else 1
            if rank and mime not in ("", "application/octet-stream"):
                continue
            try:
                media = valid_http_url(urljoin(base_url or url, link.get("href", "")))
                if not link.get("href"):
                    continue
            except ValueError:
                continue
            candidates.append((rank, media))
        if not candidates:
            continue
        media = min(candidates)[1]
        native_id = entry.get("id")
        identity = ("id:" + native_id) if native_id else ("url:" + media)
        eid = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        published = None
        # Do not use update time as publication, nor malformed dates as 'now'.
        if entry.get("published_parsed"):
            try:
                published = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        episode = PodcastEpisode(fid, url, title, eid, display_title(entry.get("title", "")),
                                 media, published, now, now)
        # XML ordering does not decide between duplicate IDs with different data.
        previous = episodes.get(eid)
        if previous is None or (newest_key(episode), media, episode.title) < (
                newest_key(previous), previous.media_url, previous.title):
            episodes[eid] = episode
    return PodcastFeed(fid, url, title, tuple(sorted(episodes.values(), key=newest_key)))


def newest_key(episode: PodcastEpisode):
    return (episode.published_at is None,
            -episode.published_at.timestamp() if episode.published_at else 0, episode.key)


@dataclass(frozen=True)
class FeedRefreshResult:
    feed_id: str
    error: str | None
    discovered: int
    title: str | None = None


@dataclass(frozen=True)
class CatalogSnapshot:
    feeds: tuple[PodcastFeed, ...] = ()
    results: tuple[FeedRefreshResult, ...] = ()
    refreshed_at: datetime | None = None


class EpisodeCatalog:
    """One per source. Readers see immutable snapshots, never partial refreshes.

    Refresh calls serialize; readers only hold a short publication lock. The
    owner calls refresh when due; there is no new daemon worker or timer.
    """
    def __init__(self):
        self._snapshot = CatalogSnapshot()
        self._lock = Lock()
        self._refresh_lock = Lock()

    def snapshot(self) -> CatalogSnapshot:
        with self._lock:
            return self._snapshot

    def refresh_due(self, config, now: datetime) -> bool:
        previous = self.snapshot().refreshed_at
        return previous is None or (utc(now) - previous).total_seconds() >= config.refresh_interval_minutes * 60

    def refresh(self, config, fetch, now: datetime, *, history=None) -> CatalogSnapshot:
        now = utc(now)
        with self._refresh_lock:
            previous = {feed.feed_id: feed for feed in self.snapshot().feeds}
            feeds, results, seen = [], [], []
            for configured in config.feeds:
                fid = feed_identity(configured.url)
                old = previous.get(fid)
                try:
                    response = fetch(configured.url)
                    new = parse_feed(response.body, configured.url, now, base_url=response.url)
                    known = {e.episode_id: e for e in old.episodes} if old else {}
                    new = replace(new, episodes=tuple(replace(e,
                        discovered_at=known[e.episode_id].discovered_at) if e.episode_id in known else e
                        for e in new.episodes))
                    feeds.append(new)
                    seen.extend(new.episodes)
                    results.append(FeedRefreshResult(fid, None, len(new.episodes), new.title or None))
                except DomainError as exc:
                    # Codes and hashes only: feed URLs/errors can contain tokens.
                    logger.warning("Podcast feed %s refresh failed: %s", fid, exc.error)
                    results.append(FeedRefreshResult(fid, exc.error, 0))
                    if old:
                        feeds.append(old)
            if history is not None:
                history.observe(seen, now)
            snapshot = CatalogSnapshot(tuple(feeds), tuple(results), now)
            with self._lock:
                self._snapshot = snapshot
            return snapshot


def eligible_episodes(snapshot: CatalogSnapshot, config, now: datetime) -> tuple[PodcastEpisode, ...]:
    if not config.feeds:
        raise DomainError(409, "NO_PODCAST_FEEDS", "No podcast feeds configured")
    now = utc(now)
    limits = {feed_identity(f.url): f for f in config.feeds}
    result = []
    for feed in snapshot.feeds:
        rule = limits.get(feed.feed_id)
        if rule is None:
            continue
        episodes = [e for e in feed.episodes if rule.max_age_days is None or (
            e.published_at is not None and (now - e.published_at).total_seconds() <= rule.max_age_days * 86400)]
        episodes.sort(key=newest_key)
        result.extend(episodes[:rule.max_episodes] if rule.max_episodes is not None else episodes)
    return tuple(sorted(result, key=newest_key))
