"""Private audiod v1 framing. No native imports; two bounded stream channels.

Control/legacy records are uint32 length, uint32 JSON-header length, JSON and
optional PCM. PCM1 records use the same outer length with a fixed binary header
and complete cached JSON metadata. A partial record timeout invalidates the
connection: never resume by interpreting its tail as another record.
"""
from collections import deque
import json
import socket
import struct
import threading
import time
from .output import PCM_CAPS

VERSION = 1
MAX_JSON = 65536
MAX_PCM = 65536
MAX_RECORD = 4 + MAX_JSON + MAX_PCM
IO_TIMEOUT = 0.2
PCM_MAX_AGE = 0.1
KINDS = {'create', 'accepted', 'prepared', 'status', 'shutdown', 'terminated', 'pcm',
         'local_request', 'local_response', 'podcast_snapshot', 'podcast_played', 'output_lease', 'output_applied', 'direct_progress', 'heartbeat', 'direct_state', 'state_request', 'radio_command', 'radio_state', 'telemetry_request', 'telemetry'}
# Private PCM v1 fast record: identity/ordering/time stay on every packet.
# Complete metadata also stays on every packet, so oldest-drop needs no ACK or
# metadata side channel. Only its JSON encoding/validation is cached.
_PCM_HEADER = struct.Struct('!4sQ16sQQI')
_PCM_MAGIC = b'PCM1'


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
    if body[:4] == _PCM_MAGIC:
        return PcmDecoder().unpack(body)
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


def receive(sock, pcm_decoder=None):
    """Idle timeout is retryable; a partial-frame timeout is fatal."""
    deadline = None

    def exact(size):
        nonlocal deadline
        data = None
        received = 0
        while received < size:
            try:
                chunk = sock.recv(size - received)
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
            if received == 0 and len(chunk) == size:
                return chunk  # Common case: no intermediate bytearray/copy.
            if data is None:
                data = bytearray()
            data.extend(chunk)
            received += len(chunk)
        return data

    size, = struct.unpack('!I', exact(4))
    if not 4 <= size <= MAX_RECORD:
        raise ProtocolError('Record exceeds limit')
    body = exact(size)
    if pcm_decoder is not None and body[:4] == _PCM_MAGIC:
        return pcm_decoder.unpack(body)
    return unpack(body)


class _PcmPayload(dict):
    """Reader-private metadata token; never serialized or exposed to clients."""


class PcmEncoder:
    def __init__(self, source_id, generation):
        validate(envelope(source_id, generation, 1, 'pcm'))
        if source_id >= 1 << 64:
            raise ProtocolError('PCM source ID exceeds wire range')
        self.source_id = source_id
        self.generation = bytes.fromhex(generation)
        self.metadata = None
        self.encoded = None

    def pack(self, sequence, produced_ns, status, probe, pcm):
        if (type(sequence) is not int or not 0 < sequence < 1 << 64
                or type(produced_ns) is not int or not 0 < produced_ns < 1 << 64
                or not pcm or len(pcm) > MAX_PCM or len(pcm) % 4):
            raise ProtocolError('Invalid PCM record')
        metadata = dict(format=PCM_CAPS, status=status, probe=probe)
        if metadata != self.metadata:
            encoded = json.dumps(metadata, separators=(',', ':'), allow_nan=False).encode('utf8')
            if len(encoded) > MAX_JSON:
                raise ProtocolError('JSON payload too large')
            # Own the key: caller mutation must not make a stale encoding match.
            from copy import deepcopy
            self.metadata = deepcopy(metadata)
            self.encoded = encoded
        size = _PCM_HEADER.size + len(self.encoded) + len(pcm)
        if size > MAX_RECORD:
            raise ProtocolError('Record exceeds limit')
        return b''.join((struct.pack('!I', size), _PCM_HEADER.pack(_PCM_MAGIC,
            self.source_id, self.generation, sequence, produced_ns, len(self.encoded)),
            self.encoded, pcm))


class PcmDecoder:
    """One PCM reader owns this bounded one-entry cache for its generation."""
    def __init__(self):
        self.encoded = None
        self.metadata = None

    def unpack(self, body):
        if not _PCM_HEADER.size < len(body) <= MAX_RECORD:
            raise ProtocolError('Invalid PCM record length')
        magic, source_id, generation, sequence, produced_ns, size = _PCM_HEADER.unpack_from(body)
        offset = _PCM_HEADER.size + size
        if (magic != _PCM_MAGIC or not sequence or not produced_ns
                or not 0 < size <= MAX_JSON or offset >= len(body)
                or len(body)-offset > MAX_PCM or (len(body)-offset) % 4):
            raise ProtocolError('Invalid PCM header')
        encoded = body[_PCM_HEADER.size:offset]
        if encoded != self.encoded:
            try:
                metadata = json.loads(encoded)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ProtocolError('Invalid PCM JSON') from exc
            if (not isinstance(metadata, dict) or set(metadata) != {'format','status','probe'}
                    or not isinstance(metadata['status'], dict) or not isinstance(metadata['probe'], dict)):
                raise ProtocolError('Invalid PCM metadata')
            self.metadata, self.encoded = metadata, bytes(encoded)
        payload = _PcmPayload(self.metadata, produced_ns=produced_ns)
        payload.metadata_key = self.encoded
        message = envelope(source_id, generation.hex(), sequence, 'pcm', payload)
        # Own only PCM bytes, not its (potentially 64 KiB) metadata backing.
        # A tiny PCM block must not retain a large header in each fanout queue.
        # This also preserves PcmOutput's existing bytes contract.
        pcm = bytes(memoryview(body)[offset:])
        return message, pcm


def configure(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    sock.settimeout(IO_TIMEOUT)


class PacketQueue:
    """At most four encoded blocks, oldest-drop, no socket IO under lock."""
    def __init__(self):
        self.items = deque(maxlen=4)
        self.lock = threading.Lock()
        self.available = threading.Condition(self.lock)
        self.dropped = 0

    def put(self, packet):
        if len(packet) > MAX_RECORD + 4:
            raise ProtocolError('Packet exceeds limit')
        with self.available:
            if len(self.items) == self.items.maxlen:
                self.dropped += 1
            self.items.append((time.monotonic(), packet))
            self.available.notify()

    def pop(self, timeout=0):
        with self.available:
            if timeout and not self.items:
                self.available.wait_for(lambda: self.items, timeout=timeout)
            while self.items:
                when, packet = self.items.popleft()
                if time.monotonic() - when <= PCM_MAX_AGE:
                    return packet
                self.dropped += 1
        return None

    def clear(self):
        with self.lock:
            self.items.clear()
