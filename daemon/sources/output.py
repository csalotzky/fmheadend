"""Bounded byte transport only. No audio arithmetic and no hardware IO."""
from collections import deque
from threading import Lock
import sys
import time

PCM_CAPS = "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"
PCM_BYTES_PER_SECOND = 48000 * 2 * 2
DEFAULT_STAGING_BUFFER_MS = 200
STAGING_MAX_AGE_S = 0.1
DEFAULT_CONSUMER_BUFFER_MS = 300


def pcm_capacity_bytes(duration_ms: int) -> int:
    """Positive integer milliseconds, bounded by Python's byte-container size."""
    bytes_per_ms = PCM_BYTES_PER_SECOND // 1000
    if type(duration_ms) is not int or not 0 < duration_ms <= sys.maxsize // bytes_per_ms:
        raise ValueError("PCM buffer milliseconds must be a positive integer whose byte capacity fits sys.maxsize")
    return duration_ms * bytes_per_ms


class PcmQueue:
    def __init__(self, max_bytes=19200, max_age_s=0.1, clock=time.monotonic, wakeup=None):
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s
        self.clock = clock
        self._items = deque()
        self._bytes = 0
        self._lock = Lock()
        self.dropped = 0
        self._wakeup = wakeup
        self._counts = dict(capacity_dropped_bytes=0, stale_dropped_bytes=0,
                            invalid_dropped_bytes=0, cleared_bytes=0, accepted_bytes=0)

    def set_wakeup(self, wakeup):
        with self._lock:
            self._wakeup = wakeup

    def snapshot(self):
        with self._lock:
            return dict(self._counts, dropped_blocks=self.dropped, buffered_bytes=self._bytes)

    def put(self, data: bytes) -> bool:
        # Only bounded deque/accounting work under this lock: no IO, callbacks,
        # downstream locks or waits for capacity. Contention is not PCM loss.
        timestamp = self.clock()
        with self._lock:
            if not data or len(data) % 4 or len(data) > self.max_bytes:
                self.dropped += 1
                self._counts['invalid_dropped_bytes'] += len(data)
                return False
            while self._items and self._bytes + len(data) > self.max_bytes:
                _, old = self._items.popleft()
                self._bytes -= len(old)
                self.dropped += 1
                self._counts['capacity_dropped_bytes'] += len(old)
            self._items.append((timestamp, data))
            self._bytes += len(data)
            self._counts['accepted_bytes'] += len(data)
            wakeup = self._wakeup
        if wakeup is not None:
            wakeup.set()
        return True

    def pop(self) -> bytes | None:
        with self._lock:
            while self._items:
                timestamp, data = self._items.popleft()
                self._bytes -= len(data)
                if self.clock() - timestamp <= self.max_age_s:
                    return data
                self.dropped += 1
                self._counts['stale_dropped_bytes'] += len(data)
            return None

    def clear(self):
        with self._lock:
            self._counts['cleared_bytes'] += self._bytes
            self._items.clear()
            self._bytes = 0

    @property
    def buffered_bytes(self):
        with self._lock:
            return self._bytes


class PcmOutput:
    """One short queue per consumer, drained by the mux output worker.

    Empty returns None, indicating silence/no audio to the transport.
    Slow consumers lose old audio independently within the configured capacity/age.
    """
    def __init__(self, consumer_buffer_ms=DEFAULT_CONSUMER_BUFFER_MS):
        self._max_bytes = pcm_capacity_bytes(consumer_buffer_ms)
        self._max_age_s = consumer_buffer_ms / 1000.0
        self._queues: dict[int, PcmQueue] = {}
        self._lock = Lock()

    def attach(self, consumer_id: int):
        with self._lock:
            self._queues.setdefault(consumer_id, PcmQueue(max_bytes=self._max_bytes, max_age_s=self._max_age_s))

    def detach(self, consumer_id: int):
        with self._lock:
            self._queues.pop(consumer_id, None)

    def publish(self, data: bytes):
        with self._lock:
            queues = tuple(self._queues.values())
        for queue in queues:
            queue.put(data)

    def read(self, consumer_id: int) -> bytes | None:
        with self._lock:
            queue = self._queues.get(consumer_id)
        return queue.pop() if queue else None

    def set_wakeup(self, consumer_id, wakeup):
        with self._lock:
            queue = self._queues.get(consumer_id)
        if queue is not None:
            queue.set_wakeup(wakeup)

    def snapshot(self):
        with self._lock:
            queues = tuple(self._queues.items())
        return {consumer: queue.snapshot() for consumer, queue in queues}

    def clear(self):
        with self._lock:
            for queue in self._queues.values():
                queue.clear()
