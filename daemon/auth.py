"""Optional app-wide Basic Auth; bounded work independent of control/audio."""
import asyncio
import base64
import binascii
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import time

import bcrypt
from starlette.responses import Response

logger = logging.getLogger(__name__)
MAX_FILE_BYTES = 65536
MAX_HEADER_BYTES = 512
MAX_PENDING = 8
CACHE_SIZE = 128
CACHE_TTL = 30.0
# Canonical bcrypt encoding, including unused salt/checksum bits. Cap cost to
# bound verification time; Apache's default cost is well below this ceiling.
HASH = re.compile(rb'\$2[aby]\$(0[4-9]|1[0-6])\$[./A-Za-z0-9]{21}[.Oeu][./A-Za-z0-9]{30}[.CGKOSWaeimquy26]')


class PasswordFileError(ValueError):
    pass


class BasicAuth:
    def __init__(self, path, clock=time.monotonic):
        self.path, self.clock = Path(path), clock
        self.key = secrets.token_bytes(32)
        self.cache = OrderedDict()
        self.users, self.content = {}, None
        self.executor = None
        self.pending = set()
        self.closing = True
        self.file_error = None

    def _read(self):
        try:
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
            with os.fdopen(os.open(self.path, flags), 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise PasswordFileError('not a regular file')
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise PasswordFileError('file exceeds 64 KiB')
            digest = (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns,
                      metadata.st_ctime_ns, hashlib.sha256(raw).digest())
            if digest == self.content:
                return
            self.cache.clear()
            users = {}
            for number, line in enumerate(raw.splitlines(), 1):
                if not line or line.startswith(b'#'):
                    continue
                user, separator, hashed = line.partition(b':')
                if (not separator or not 1 <= len(user) <= 128 or
                        any(c < 32 or c == 127 for c in user) or
                        user in users or not HASH.fullmatch(hashed)):
                    raise PasswordFileError(f'invalid bcrypt entry at line {number}')
                user.decode('utf-8')
                users[user] = hashed
            if not users:
                raise PasswordFileError('no credentials configured')
            self.users, self.content = users, digest
            self.file_error = None
        except (OSError, UnicodeError, PasswordFileError) as exc:
            self.cache.clear()
            self.users, self.content = {}, None
            # Never expose file content, hashes, or submitted credentials.
            reason = str(exc) if isinstance(exc, PasswordFileError) else type(exc).__name__
            raise PasswordFileError(f'Password file {self.path}: {reason}') from None

    async def start(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='headend-auth')
        self.closing = False
        try:
            await self._submit(self._read)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.closing = True
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
        self.cache.clear()
        self.users, self.content = {}, None

    async def _submit(self, function, *args):
        if self.closing or len(self.pending) >= MAX_PENDING:
            return False
        future = asyncio.get_running_loop().run_in_executor(self.executor, function, *args)
        self.pending.add(future)
        future.add_done_callback(self.pending.discard)
        # A cancelled HTTP request cannot release capacity while work still runs.
        return await asyncio.shield(future)

    def _verify(self, user, password):
        try:
            self._read()
            token = hmac.digest(self.key, user + b':' + password, 'sha256')
            expires = self.cache.get(token, 0)
            if expires > self.clock():
                self.cache.move_to_end(token)
                return True
            self.cache.pop(token, None)
            hashed = self.users.get(user)
            if hashed is None or not bcrypt.checkpw(password, hashed):
                return False
            generation = self.content
            self._read()  # Do not cache/accept a result for a replaced file.
            if self.content != generation:
                return False
            self.cache[token] = self.clock() + CACHE_TTL
            while len(self.cache) > CACHE_SIZE:
                self.cache.popitem(last=False)
            return True
        except PasswordFileError as exc:
            if self.file_error != str(exc):
                logger.error('%s', exc)
                self.file_error = str(exc)
            return False

    async def authenticate(self, headers):
        values = [value for name, value in headers if name.lower() == b'authorization']
        if len(values) != 1 or len(values[0]) > MAX_HEADER_BYTES:
            return False
        scheme, separator, encoded = values[0].partition(b' ')
        if not separator or scheme.lower() != b'basic':
            return False
        try:
            raw = base64.b64decode(encoded, validate=True)
            user, separator, password = raw.partition(b':')
            if not separator or not 1 <= len(user) <= 128 or len(password) > 72:
                return False
            raw.decode('utf-8')
        except (binascii.Error, UnicodeError):
            return False
        return await self._submit(self._verify, user, password)


class BasicAuthMiddleware:
    def __init__(self, app, auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and not await self.auth.authenticate(scope['headers']):
            response = Response(status_code=401, headers={
                'WWW-Authenticate': 'Basic realm="FMheadend", charset="UTF-8"',
                'Cache-Control': 'no-store',
            })
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
