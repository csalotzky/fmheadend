"""Optional local NDJSON diagnostics. Native elements never cross this boundary."""
import asyncio
from contextlib import suppress
import errno
import json
import logging
import os
from pathlib import Path
import socket
import stat
import time

from .errors import DomainError

log = logging.getLogger(__name__)


class AudioTelemetry:
    def __init__(self, path, hz, control, sources):
        self.path = Path(path)
        self.hz, self.control, self.sources = hz, control, sources
        self.server = self.client = self._owner = self._identity = None

    async def start(self):
        # A persistent, locked ownership marker permits recovery only of our own
        # stale socket inode. Never unlink an arbitrary file or live endpoint.
        import fcntl
        fd = os.open(str(self.path) + '.owner', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._owner = os.fdopen(fd, 'r+')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(fd).st_uid != os.getuid():
            raise OSError('Audio telemetry ownership marker has a different owner')
        content = self._owner.read()
        try:
            marker = json.loads(content) if content else None
        except ValueError as exc:
            raise OSError('Unrecognized audio telemetry ownership marker') from exc
        if content and (not isinstance(marker, dict) or marker.get('application') != 'fmheadend-audio-telemetry'):
            raise OSError('Unrecognized audio telemetry ownership marker')
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            expected = dict(application='fmheadend-audio-telemetry', dev=existing.st_dev, ino=existing.st_ino)
            if (not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid() or marker != expected):
                raise OSError('Refusing to replace an unowned audio telemetry path')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise
                else:
                    raise OSError('Audio telemetry socket is already active')
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            info = self.path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            self._owner.seek(0)
            self._owner.truncate()
            json.dump(dict(application='fmheadend-audio-telemetry', dev=info.st_dev, ino=info.st_ino), self._owner)
            self._owner.flush()
            listener.listen(1)
            listener.setblocking(False)
            self.server = await asyncio.start_unix_server(self._connected, sock=listener)
        except BaseException:
            listener.close()
            raise

    def _connected(self, reader, writer):
        if self.client is not None:
            writer.close()
            return
        self.client = asyncio.create_task(self._serve(reader, writer))

    async def _request(self, enabled):
        # Control calls are bounded/serialized; never queue a telemetry backlog.
        while True:
            try:
                await self.control.call(lambda: self.sources.set_telemetry(enabled, self.hz))
                return
            except DomainError as exc:
                if exc.error != 'CONTROL_BUSY':
                    raise
                await asyncio.sleep(0.05)

    async def _emit(self, writer):
        loop = asyncio.get_running_loop()
        period = 1 / self.hz
        next_deadline = loop.time()
        while True:
            sources = self.sources.telemetry_snapshot()
            record = dict(schema=1, emit_monotonic_ns=time.monotonic_ns(), sources=sources)
            writer.write((json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n').encode())
            # At most one outstanding record: disconnect a blocked client.
            await asyncio.wait_for(writer.drain(), timeout=0.25)
            next_deadline += period
            now = loop.time()
            if next_deadline <= now:
                # Missed deadlines are discarded, never replayed as a burst.
                next_deadline = now + period
            await asyncio.sleep(next_deadline - now)

    async def _serve(self, reader, writer):
        tasks = []
        try:
            writer.transport.set_write_buffer_limits(high=16384, low=0)
            await self._request(True)
            tasks = [asyncio.create_task(self._emit(writer)), asyncio.create_task(reader.read(1))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        except Exception:
            log.exception('Audio telemetry client ended')
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with suppress(ConnectionError, OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.25)
            await self._request(False)
            self.client = None

    async def close(self):
        try:
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            if self.client is not None:
                task = self.client
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            try:
                if self._identity is not None:
                    with suppress(FileNotFoundError):
                        info = self.path.lstat()
                        if (info.st_dev, info.st_ino) == self._identity:
                            self.path.unlink()
            finally:
                if self._owner is not None:
                    self._owner.close()
