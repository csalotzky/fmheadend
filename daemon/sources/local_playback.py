"""LOCAL resolution/cycle policy for the shared sequenced media player."""
from pathlib import Path
import logging
import random
import time

from ..errors import DomainError
from .local_media import PlaybackQueue, resolve_media
from .local_jobs import scan_jobs, cycle_jobs
from .sequenced_playback import Cycle, Ticket, SequencedPlayback, SequencedPlaybackError

LocalPlaybackError = SequencedPlaybackError
log = logging.getLogger(__name__)


class LocalPlayback(SequencedPlayback):
    def __init__(self, config, wakeup, *, resolver=resolve_media, rng=None,
                 source_id=None, clock=time.monotonic):
        super().__init__(wakeup)
        self.config, self.resolver = config, resolver
        self.rng = rng if rng is not None else random.Random()
        self.owner = source_id if source_id is not None else object()
        self.clock = clock
        self.queue = None
        self.cycle = None
        self.catalog = None
        self.revision = 0
        self.generation = 0  # Cycle identity, separate from this playback instance.
        self.scan = self.build = None
        self.next_queue = None
        self.next_revision = -1
        self.refresh_requested = False
        self.retry_at = 0.0
        self.error = 'LOCAL_DISCOVERY_PENDING'

    def _service_jobs(self):
        """Source worker only: poll snapshots; never wait or enumerate here."""
        if self.closed:
            return
        if self.scan is not None and self.scan.done.is_set():
            job, self.scan = self.scan, None
            if job.error is not None:
                self.error = job.error.error if isinstance(job.error, DomainError) else 'LOCAL_DISCOVERY_ERROR'
                log.warning('source %s LOCAL refresh failed: %s', self.owner, self.error)
                if not isinstance(job.error, DomainError):
                    log.error('LOCAL discovery raised an unexpected %s', type(job.error).__name__)
                self.retry_at = self.clock() + 5.0
                if self.catalog is None:
                    if not isinstance(job.error, DomainError):
                        raise job.error
                    raise LocalPlaybackError(self.error) from job.error
            else:
                self.catalog = job.value
                self.revision += 1
                self.error = None
                log.info('source %s LOCAL catalog resolved %s candidates', self.owner, len(self.catalog))
        if self.build is not None and self.build[0].done.is_set():
            job, generation, revision = self.build
            self.build = None
            if job.error is not None:
                raise job.error
            if generation == self.generation:
                self.next_queue, self.next_revision = job.value, revision
        # Request once per successful cycle, or retry failed discovery no faster
        # than five seconds. Existing media remains playable during either case.
        wanted = self.catalog is None or (self.cycle is not None and self.cycle.played and not self.refresh_requested)
        if self.scan is None and wanted and self.clock() >= self.retry_at:
            resolver, config = self.resolver, self.config

            def resolve():
                items = tuple(resolver(config))
                if not items:
                    raise DomainError(409, 'NO_PLAYABLE_TRACKS', 'No media candidates')
                return items

            self.scan = scan_jobs.submit(self.owner, resolve, self.wakeup)
            if self.scan is None:
                self.error = 'LOCAL_DISCOVERY_BUSY'
                self.retry_at = self.clock() + 5.0
            else:
                self.refresh_requested = True
        if self.catalog is not None and self.build is None and (
                self.next_queue is None or self.next_revision != self.revision):
            items, order, rng = self.catalog, self.config.order, self.rng
            previous = self.queue.last_item if self.queue is not None else None
            job = cycle_jobs.submit(self.owner,
                lambda: PlaybackQueue(items, order, rng=rng, previous=previous), self.wakeup)
            if job is not None:
                self.build = job, self.generation, self.revision

    def service(self, player):
        self._service_jobs()
        super().service(player)

    def _candidate(self):
        self._service_jobs()
        if self.closed:
            return None
        if self.queue is None or self.queue.exhausted:
            if self.cycle is not None and not self.cycle.played:
                return None  # Wait for actual rendered PCM before rebuilding.
            if self.next_queue is None:
                return None  # Pending initial discovery or cycle preparation.
            if self.scan is not None and self.error != 'LOCAL_REFRESH_PENDING':
                self.error = 'LOCAL_REFRESH_PENDING'
                log.warning('LOCAL refresh pending; continuing with the last valid catalog')
            self.queue, self.next_queue = self.next_queue, None
            self.cycle = Cycle()
            self.generation += 1
            self.refresh_requested = False
            self._service_jobs()  # Prebuild a fallback independently of scan I/O.
        item = self.queue.next_item()
        if item is None:
            raise LocalPlaybackError('NO_PLAYABLE_TRACKS')
        # pathlib performs correct URI escaping; configuration remains path-based.
        return Ticket(item.locator.as_uri() if isinstance(item.locator, Path) else item.locator, self.cycle)

    def _unavailable(self):
        if self.cycle is not None and not self.cycle.played:
            return super()._unavailable()
        return None  # The existing idle-player path resumes when a job wakes it.

    def close(self):
        super().close()
        for job in (self.scan, self.build[0] if self.build is not None else None):
            if job is not None:
                job.cancel()  # Invalidate publication, not the OS filesystem call.
        self.scan = self.build = self.next_queue = self.catalog = None
        self.queue = self.cycle = None
