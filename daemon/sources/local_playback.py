"""Transient LOCAL sequencing owned by the existing source worker.

Only handoff/stream-boundary bookkeeping runs on GStreamer threads. Resolution,
queue construction and error decisions run on the worker. No lock covers IO or GI.
"""
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
import logging

from ..errors import DependencyUnavailable, DomainError
from .local_media import PlaybackQueue, resolve_media

log = logging.getLogger(__name__)


class LocalPlaybackError(DependencyUnavailable):
    """Terminal empty/failed pass. A true source restart may retry."""


@dataclass
class Cycle:
    played: bool = False


@dataclass
class Ticket:
    uri: str
    cycle: Cycle


class LocalPlayback:
    def __init__(self, config, wakeup, *, resolver=resolve_media, rng=None):
        self.config, self.wakeup, self.resolver, self.rng = config, wakeup, resolver, rng
        self.queue = None
        self.cycle = None
        self.lock = Lock()
        self.prepared = None
        self.submitted = deque()
        self.rendering = None
        self.closed = False

    def _resolve(self):
        try:
            items = self.resolver(self.config)
        except DomainError as exc:
            # Do not copy resolver paths or remote credentials into runtime logs.
            raise LocalPlaybackError(exc.error) from exc
        if self.queue is None:
            self.queue = PlaybackQueue(items, self.config.order, rng=self.rng)
        else:
            self.queue.rebuild(items)
        self.cycle = Cycle()
        log.info('LOCAL cycle resolved %s candidates', len(items))

    def _candidate(self):
        if self.queue is None:
            self._resolve()
        elif self.queue.exhausted:
            if not self.cycle.played:
                return None  # Wait for actual rendered PCM before rebuilding.
            self._resolve()
        item = self.queue.next_item()
        if item is None:
            raise LocalPlaybackError('NO_PLAYABLE_TRACKS')
        # pathlib performs correct URI escaping; configuration remains path-based.
        return Ticket(item.locator.as_uri() if isinstance(item.locator, Path) else item.locator, self.cycle)

    def initial(self):
        ticket = self._candidate()
        with self.lock:
            self.submitted.append(ticket)
        self.prepare()
        return ticket.uri

    def prepare(self):
        """Worker only; keep one next URI ready without scanning in callbacks."""
        with self.lock:
            if self.closed or self.prepared is not None:
                return
        ticket = self._candidate()
        with self.lock:
            if not self.closed:
                self.prepared = ticket

    def handoff(self, player):
        """Native about-to-finish callback: bounded bookkeeping and one URI set."""
        with self.lock:
            if self.closed:
                return
            ticket, self.prepared = self.prepared, None
            if ticket is not None:
                self.submitted.append(ticket)
        if ticket is not None:
            player.set_property('uri', ticket.uri)
        self.wakeup.set()

    def service(self, player):
        # Never set URI from the worker while native handoff is running. If the
        # callback found no prepared item, EOS provides the safe fallback.
        self.prepare()

    def stream_start(self):
        # Called at appsink, after all buffers of the previous stream. Associate
        # success with the rendered item, never just the most recently set URI.
        with self.lock:
            self.rendering = self.submitted.popleft() if self.submitted else None

    def pcm(self):
        with self.lock:
            if self.rendering is not None:
                self.rendering.cycle.played = True

    def resume(self, *, failed):
        """Worker after NULL: recover EOS without handoff, or skip a failed item."""
        with self.lock:
            # EOS may have won the race with a late handoff. Don't skip that URI.
            ticket = self.submitted[0] if not failed and self.submitted else self.prepared
            if ticket is self.prepared:
                self.prepared = None
            self.submitted.clear()
            self.rendering = None
        if ticket is None:
            ticket = self._candidate()
        if ticket is None:
            raise LocalPlaybackError('PLAYBACK_EXHAUSTED')
        with self.lock:
            self.submitted.append(ticket)
        self.prepare()
        return ticket.uri

    def close(self):
        with self.lock:
            self.closed = True
            self.prepared = self.rendering = None
            self.submitted.clear()
        self.queue = self.cycle = None
