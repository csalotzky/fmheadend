"""LOCAL resolution/cycle policy for the shared sequenced media player."""
from pathlib import Path
import logging

from ..errors import DomainError
from .local_media import PlaybackQueue, resolve_media
from .sequenced_playback import Cycle, Ticket, SequencedPlayback, SequencedPlaybackError

LocalPlaybackError = SequencedPlaybackError
log = logging.getLogger(__name__)


class LocalPlayback(SequencedPlayback):
    def __init__(self, config, wakeup, *, resolver=resolve_media, rng=None):
        super().__init__(wakeup)
        self.config, self.resolver, self.rng = config, resolver, rng
        self.queue = None
        self.cycle = None

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

    def close(self):
        super().close()
        self.queue = self.cycle = None
