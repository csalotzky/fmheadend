"""Private audiod v1 framing. No native imports; two bounded stream channels.

Every record is uint32 network-order length, uint32 JSON-header length, UTF-8
JSON envelope, optional PCM bytes. A partial record timeout invalidates the
connection: never resume by interpreting its tail as another record.
"""
from collections import deque
import json
import socket
import struct
import threading
import time

VERSION = 1
MAX_JSON = 65536
MAX_PCM = 65536
MAX_RECORD = 4 + MAX_JSON + MAX_PCM
IO_TIMEOUT = 0.2
PCM_MAX_AGE = 0.1
KINDS = {'create', 'accepted', 'prepared', 'status', 'shutdown', 'terminated', 'pcm',
         'local_request', 'local_response', 'podcast_snapshot', 'podcast_played'}


class ProtocolError(ValueError):
    pass


def envelope(source_id, generation, sequence, kind, payload=None):
    return dict(version=VERSION, source_id=source_id, generation=generation,
                sequence=sequence, kind=kind, payload=payload or {})


def validate(message):
    if not isinstance(message, dict) or set(message) != {
            'version', 'source_id', 'generation', 'sequence', 'kind', 'payload'}:
        raise ProtocolError('Invalid envelope')
    if (type(message['version']) is not int or message['version'] != VERSION
            or type(message['source_id']) is not int or message['source_id'] < 0
            or type(message['sequence']) is not int or message['sequence'] < 1
            or not isinstance(message['generation'], str)
            or len(message['generation']) != 32
            or any(c not in '0123456789abcdef' for c in message['generation'])
            or not isinstance(message['kind'], str) or message['kind'] not in KINDS
            or not isinstance(message['payload'], dict)):
        raise ProtocolError('Unsupported or malformed envelope')


def pack(message, pcm=b''):
    validate(message)
    if (message['kind'] == 'pcm') != bool(pcm) or len(pcm) > MAX_PCM or len(pcm) % 4:
        raise ProtocolError('Invalid PCM record')
    header = json.dumps(message, separators=(',', ':'), allow_nan=False).encode('utf8')
    if len(header) > MAX_JSON:
        raise ProtocolError('JSON payload too large')
    body = struct.pack('!I', len(header)) + header + pcm
    return struct.pack('!I', len(body)) + body


def unpack(body):
    if not 4 <= len(body) <= MAX_RECORD:
        raise ProtocolError('Invalid record length')
    size, = struct.unpack('!I', body[:4])
    if not 1 <= size <= MAX_JSON or size + 4 > len(body):
        raise ProtocolError('Invalid header length')
    try:
        message = json.loads(body[4:4 + size])
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError('Invalid JSON') from exc
    validate(message)
    pcm = body[4 + size:]
    if (message['kind'] == 'pcm') != bool(pcm) or len(pcm) > MAX_PCM or len(pcm) % 4:
        raise ProtocolError('Invalid PCM payload')
    return message, pcm


def receive(sock):
    """Idle timeout is retryable; a partial-frame timeout is fatal."""
    deadline = None

    def exact(size):
        nonlocal deadline
        data = bytearray()
        while len(data) < size:
            try:
                chunk = sock.recv(size - len(data))
            except socket.timeout as exc:
                if deadline is None:
                    raise
                raise ProtocolError('Partial record timeout') from exc
            if not chunk:
                raise EOFError('Audio channel closed')
            if deadline is None:
                deadline = time.monotonic() + IO_TIMEOUT
            if time.monotonic() > deadline:
                raise ProtocolError('Record deadline exceeded')
            data.extend(chunk)
        return data

    size, = struct.unpack('!I', exact(4))
    if not 4 <= size <= MAX_RECORD:
        raise ProtocolError('Record exceeds limit')
    return unpack(exact(size))


def configure(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    sock.settimeout(IO_TIMEOUT)


class PacketQueue:
    """At most four encoded blocks, oldest-drop, no socket IO under lock."""
    def __init__(self):
        self.items = deque(maxlen=4)
        self.lock = threading.Lock()
        self.dropped = 0

    def put(self, packet):
        if len(packet) > MAX_RECORD + 4:
            raise ProtocolError('Packet exceeds limit')
        with self.lock:
            if len(self.items) == self.items.maxlen:
                self.dropped += 1
            self.items.append((time.monotonic(), packet))

    def pop(self):
        with self.lock:
            while self.items:
                when, packet = self.items.popleft()
                if time.monotonic() - when <= PCM_MAX_AGE:
                    return packet
                self.dropped += 1
        return None

    def clear(self):
        with self.lock:
            self.items.clear()
