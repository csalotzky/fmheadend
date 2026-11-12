"""LOCAL-only catalog transfer and global CPU leases, serviced by IPC threads.

No playlist algorithm lives here. LocalPlayback owns requests, revisions and
safe cycle boundaries. Parent scans use the existing global LocalJobs pools.
Each generation permits one scan and one cycle; completed catalogs are atomic.
"""
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread
import json

from ..errors import DomainError
from ..models.configuration import LocalConfig
from .audio_ipc import ProtocolError
from .local_jobs import Job, scan_jobs, cycle_jobs
from .local_media import MediaItem, resolve_media
from .local_playback import LocalPlayback

MAX_ITEMS = 100000
MAX_CATALOG_BYTES = 16 * 1024 * 1024
CHUNK_ITEMS = 64
MAX_LOCATOR = 8192


def item_wire(item):
    value = str(item.locator)
    if not value or len(value) > MAX_LOCATOR:
        raise DomainError(422, 'LOCAL_CATALOG_LIMIT', 'Catalog locator exceeds 8192 characters')
    result = ['path' if isinstance(item.locator, Path) else 'url', value]
    if len(json.dumps(result)) > 56000:
        raise DomainError(422, 'LOCAL_CATALOG_LIMIT', 'Encoded catalog locator exceeds frame budget')
    return result


def item_read(value):
    if (not isinstance(value, list) or len(value) != 2 or value[0] not in ('path', 'url')
            or not isinstance(value[1], str) or not 1 <= len(value[1]) <= MAX_LOCATOR):
        raise ProtocolError('Invalid LOCAL catalog item')
    return MediaItem(Path(value[1]) if value[0] == 'path' else value[1])


class ParentCatalog:
    """Owned exclusively by the adapter IO thread; close only after child reap."""
    def __init__(self, source):
        self.config = LocalConfig.model_validate(source.config).to_core()
        self.owner = getattr(source, 'id', None) or object()  # Same key, including retired scans.
        self.scan = self.lease = None
        self.scan_request = self.cycle_request = 0
        self.last_request = 0
        self.transfer = None
        self.wakeup = Event()

    def accept(self, data):
        if set(data) != {'op', 'request'} or type(data['request']) is not int or data['request'] <= 0:
            raise ProtocolError('Invalid LOCAL request')
        op, request = data['op'], data['request']
        if op == 'cycle_done':
            if request != self.cycle_request or self.lease is None:
                raise ProtocolError('Invalid LOCAL lease release')
            cycle_jobs.release(self.owner, self.lease)
            self.lease = None
            self.cycle_request = 0
            return
        if request <= self.last_request:
            raise ProtocolError('Repeated LOCAL job request')
        self.last_request = request
        if op == 'scan' and not self.scan_request:
            self.scan_request = request
        elif op == 'cycle' and not self.cycle_request:
            self.cycle_request = request
        else:
            raise ProtocolError('Overlapping LOCAL job')

    def _resolve(self):
        items = resolve_media(self.config)
        if not items:
            raise DomainError(409, 'NO_PLAYABLE_TRACKS', 'Empty catalog')
        if len(items) > MAX_ITEMS:
            raise DomainError(422, 'LOCAL_CATALOG_LIMIT', 'Too many catalog entries')
        wire = [item_wire(item) for item in items]
        if sum(len(value.encode('utf8')) for _, value in wire) > MAX_CATALOG_BYTES:
            raise DomainError(422, 'LOCAL_CATALOG_LIMIT', 'Catalog exceeds 16 MiB')
        return wire

    def poll(self):
        # At most one bounded command per control iteration, PCM has its own IO.
        if self.cycle_request and self.lease is None:
            self.lease = cycle_jobs.reserve(self.owner)
            if self.lease is not None:
                return dict(op='cycle', request=self.cycle_request)
        if self.transfer is not None:
            try:
                return next(self.transfer)
            except StopIteration:
                self.transfer = None
        if self.scan_request and self.scan is None:
            self.scan = scan_jobs.submit(self.owner, self._resolve, self.wakeup)
        if self.scan is not None and self.scan.done.is_set():
            job, self.scan = self.scan, None
            request = self.scan_request
            if job.error is not None:
                self.scan_request = 0
                if not isinstance(job.error, DomainError):
                    raise job.error
                return dict(op='error', request=request, error=job.error.error)
            items = job.value

            def chunks():
                offset = 0
                while offset < len(items):
                    chunk, size = [], 0
                    for item in items[offset:offset + CHUNK_ITEMS]:
                        encoded = len(json.dumps(item, separators=(',', ':')))
                        if chunk and size + encoded > 56000:
                            break
                        chunk.append(item)
                        size += encoded + 1
                    final = offset + len(chunk) == len(items)
                    if final:
                        self.scan_request = 0
                    yield dict(op='catalog', request=request, offset=offset,
                               items=chunk, final=final)
                    offset += len(chunk)
            self.transfer = chunks()
            return next(self.transfer)
        return None

    def close(self):
        if self.scan is not None:
            self.scan.cancel()  # Pool retains its key until the filesystem returns.
        if self.lease is not None:
            cycle_jobs.release(self.owner, self.lease)  # Child has actually reaped.
        self.scan = self.lease = self.transfer = None


