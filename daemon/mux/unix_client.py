"""Unix mux control and PCM client. No SPI access or sample processing."""
from dataclasses import dataclass
import json
from pathlib import Path
import socket
import threading
import time
from typing import Callable

from ..errors import DependencyUnavailable
from ..sources.output import PcmOutput


@dataclass
class Producer:
    source_id: int
    output: PcmOutput
    sock: socket.socket | None
    pending: bytes = b""
    offset: int = 0
    since: float = 0
    retry_at: float = 0
    disconnects: int = 0
    stale_disconnects: int = 0
    discarded_pending_bytes: int = 0


class UnixMuxClient:
    def __init__(self, output_provider: Callable[[int], PcmOutput | None], runtime_dir="/run/fmheadend"):
        self.output_provider = output_provider
        self.runtime_dir = Path(runtime_dir)
        self.producers: dict[int, Producer] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._work = threading.Event()
        self._thread = None
        self.last_error: str | None = None

    def _connect(self, channel):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Reconnect runs on the shared writer. Never wait for a channel's
            # listen backlog while the other seven need PCM delivery.
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            sock.connect(str(self.runtime_dir / f"pcm{channel}.sock"))
            return sock
        except OSError:
            sock.close()
            raise

    def attach(self, modulator_id: int, source_id: int):
        if not 0 <= modulator_id < 8:
            raise ValueError("PCM channel must be 0..7")
        with self._lock:
            existing = self.producers.get(modulator_id)
            if existing and existing.source_id == source_id:
                return
        output = self.output_provider(source_id)
        if output is None:
            raise DependencyUnavailable("Source has no digital PCM output")
        try:
            sock = self._connect(modulator_id)
        except OSError as exc:
            # Keep the producer so mux startup/restart needs no API reconfiguration.
            sock = None
            self.last_error = str(exc)
        with self._lock:
            if self._stop.is_set():
                if sock:
                    sock.close()
                raise DependencyUnavailable("Mux client is closed")
            if existing and existing.sock:
                existing.sock.close()
            if existing:
                existing.output.set_wakeup(modulator_id, None)
            self.producers[modulator_id] = Producer(source_id, output, sock)
            output.set_wakeup(modulator_id, self._work)
            self._work.set()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="mux-pcm-output", daemon=True)
                self._thread.start()

    def detach(self, modulator_id: int):
        with self._lock:
            producer = self.producers.pop(modulator_id, None)
            if producer:
                producer.output.set_wakeup(modulator_id, None)
            if producer and producer.sock:
                producer.sock.close()
        self._work.set()

    def _disconnect(self, producer, now, error):
        producer.disconnects += 1
        producer.discarded_pending_bytes += len(producer.pending) - producer.offset
        if producer.sock:
            producer.sock.close()
        producer.sock = None
        producer.pending = b""
        producer.offset = 0
        producer.retry_at = now + 0.25
        self.last_error = str(error)

    def _step(self, channel: int, producer: Producer, now: float):
        if producer.sock is None:
            if now < producer.retry_at:
                return
            try:
                producer.sock = self._connect(channel)
            except OSError as exc:
                self._disconnect(producer, now, exc)
                return
        if producer.pending and now - producer.since > 0.1:
            # A partial stereo frame cannot be abandoned inside a stream.
            # Close the producer session so muxd discards its partial bytes too.
            producer.stale_disconnects += 1
            self._disconnect(producer, now, "PCM writer stalled; discarded stale audio")
            return
        if not producer.pending:
            data = producer.output.read(channel)
            if data is None:
                return
            producer.pending, producer.offset, producer.since = data, 0, now
        try:
            sent = producer.sock.send(memoryview(producer.pending)[producer.offset:])
            if sent == 0:
                raise ConnectionError("Mux PCM socket closed")
            producer.offset += sent
            if producer.offset == len(producer.pending):
                producer.pending, producer.offset = b"", 0
                self.last_error = None
            return True
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            self._disconnect(producer, now, exc)

    def _drain(self):
        # One nonblocking send per channel per round. Bound catch-up work and
        # release the ownership lock between rounds for attach/detach/close.
        for _ in range(16):
            if self._stop.is_set():
                return False
            progressed = False
            with self._lock:
                for channel, producer in self.producers.items():
                    progressed |= bool(self._step(channel, producer, time.monotonic()))
            if not progressed:
                return False
        return True

    def _run(self):
        while not self._stop.is_set():
            self._work.clear()
            if self._drain():
                continue
            with self._lock:
                pending = any(p.pending for p in self.producers.values())
            # Incoming PCM wakes idle immediately. Only blocked socket writes
            # need the existing 2 ms retry cadence; idle reconnects stay bounded.
            self._work.wait(0.002 if pending else 0.05)

    def pcm_snapshot(self):
        with self._lock:
            return {channel: dict(disconnects=p.disconnects,
                                  stale_disconnects=p.stale_disconnects,
                                  discarded_pending_bytes=p.discarded_pending_bytes)
                    for channel, p in self.producers.items()}

    def get_status(self) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(str(self.runtime_dir / "mux-control.sock"))
                sock.sendall(b'{"command":"get_status"}\n')
                data = bytearray()
                deadline = time.monotonic() + 0.5
                while b"\n" not in data:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Mux status response timed out")
                    sock.settimeout(remaining)
                    block = sock.recv(4096)
                    if not block:
                        raise ValueError("Incomplete mux status response")
                    data.extend(block)
                    if len(data) > 16384:
                        raise ValueError("Mux status response exceeds size limit")
            status = json.loads(data)
            if not isinstance(status, dict) or status.get("state") != "RUNNING" or len(status.get("channels", [])) != 8:
                raise ValueError("Invalid mux status")
            if [c["id"] for c in status["channels"]] != list(range(8)):
                raise ValueError("Invalid mux channel IDs")
            for channel in status["channels"]:
                for key in ("underruns", "overruns", "bytes_received", "bytes_consumed"):
                    if type(channel.get(key)) is not int or channel[key] < 0:
                        raise ValueError(f"Invalid mux channel counter: {key}")
            for key in ("frames_sent", "spi_errors", "transport_state", "mocked", "transport", "last_error"):
                if key not in status:
                    raise ValueError(f"Missing mux status field: {key}")
            return status
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DependencyUnavailable(f"Mux control unavailable: {exc}") from exc

    def close(self):
        self._stop.set()
        self._work.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            for producer in self.producers.values():
                if producer.sock:
                    producer.sock.close()
            for channel, producer in self.producers.items():
                producer.output.set_wakeup(channel, None)
            self.producers.clear()
