"""Parent-owned feed/history I/O, independent of playback and native callbacks.

One I/O thread per source uses the existing shared parser admission policy.
Only that thread opens/uses/closes SQLite. Notifications are fire-and-forget;
failed writes are not retried because their commit outcome may be ambiguous.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Mapping
import time

from ..database import Database
from ..errors import DependencyUnavailable, DomainError
from .podcast import CatalogSnapshot, EpisodeCatalog
from .podcast_fetch import FeedFetcher
from .podcast_history import EpisodeHistory, PodcastHistory
from .podcast_parser import shared_parser

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PodcastFeedSnapshot:
    revision: int = 0
    catalog: CatalogSnapshot = field(default_factory=CatalogSnapshot)
    history: Mapping[tuple[str, str], EpisodeHistory] = field(
        default_factory=lambda: MappingProxyType({}))


class PodcastFeedService:
    """Lifecycle calls are serialized by the source owner, as for PodcastRuntime.

    snapshot()/played() are safe from other threads. Callbacks run on the I/O
    owner, outside its lock, and must finish promptly. Revisions are local to
    this service lifetime; a future IPC adapter must also validate generation.
    history_factory is an injection hook: it runs on the I/O owner and supplies
    a repository owned/closed by the caller; production uses database_path.
    """
    def __init__(self, config, *, database_path=None, source_id=None,
                 history_factory=None, fetch_factory=FeedFetcher, clock=time.monotonic,
                 now=lambda: datetime.now(timezone.utc), parser=None,
                 on_snapshot=lambda snapshot: None, on_error=lambda error: None):
        self.config = config
        self.database_path, self.source_id = database_path, source_id
        self.history_factory = history_factory
        self.fetch_factory, self.clock, self.now = fetch_factory, clock, now
        self._parser = parser if parser is not None else shared_parser()
        self.catalog = EpisodeCatalog(self._parser)
        self.on_snapshot, self.on_error = on_snapshot, on_error
        self.lock = Lock()
        self.closed = False
        self.error = None
        self._snapshot = PodcastFeedSnapshot()
        self._marks = deque()
        self._io_ready, self._io_stop = Event(), Event()
        self._io_thread = None
        self._next_refresh = float('-inf')
        self._snapshot_requested = Event()

    def request_snapshot(self):
        """Refresh durable history on the I/O owner without refetching RSS."""
        with self.lock:
            self._snapshot_requested.set()
            self._io_ready.set()

    def snapshot(self):
        """Last complete catalog and durable history, detached from SQLite."""
        with self.lock:
            return self._snapshot

    def played(self, episode, when):
        """Queue one notification; False means closed/backlog, not a failed commit."""
        with self.lock:
            if self.closed or len(self._marks) >= 64:
                return False
            self._marks.append((episode, when))
            self._io_ready.set()
            return True

    def start(self):
        # Process launch and terminal cleanup have different parent threads.
        # Publish/start the I/O owner atomically with respect to close intent.
        with self.lock:
            if self.closed:
                raise DependencyUnavailable('Podcast feed service is closed')
            if self.history_factory is None and not self.database_path:
                raise DependencyUnavailable('Podcast playback requires a persisted source database')
            if self._io_thread is None:
                self._io_thread = Thread(target=self._run_io, name=f'podcast-{self.source_id}', daemon=True)
                try:
                    self._io_thread.start()
                except RuntimeError:
                    self._io_thread = None
                    raise

    def _run_io(self):
        database = None
        try:
            if self.history_factory is not None:
                history = self.history_factory()
            else:
                database = Database(self.database_path)
                history = PodcastHistory(database, self.source_id)
            while not self._io_stop.is_set():
                self._io_ready.clear()
                self.io_step(history)
                self._io_ready.wait(max(0, self._next_refresh - self.clock()))
            self._flush_marks(history)
        except Exception:
            # Exceptions may embed feed/enclosure credentials.
            with self.lock:
                self.error = 'PODCAST_HISTORY_IO_ERROR'
            log.error('Podcast history/refresh worker failed for source %s', self.source_id)
            self.on_error(self.error)
        finally:
            if database is not None:
                database.close()

    def _flush_marks(self, history):
        while True:
            with self.lock:
                if not self._marks:
                    return
                episode, when = self._marks.popleft()
            history.mark_played(episode, when)

    def io_step(self, history):
        """One I/O-owner turn; must not run concurrently with start()/another turn."""
        self._flush_marks(history)
        if self._io_stop.is_set():
            return
        with self.lock:
            requested = self._snapshot_requested.is_set()
            self._snapshot_requested.clear()
        if self.clock() < self._next_refresh:
            if requested:
                self._publish(self.catalog.snapshot(), history)
            return
        fetch = self.fetch_factory()

        def cancellable(url):
            if self._io_stop.is_set():
                raise DomainError(503, 'PODCAST_REFRESH_CANCELLED', 'Source stopped')
            return fetch(url)

        view = self.catalog.refresh(self.config, cancellable, self.now())
        self._publish(view, history)
        self._next_refresh = self.clock() + self.config.refresh_interval_minutes * 60

    def _publish(self, view, history):
        state = dict(history.snapshot())
        with self.lock:
            if self.closed:
                return
            self._snapshot = PodcastFeedSnapshot(self._snapshot.revision + 1, view,
                                                  MappingProxyType(state))
            snapshot = self._snapshot
        # Playback callbacks must never execute while holding the service lock.
        self.on_snapshot(snapshot)

    def close(self):
        with self.lock:
            self.closed = True
        self._io_stop.set()
        self._io_ready.set()
        if self._io_thread is not None:
            self._io_thread.join(timeout=30)
            if self._io_thread.is_alive():
                raise DependencyUnavailable('Podcast refresh did not stop within its timeout')