class RemoteJobs:
    def __init__(self, bridge, kind):
        self.bridge, self.kind = bridge, kind

    def submit(self, owner, operation, wakeup):
        return self.bridge.submit(self.kind, operation, wakeup)


class WorkerCatalog:
    """Native worker submits locally; only audiod control loop reads/writes IPC."""
    def __init__(self):
        self.lock = Lock()
        self.sequence = 0
        self.jobs = {}
        self.outgoing = deque()
        self.items = []
        self.size = 0

    def playback(self, config, wakeup, **kwargs):
        return LocalPlayback(config, wakeup, **kwargs,
            jobs=(RemoteJobs(self, 'scan'), RemoteJobs(self, 'cycle')), retry_unavailable=True)

    def submit(self, kind, operation, wakeup):
        with self.lock:
            if kind in self.jobs:
                return None
            self.sequence += 1
            job = Job()
            self.jobs[kind] = (self.sequence, job, operation, wakeup)
            self.outgoing.append(dict(op=kind, request=self.sequence))
            return job

    def poll(self):
        with self.lock:
            return self.outgoing.popleft() if self.outgoing else None

    def accept(self, data):
        op, request = data.get('op'), data.get('request')
        kind = 'cycle' if op == 'cycle' else 'scan'
        with self.lock:
            entry = self.jobs.get(kind)
        if entry is None or type(request) is not int or request != entry[0]:
            raise ProtocolError('Stale LOCAL catalog/lease')
        _, job, operation, wakeup = entry
        if op == 'cycle' and set(data) == {'op', 'request'}:
            if getattr(job, 'started', False):
                raise ProtocolError('Repeated LOCAL cycle grant')
            job.started = True
            # The parent lease bounds CPU work across all source processes.
            def build():
                try:
                    value, error = operation(), None
                except Exception as exc:
                    value, error = None, exc
                with self.lock:
                    self.outgoing.append(dict(op='cycle_done', request=request))
                    del self.jobs[kind]
                self._complete(job, wakeup, value, error)
            Thread(target=build, name='local-cycle', daemon=True).start()
        elif op == 'error' and set(data) == {'op', 'request', 'error'} and isinstance(data['error'], str):
            self.items, self.size = [], 0
            with self.lock:
                del self.jobs[kind]
            self._complete(job, wakeup, None, DomainError(503, data['error'], data['error']))
        elif (op == 'catalog' and set(data) == {'op', 'request', 'offset', 'items', 'final'}
              and type(data['offset']) is int and data['offset'] == len(self.items)
              and type(data['final']) is bool and isinstance(data['items'], list)
              and 1 <= len(data['items']) <= CHUNK_ITEMS):
            items = [item_read(value) for value in data['items']]
            self.size += sum(len(value[1].encode('utf8')) for value in data['items'])
            if self.size > MAX_CATALOG_BYTES or len(self.items) + len(items) > MAX_ITEMS:
                raise ProtocolError('LOCAL catalog exceeds bounds')
            self.items.extend(items)
            if data['final']:
                value, self.items, self.size = tuple(self.items), [], 0
                with self.lock:
                    del self.jobs[kind]
                self._complete(job, wakeup, value, None)
        else:
            raise ProtocolError('Invalid LOCAL response')

    @staticmethod
    def _complete(job, wakeup, value, error):
        with job.lock:
            if not job.cancelled:
                job.value, job.error = value, error
            job.done.set()
        wakeup.set()
