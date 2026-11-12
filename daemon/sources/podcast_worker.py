"""PODCAST snapshot transfer and one-way played notifications; no scheduler copy."""
from collections import deque
from dataclasses import asdict
from datetime import datetime
import json
from threading import Lock
from types import MappingProxyType

from .audio_ipc import ProtocolError
from .podcast import (CatalogSnapshot, PodcastEpisode, PodcastFeed, FeedRefreshResult,
                      feed_identity, valid_http_url)
from .podcast_history import EpisodeHistory
from .podcast_service import PodcastFeedSnapshot

MAX_BYTES = 8 * 1024 * 1024
MAX_EPISODES = 10000
CHUNK = 24000  # ASCII JSON text; outer escaping stays below 64 KiB.


def stamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError('Invalid podcast date')
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ProtocolError('Naive podcast date')
    return result


def encode(snapshot):
    data = dict(catalog=asdict(snapshot.catalog), history=[
        [*key, asdict(value)] for key, value in snapshot.history.items()])
    value = json.dumps(data, default=lambda d: d.isoformat(), ensure_ascii=True,
                       separators=(',', ':'), allow_nan=False)
    if len(value) > MAX_BYTES:
        raise ProtocolError('Podcast catalog exceeds 8 MiB')
    return value


def decode(value, revision, config):
    try:
        data = json.loads(value)
        if set(data) != {'catalog', 'history'}:
            raise ValueError()
        catalog = data['catalog']
        if set(catalog) != {'feeds', 'results', 'refreshed_at'}:
            raise ValueError()
        allowed = {feed_identity(f.url): f.url for f in config.feeds}
        feeds, seen, count = [], set(), 0
        for f in catalog['feeds']:
            if (set(f) != {'feed_id', 'url', 'title', 'episodes'} or
                    f['feed_id'] not in allowed or allowed[f['feed_id']] != f['url'] or f['feed_id'] in seen):
                raise ValueError()
            seen.add(f['feed_id'])
            episodes, keys = [], set()
            for raw in f['episodes']:
                if len(json.dumps(raw, ensure_ascii=True)) > 48000:
                    raise ValueError()
                e = dict(raw)
                for name in ('published_at', 'discovered_at', 'last_seen_at'):
                    e[name] = stamp(e[name])
                episode = PodcastEpisode(**e)
                if (episode.feed_id != f['feed_id'] or episode.feed_url != f['url'] or
                        episode.key in keys or not episode.episode_id or
                        episode.discovered_at is None or episode.last_seen_at is None):
                    raise ValueError()
                valid_http_url(episode.media_url)
                for field in ('feed_title', 'title', 'episode_id', 'media_url'):
                    v = getattr(episode, field)
                    limit = 500 if field in ('title', 'feed_title') else 8192
                    if not isinstance(v, str) or len(v) > limit:
                        raise ValueError()
                keys.add(episode.key)
                episodes.append(episode)
                count += 1
                if count > MAX_EPISODES:
                    raise ValueError()
            if not isinstance(f['title'], str) or len(f['title']) > 500:
                raise ValueError()
            feeds.append(PodcastFeed(f['feed_id'], f['url'], f['title'], tuple(episodes)))
        results = tuple(FeedRefreshResult(**r) for r in catalog['results'])
        if (len(results) > len(allowed) or len({r.feed_id for r in results}) != len(results)
                or any(r.feed_id not in allowed or type(r.discovered) is not int or
                       r.discovered < 0 or (r.error is not None and
                       (not isinstance(r.error, str) or len(r.error) > 500)) or
                       (r.title is not None and (not isinstance(r.title, str) or len(r.title) > 500))
                       for r in results)):
            raise ValueError()
        history = {}
        if len(data['history']) > MAX_EPISODES:
            raise ValueError()
        for fid, eid, h in data['history']:
            if (not isinstance(fid, str) or not isinstance(eid, str) or len(eid) > 8192
                    or (fid, eid) in history or set(h) != {'play_count', 'last_played_at', 'last_seen_at'}
                    or type(h['play_count']) is not int or not 0 <= h['play_count'] < 2**63):
                raise ValueError()
            history[fid, eid] = EpisodeHistory(h['play_count'], stamp(h['last_played_at']), stamp(h['last_seen_at']))
        return PodcastFeedSnapshot(revision, CatalogSnapshot(tuple(feeds), results,
            stamp(catalog['refreshed_at'])), MappingProxyType(history))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise ProtocolError('Invalid podcast snapshot') from exc


