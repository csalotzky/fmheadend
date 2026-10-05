"""Source-owned Podcast policy over the common sequenced player.

The source worker selects items. Serialized sink callbacks associate/count PCM.
One I/O worker owns a separate SQLite connection, refresh and history writes;
it publishes snapshots under the ticket lock, never calls native GStreamer.
"""
from collections import deque
from datetime import datetime, timezone
import logging
from threading import Event, Thread
import time

from ..database import Database
from ..errors import DependencyUnavailable, DomainError
from .output import PCM_BYTES_PER_SECOND
from .podcast import CatalogSnapshot, EpisodeCatalog, eligible_episodes
from .podcast_fetch import FeedFetcher
from .podcast_history import PodcastHistory, EpisodeHistory
from .podcast_schedule import PodcastScheduler
from .sequenced_playback import SequencedPlayback, Ticket

log = logging.getLogger(__name__)
PLAYED_BYTES = 60 * PCM_BYTES_PER_SECOND


class PodcastRuntime(SequencedPlayback):
    def __init__(self, config, wakeup, *, database_path=None, source_id=None,
                 history_factory=None, fetch_factory=FeedFetcher, clock=time.monotonic,
                 now=lambda: datetime.now(timezone.utc), rng=None):
        super().__init__(wakeup)
        self.config = config
        self.database_path, self.source_id = database_path, source_id
        self.history_factory = history_factory
        self.fetch_factory, self.clock, self.now = fetch_factory, clock, now
        self.catalog = EpisodeCatalog()
        self.catalog_view = self.catalog.snapshot()
        self.eligible_keys = frozenset()
        self.scheduler = PodcastScheduler(config.schedule_mode, rng=rng)
        self.revision = 0
        self.history_state = {}
        self.failed = set()
        self.active = None
        self.error = 'PODCAST_REFRESH_PENDING'
        self.history_error = None
        self._marks = deque()
        self._sources = deque(maxlen=32)
        self._io_ready, self._io_stop = Event(), Event()
        self._io_thread = None
        self._next_refresh = float('-inf')

    def start(self):
        if self.history_factory is None and not self.database_path:
            raise DependencyUnavailable('Podcast playback requires a persisted source database')
        if self._io_thread is None:
            self._io_thread = Thread(target=self._run_io, name=f'podcast-{self.source_id}', daemon=True)
            self._io_thread.start()

    def _run_io(self):
        database = None
        try:
            if self.history_factory is not None:
                history = self.history_factory()
            else:
                database = Database(self.database_path)
                history = PodcastHistory(database, self.source_id)
            with self.lock:
                self.history_state = history.snapshot()
            while not self._io_stop.is_set():
                self._io_ready.clear()
                self.io_step(history)
                self._io_ready.wait(max(0, self._next_refresh - self.clock()))
            self._flush_marks(history)
        except Exception:
            # Exceptions can embed enclosure/feed credentials. Report a category.
            with self.lock:
                self.history_error = 'PODCAST_HISTORY_IO_ERROR'
            log.error('Podcast history/refresh worker failed for source %s', self.source_id)
            self.wakeup.set()
        finally:
            if database is not None:
                database.close()

    def _flush_marks(self, history):
        while True:
            with self.lock:
                if not self._marks:
                    return
                episode, when = self._marks.popleft()
            # Once per occurrence, never from a streaming callback. A repository
            # failure is explicit and is not blindly retried (ambiguous commit).
            history.mark_played(episode, when)

    def io_step(self, history):
        """One deterministic I/O turn; tests inject monotonic/UTC clocks."""
        self._flush_marks(history)
        if self._io_stop.is_set() or self.clock() < self._next_refresh:
            return
        fetch = self.fetch_factory()
        def cancellable(url):
            if self._io_stop.is_set():
                raise DomainError(503, 'PODCAST_REFRESH_CANCELLED', 'Source stopped')
            return fetch(url)
        view = self.catalog.refresh(self.config, cancellable, self.now(), history=history)
        keys = frozenset(e.key for e in eligible_episodes(view, self.config, self.now()))
        with self.lock:
            if not self.closed:
                self.catalog_view = view
                self.eligible_keys = keys
                self.revision += 1
                succeeded = {r.feed_id for r in view.results if r.error is None}
                self.failed = {key for key in self.failed if key[0] not in succeeded}
        self._next_refresh = self.clock() + self.config.refresh_interval_minutes * 60
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
                if len(self._marks) >= 64:
                    self.history_error = 'PODCAST_HISTORY_BACKLOG'
                    self.wakeup.set()
                    return
                ticket.marked = True
                episode, when = ticket.context, self.now()
                old = self.history_state.get(episode.key, EpisodeHistory())
                self.history_state[episode.key] = EpisodeHistory(old.play_count + 1, when, old.last_seen_at)
                self.revision += 1
                self._marks.append((episode, when))
                self._io_ready.set()
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
        self._io_stop.set()
        self._io_ready.set()
        if self._io_thread is not None:
            self._io_thread.join(timeout=30)
            if self._io_thread.is_alive():
                raise DependencyUnavailable('Podcast refresh did not stop within its timeout')
        with self.lock:
            self.active = None
            self.failed.clear()
            self.history_state.clear()
            self.catalog_view = CatalogSnapshot()
            self.catalog = EpisodeCatalog()
            self.eligible_keys = frozenset()
            self._sources.clear()
