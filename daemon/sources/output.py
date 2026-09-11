"""Bounded byte transport only. No audio arithmetic and no hardware IO."""
from collections import deque
from threading import Lock
import time

PCM_CAPS = "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"
PCM_BYTES_PER_SECOND = 48000 * 2 * 2


class PcmQueue:
    def __init__(self, max_bytes=19200, max_age_s=0.1, clock=time.monotonic):
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s
        self.clock = clock
        self._items = deque()
        self._bytes = 0
        self._lock = Lock()
        self.dropped = 0

    def put(self, data: bytes) -> bool:
        # Streaming producers never wait for a reader or a contended lock.
        if not data or len(data) % 4 or len(data) > self.max_bytes:
            self.dropped += 1
            return False
        if not self._lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            while self._items and self._bytes + len(data) > self.max_bytes:
                _, old = self._items.popleft()
                self._bytes -= len(old)
                self.dropped += 1
            self._items.append((self.clock(), data))
            self._bytes += len(data)
            return True
        finally:
            self._lock.release()

    def pop(self) -> bytes | None:
        with self._lock:
            while self._items:
                timestamp, data = self._items.popleft()
                self._bytes -= len(data)
                if self.clock() - timestamp <= self.max_age_s:
                    return data
                self.dropped += 1
            return None

    def clear(self):
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def buffered_bytes(self):
        with self._lock:
            return self._bytes


class PcmOutput:
    """One short queue per consumer, drained by the mux output worker.

    Empty returns None, indicating silence/no audio to the transport.
    Slow consumers lose old audio independently. Never stores more than 100 ms.
    """
    def __init__(self):
        self._queues: dict[int, PcmQueue] = {}
        self._lock = Lock()

    def attach(self, consumer_id: int):
        with self._lock:
            self._queues.setdefault(consumer_id, PcmQueue())

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

    def clear(self):
        with self._lock:
            for queue in self._queues.values():
                queue.clear()