class WorkerPodcast:
    def __init__(self, config):
        self.config = config
        self.lock = Lock()
        self.runtime = None
        self.snapshot = None
        self.pending_revision = 0
        self.parts = []
        self.size = 0
        self.outgoing = deque()

    def playback(self, config, wakeup, **kwargs):
        from .podcast_runtime import PodcastRuntime
        runtime = PodcastRuntime(config, wakeup, feed_service=self)
        with self.lock:
            self.runtime = runtime
            snapshot = self.snapshot
        if snapshot is not None:
            runtime.apply_snapshot(snapshot)
        return runtime

    def start(self):
        pass

    def close(self):
        pass

    def played(self, episode, when):
        with self.lock:
            if len(self.outgoing) >= 64:
                return False
            self.outgoing.append(dict(episode=asdict(episode), when=when.isoformat()))
            for key in ('published_at', 'discovered_at', 'last_seen_at'):
                value = self.outgoing[-1]['episode'][key]
                self.outgoing[-1]['episode'][key] = value.isoformat() if value else None
        return True

    def poll(self):
        with self.lock:
            return self.outgoing.popleft() if self.outgoing else None

    def accept(self, data):
        if (set(data) != {'revision', 'offset', 'text', 'final'} or
                type(data['revision']) is not int or data['revision'] <= 0 or
                type(data['offset']) is not int or type(data['final']) is not bool or
                not isinstance(data['text'], str) or not data['text'].isascii() or
                not 1 <= len(data['text']) <= CHUNK):
            raise ProtocolError('Invalid podcast chunk')
        revision = data['revision']
        if self.snapshot is not None and revision <= self.snapshot.revision:
            return
        if not self.parts:
            self.pending_revision = revision
        if revision != self.pending_revision or data['offset'] != self.size:
            raise ProtocolError('Interrupted podcast snapshot')
        self.size += len(data['text'])
        if self.size > MAX_BYTES:
            raise ProtocolError('Podcast snapshot too large')
        self.parts.append(data['text'])
        if data['final']:
            snapshot = decode(''.join(self.parts), revision, self.config)
            with self.lock:
                self.snapshot = snapshot
                runtime = self.runtime
            self.parts, self.size = [], 0
            if runtime is not None:
                runtime.apply_snapshot(snapshot)


class ParentPodcast:
    """Generation-local transfer cursor; service lifetime belongs to its parent adapter."""
    def __init__(self, service):
        self.service = service
        self.revision = service.snapshot().revision
        service.request_snapshot()
        self.transfer = None

    def poll(self):
        if self.service.error:
            raise ProtocolError(self.service.error)
        if self.transfer is None:
            snapshot = self.service.snapshot()
            if snapshot.revision <= self.revision:
                return None
            value = encode(snapshot)
            decode(value, snapshot.revision, self.service.config)
            self.revision = snapshot.revision
            self.transfer = iter(dict(revision=self.revision, offset=i, text=value[i:i+CHUNK],
                final=i+CHUNK >= len(value)) for i in range(0, len(value), CHUNK))
        result = next(self.transfer, None)
        if result is None:
            self.transfer = None
        return result

    def accept(self, data):
        # A rendering episode can legitimately have left the current RSS catalog.
        if set(data) != {'episode', 'when'}:
            raise ProtocolError('Invalid played notification')
        raw = dict(data['episode'])
        fid = raw.get('feed_id')
        url = next((f.url for f in self.service.config.feeds if feed_identity(f.url) == fid), None)
        shell = dict(catalog=dict(feeds=[dict(feed_id=fid, url=url, title=raw.get('feed_title'),
            episodes=[raw])], results=[], refreshed_at=None), history=[])
        snapshot = decode(json.dumps(shell), 1, self.service.config)
        when = stamp(data['when'])
        if when is None or not self.service.played(snapshot.catalog.feeds[0].episodes[0], when):
            raise ProtocolError('Podcast history backlog')

    def close(self):
        self.transfer = None
