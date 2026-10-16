"""Shared sequenced-URI handoff; policy stays with the source's item provider.

Worker methods select/prepare. Streaming callbacks only move prepared tickets.
The GstPipeline owns the single native player and serializes URI/NULL changes.
"""
from collections import deque
from dataclasses import dataclass
from threading import Lock

from ..errors import SourceUnavailable


class SequencedPlaybackError(SourceUnavailable):
    """No selectable item; source policy determines whether refresh can recover."""


@dataclass
class Cycle:
    played: bool = False


@dataclass
class Ticket:
    uri: str
    cycle: Cycle | None = None
    context: object = None
    pcm_bytes: int = 0
    marked: bool = False
    revision: int = 0


class SequencedPlayback:
    def __init__(self, wakeup):
        self.wakeup = wakeup
        self.lock = Lock()
        self.prepared = None
        self.submitted = deque()
        self.rendering = None
        self.closed = False

    def _candidate(self):
        raise NotImplementedError

    def _valid_prepared(self, ticket):
        return True

    def initial(self):
        ticket = self._candidate()
        if ticket is None:
            return self._unavailable()
        with self.lock:
            self.submitted.append(ticket)
        self.prepare()
        return ticket.uri

    def _unavailable(self):
        raise SequencedPlaybackError('PLAYBACK_EXHAUSTED')

    def prepare(self):
        """Worker only; keep one next URI ready without scanning in callbacks."""
        with self.lock:
            if self.prepared is not None and not self._valid_prepared(self.prepared):
                self.prepared = None
            if self.closed or self.prepared is not None:
                return
        ticket = self._candidate()
        with self.lock:
            if not self.closed:
                self.prepared = ticket if ticket is None or self._valid_prepared(ticket) else None

    def handoff(self, player):
        """Native about-to-finish callback: bounded bookkeeping and one URI set."""
        with self.lock:
            if self.closed:
                return
            ticket, self.prepared = self.prepared, None
            if ticket is not None and not self._valid_prepared(ticket):
                ticket = None
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
            if self.closed:
                return None
            self.rendering = self.submitted.popleft() if self.submitted else None
            return self.rendering.uri if self.rendering else None

    def pcm(self, size=0):
        with self.lock:
            if self.rendering is not None and self.rendering.cycle is not None:
                self.rendering.cycle.played = True

    def resume(self, *, failed):
        """Worker after NULL: recover EOS without handoff, or skip a failed item."""
        with self.lock:
            # EOS may have won the race with a late handoff. Don't skip that URI.
            ticket = self.submitted[0] if not failed and self.submitted else self.prepared
            if ticket is self.prepared:
                self.prepared = None
                if ticket is not None and not self._valid_prepared(ticket):
                    ticket = None
            self.submitted.clear()
            self.rendering = None
        if ticket is None:
            ticket = self._candidate()
        if ticket is None:
            return self._unavailable()
        with self.lock:
            self.submitted.append(ticket)
        self.prepare()
        return ticket.uri

    def close(self):
        with self.lock:
            self.closed = True
            self.prepared = self.rendering = None
            self.submitted.clear()
