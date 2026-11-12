"""Source-owned Podcast policy over the common sequenced player.

The source worker selects items. Serialized sink callbacks associate/count PCM.
PodcastFeedService owns feed/history I/O and publishes detached snapshots.
Playback owns scheduling and optimistic history; native callbacks never do I/O.
"""
from collections import deque
from datetime import datetime, timezone
import time

from ..errors import DependencyUnavailable
from .output import PCM_BYTES_PER_SECOND
from .podcast import CatalogSnapshot, eligible_episodes
from .podcast_fetch import FeedFetcher
from .podcast_service import PodcastFeedService
from .podcast_history import EpisodeHistory
from .podcast_schedule import PodcastScheduler
from .sequenced_playback import SequencedPlayback, Ticket

PLAYED_BYTES = 60 * PCM_BYTES_PER_SECOND


class PodcastRuntime(SequencedPlayback):
    def __init__(self, config, wakeup, *, database_path=None, source_id=None,
                 history_factory=None, fetch_factory=FeedFetcher, clock=time.monotonic,
                 now=lambda: datetime.now(timezone.utc), rng=None, parser=None, feed_service=None):
        super().__init__(wakeup)
        self.config = config
        self.now = now
        self.catalog_view = CatalogSnapshot()
        self.eligible_keys = frozenset()
        self.scheduler = PodcastScheduler(config.schedule_mode, rng=rng)
        self.revision = 0
        self.history_state = {}
        self.failed = set()
        self.active = None
        self.error = 'PODCAST_REFRESH_PENDING'
        self.history_error = None
        self._sources = deque(maxlen=32)
        self._feed_revision = 0
        self.feed_service = feed_service if feed_service is not None else PodcastFeedService(config, database_path=database_path,
            source_id=source_id, history_factory=history_factory, fetch_factory=fetch_factory,
            clock=clock, now=now, parser=parser, on_snapshot=self.apply_snapshot,
            on_error=self._io_error)

    def start(self):
        self.feed_service.start()

    def _io_error(self, error):
        with self.lock:
            if not self.closed:
                self.history_error = error
        self.wakeup.set()

    def apply_snapshot(self, snapshot):
        """Merge durable state without undoing newer optimistic PCM accounting."""
        keys = frozenset(e.key for e in
            eligible_episodes(snapshot.catalog, self.config, self.now()))
        with self.lock:
            if self.closed or snapshot.revision <= self._feed_revision:
                return
            self._feed_revision = snapshot.revision
            self.catalog_view = snapshot.catalog
            self.eligible_keys = keys
            for key, stored in snapshot.history.items():
                old = self.history_state.get(key, EpisodeHistory())
                def latest(a, b):
                    return max(a, b) if a is not None and b is not None else a or b
                self.history_state[key] = EpisodeHistory(
                    max(old.play_count, stored.play_count),
                    latest(old.last_played_at, stored.last_played_at),
                    latest(old.last_seen_at, stored.last_seen_at))
            self.revision += 1
            succeeded = {r.feed_id for r in snapshot.catalog.results if r.error is None}
            self.failed = {key for key in self.failed if key[0] not in succeeded}
        self.wakeup.set()

    def _candidate(self):
        with self.lock:
            if self.closed:
                return None
            if self.history_error:
                raise DependencyUnavailable(self.history_error)
            now = self.now()
            episodes = eligible_episodes(self.catalog_view, self.config, now)
            available = [e for e in episodes if e.key not in self.failed]
            if not available:
                self.error = 'PLAYBACK_EXHAUSTED' if episodes else 'NO_PLAYABLE_EPISODES'
                return None
            episode = self.scheduler.choose_next(available, self.history_state, now,
                replay_cooldown_hours=self.config.replay_cooldown_hours)
            item = episode.media_item()
            self.error = None
            return Ticket(item.locator, context=episode, revision=self.revision)

    def _unavailable(self):
        return None  # Keep the shared pipeline/refresh owner alive while idle.

    def _valid_prepared(self, ticket):
        # Called under the shared ticket lock, including streaming handoff. No
        # network/database work or catalog traversal occurs in that callback.
        e = ticket.context
        if self.config.schedule_mode == 'SMART' and ticket.revision != self.revision:
            return False
        if e.key not in self.eligible_keys or e.key in self.failed:
            return False
        rule = next(f for f in self.config.feeds if f.url == e.feed_url)
        return rule.max_age_days is None or (e.published_at is not None and
            (self.now() - e.published_at).total_seconds() <= rule.max_age_days * 86400)

    def identity_metadata(self):
        with self.lock:
            episode = self.rendering.context if self.rendering else None
            return dict(title=episode.title or None, artist=episode.feed_title or None) if episode else {}

    def bind_source(self, element):
        """Remember bounded native source/occurrence associations for bus errors."""
        with self.lock:
            ticket = self.submitted[-1] if self.submitted else self.rendering
            if not self.closed and ticket is not None:
                self._sources.append((element, ticket))

    def stale_error(self, element):
        with self.lock:
            for source, ticket in reversed(self._sources):
                if source == element:
                    return ticket is not self.rendering and all(ticket is not t for t in self.submitted)
        # Some decoder errors carry no per-item source identity. They require
        # conservative teardown; never guess that an unassociated error is stale.
        return False

    def pcm(self, size=0):
        if size <= 0 or size % 4:
            return
        with self.lock:
            if self.closed or self.rendering is None:
                return
            ticket = self.rendering
            self.active = ticket
            ticket.pcm_bytes += size
            if ticket.pcm_bytes >= PLAYED_BYTES and not ticket.marked:
                episode, when = ticket.context, self.now()
                if not self.feed_service.played(episode, when):
                    self.history_error = 'PODCAST_HISTORY_BACKLOG'
                    self.wakeup.set()
                    return
                ticket.marked = True
                old = self.history_state.get(episode.key, EpisodeHistory())
                self.history_state[episode.key] = EpisodeHistory(old.play_count + 1, when, old.last_seen_at)
                self.revision += 1
                self.wakeup.set()

    def resume(self, *, failed):
        # Native NULL has quiesced callbacks. Error messages do not reliably name
        # the rendering vs prefetched stream; suppress every interrupted submitted
        # occurrence, not the still-unsubmitted prepared item, then skip safely.
        with self.lock:
            if failed:
                for ticket in [self.rendering, *self.submitted]:
                    if ticket is not None:
                        self.failed.add(ticket.context.key)
                if self.prepared and self.prepared.context.key in self.failed:
                    self.prepared = None
            self.active = None
        return super().resume(failed=failed)

    def service(self, player):
        if self.history_error:
            raise DependencyUnavailable(self.history_error)
        super().service(player)

    def close(self):
        super().close()
        self.feed_service.close()
        with self.lock:
            self.active = None
            self.failed.clear()
            self.history_state.clear()
            self.catalog_view = CatalogSnapshot()
            self.eligible_keys = frozenset()
            self._sources.clear()
