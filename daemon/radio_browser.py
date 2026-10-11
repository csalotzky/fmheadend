"""Bounded, read-only Radio Browser client. No Source/playback side effects."""
import asyncio
import json
import random
import re
import socket
import time
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from . import __version__
from .errors import DomainError

MAX_BYTES = 2 * 1024 * 1024
FALLBACK_HOSTS = ('de1.api.radio-browser.info', 'at1.api.radio-browser.info')


def http_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ('http', 'https') and parsed.hostname and parsed.port != 0:
            return value
    except ValueError:
        pass
    return None


def playable_url(url, resolved, hls=False):
    """Prefer the durable original; plain station playlists need a resolved URI.

    GStreamer uridecodebin handles redirects/HLS, not generic M3U/PLS station
    playlists. url_resolved is the same entry, never a quality alternative.
    """
    original, resolved = http_url(url), http_url(resolved)
    if original:
        suffix = urlsplit(original).path.lower()
        if not hls and suffix.endswith(('.m3u', '.pls', '.asx', '.xspf')):
            return resolved
        return original
    return resolved


class Station(BaseModel):
    stationuuid: UUID
    name: str
    url: str
    url_resolved: str | None = None
    playable_url: str | None = None
    countrycode: str = ''
    country: str = ''
    tags: list[str] = Field(default_factory=list)
    codec: str = ''
    bitrate: int = Field(default=0, ge=0)
    favicon: str | None = None
    lastcheckok: bool | None = None
    hls: bool = False


class Stations(BaseModel):
    stations: list[Station]
    offset: int
    limit: int


class Option(BaseModel):
    name: str
    stationcount: int = Field(ge=0)


def station(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid station')
    # Missing health is unknown, never interpreted as a successful check.
    def flag(name, default=None):
        value = data.get(name, default)
        if value is None:
            return default
        if value not in (0, 1, '0', '1', False, True):
            raise ValueError('Invalid flag')
        return value in (1, '1', True)
    hls = flag('hls', False)
    raw_tags = data.get('tags') or ''
    if not isinstance(raw_tags, str):
        raise ValueError('Invalid tags')
    url = data.get('url') or ''
    resolved = data.get('url_resolved') or None
    return Station(stationuuid=data['stationuuid'], name=data['name'], url=url,
        url_resolved=resolved, playable_url=playable_url(url, resolved, hls),
        countrycode=data.get('countrycode') or '', country=data.get('country') or '',
        tags=[tag.strip() for tag in raw_tags.split(',') if tag.strip()],
        codec=data.get('codec') or '', bitrate=data.get('bitrate') or 0,
        favicon=http_url(data.get('favicon')), lastcheckok=flag('lastcheckok'), hls=hls)


async def discover_servers():
    """Official all.api DNS + PTR discovery, bounded and off the event loop."""
    loop = asyncio.get_running_loop()
    hosts = set()
    try:
        async with asyncio.timeout(4):
            addresses = await loop.getaddrinfo('all.api.radio-browser.info', 443, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(item[4] for item in addresses))[:8]
            results = await asyncio.gather(*(loop.getnameinfo(address, socket.NI_NAMEREQD)
                                            for address in addresses), return_exceptions=True)
            for result in results:
                if not isinstance(result, BaseException):
                    host = result[0].lower().rstrip('.')
                    if re.fullmatch(r'[a-z0-9-]+\.api\.radio-browser\.info', host):
                        hosts.add(host)
    except (OSError, TimeoutError):
        pass
    result = sorted(hosts)
    random.shuffle(result)
    return result or list(FALLBACK_HOSTS)


class RadioBrowserClient:
    def __init__(self, *, transport=None, discovery=discover_servers):
        self.discovery = discovery
        self._hosts = []
        self._hosts_until = 0.0
        self._discovery_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(4)
        self.http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(4, connect=3, pool=2),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={'User-Agent': f'FMheadend/{__version__} (radio directory and linked-stream recovery)',
                     'Accept': 'application/json', 'Accept-Encoding': 'identity'})

    async def close(self):
        await self.http.aclose()

    async def _servers(self):
        async with self._discovery_lock:
            if time.monotonic() >= self._hosts_until:
                self._hosts = await self.discovery()
                self._hosts_until = time.monotonic() + 900
            return self._hosts or list(FALLBACK_HOSTS)

    async def _read(self, path, params, normalize):
        try:
            async with asyncio.timeout(15), self._slots:
                hosts = await self._servers()
                for host in hosts[:2]:  # At most one retry, on another official mirror.
                    if not re.fullmatch(r'[a-z0-9-]+\.api\.radio-browser\.info', host):
                        continue
                    try:
                        async with self.http.stream('GET', f'https://{host}{path}', params=params) as response:
                            response.raise_for_status()
                            if response.headers.get('content-encoding', 'identity') != 'identity':
                                raise ValueError('Unexpected compressed response')
                            body = bytearray()
                            async for block in response.aiter_bytes(16384):
                                body.extend(block)
                                if len(body) > MAX_BYTES:
                                    raise ValueError('Response too large')
                        rows = json.loads(body)
                        if not isinstance(rows, list) or len(rows) > params['limit']:
                            raise ValueError('Invalid result page')
                        return normalize(rows)
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        continue
                self._hosts_until = 0  # Rediscover on next user request/recovery, not in a loop.
        except (TimeoutError, OSError):
            pass
        raise DomainError(503, 'RADIO_BROWSER_UNAVAILABLE', 'Radio Browser is temporarily unavailable or returned an invalid response')

    async def search(self, *, name='', countrycode=None, tag=None, offset=0, limit=25, hidebroken=True):
        params = dict(name=name, offset=offset, limit=limit, hidebroken=str(hidebroken).lower(), order='name')
        if countrycode:
            params['countrycode'] = countrycode.upper()
        if tag:
            params['tag'] = tag
        rows = await self._read('/json/stations/search', params, lambda rows: [station(row) for row in rows])
        if hidebroken:
            rows = [row for row in rows if row.lastcheckok is not False]
        return Stations(stations=rows, offset=offset, limit=limit)

    async def lookup(self, uuid):
        uuid = UUID(str(uuid))
        rows = await self._read(f'/json/stations/byuuid/{uuid}', dict(limit=1, offset=0, hidebroken='false'),
                                lambda rows: [station(row) for row in rows])
        if rows and rows[0].stationuuid != uuid:
            raise DomainError(503, 'RADIO_BROWSER_UNAVAILABLE', 'Radio Browser returned an invalid station identity')
        return rows[0] if rows else None

    async def countries(self, *, offset=0, limit=250):
        return await self._read('/json/countrycodes', dict(offset=offset, limit=limit, order='name'),
                               lambda rows: [Option.model_validate(row) for row in rows])

    async def tags(self, *, name='', offset=0, limit=25):
        path = '/json/tags' + ('/' + quote(name, safe='') if name else '')
        return await self._read(path, dict(offset=offset, limit=limit, order='name'),
                               lambda rows: [Option.model_validate(row) for row in rows])
