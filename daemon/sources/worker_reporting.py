"""Bounded cached state events plus one source heartbeat; no native queries."""
import time

from ..models.configuration import SourceTestResult
from ..models.runtime_status import InputSourceStatus
from .audio_ipc import ProtocolError


class WorkerReporter:
    def __init__(self, pipeline, output, catalog, emit, clock=time.monotonic):
        self.pipeline, self.output, self.catalog, self.emit = pipeline, output, catalog, emit
        self.clock = clock
        self.key = object()
        self.next_heartbeat = 0
        self.radio_key = None
        self.next_telemetry = 0

    def poll(self, force=False):
        p = self.pipeline
        radio = getattr(p, 'radio_url', None)
        if radio is not None:
            eligible, trial = radio.observe()
            observation = dict(revision=getattr(p, '_radio_revision', 0), eligible=eligible, trial=trial)
            if force or observation != self.radio_key:
                self.emit('radio_state', observation)
                self.radio_key = observation
        enabled, hz = getattr(p, '_telemetry_request', (False, 50.0))
        if enabled and self.clock() >= self.next_telemetry:
            self.emit('telemetry', dict(revision=getattr(p, '_worker_telemetry_revision', 0), sample=p.telemetry_snapshot()))
            self.next_telemetry = self.clock() + 1/hz
        waiting = self.clock() < p.local_wait_until if self.catalog is not None else None
        revision = getattr(p, 'status_revision', None)
        # Compatibility for injected test decoders only. GstPipeline supplies a
        # scalar revision updated at the state mutation, not a polled tree.
        fallback = p.snapshot() if revision is None else None
        key = (revision if revision is not None else fallback, p.fatal_error, waiting)
        if force or key != self.key:
            value = dict(status=fallback if fallback is not None else p.snapshot(),
                         staging=p.pcm_snapshot()['staging'], fatal_error=p.fatal_error)
            if self.catalog is not None:
                value['local_waiting'] = waiting
            self.emit('status', value)
            self.key = key
        if self.output is not None:
            changes = self.output.changes(force=force)
            if changes is not None:
                self.emit('direct_state', changes)
        now = self.clock()
        if now >= self.next_heartbeat:
            self.emit('heartbeat', self.output.heartbeat() if self.output is not None else {})
            self.next_heartbeat = now + 1.0


def accept_report(parent, kind, value):
    now = time.monotonic()
    if kind == 'heartbeat':
        if parent.direct_runtime is None:
            if value:
                raise ProtocolError('Unexpected PCM1 heartbeat fields')
        else:
            if set(value) != {'blocks', 'bytes', 'produced_ns', 'writer_at'}:
                raise ProtocolError('Invalid heartbeat')
            previous = parent.direct_progress
            for key in ('blocks', 'bytes', 'produced_ns'):
                if type(value[key]) is not int or not 0 <= value[key] < 2**63 or value[key] < previous.get(key, 0):
                    raise ProtocolError('Invalid progress counter')
            if type(value['writer_at']) not in (int,float) or not 0 <= now-value['writer_at'] < float('inf'):
                raise ProtocolError('Invalid writer clock')
            if any(lease is not None for _,lease in parent._direct_intents.values()) and now-max(
                    value['writer_at'], getattr(parent, '_direct_writer_required_since', 0)) > 5:
                raise ProtocolError('Direct writer stalled')
            if value['blocks'] > previous.get('blocks', 0):
                if not 0 <= time.monotonic_ns()-value['produced_ns'] <= 5_000_000_000:
                    raise ProtocolError('Invalid PCM clock')
                parent._last_pcm = value['produced_ns']/1e9
                parent._has_pcm = True
                if parent.reconnect or (parent._prepared and parent._probe.ok):
                    parent._ready.set()
            parent.direct_progress = dict(previous, **value)
        parent._last_status = now
        return
    if parent.direct_runtime is None or set(value) != {'probe','metadata','bindings','connections','consumers','writer'}:
        raise ProtocolError('Invalid direct state')
    for field in ('connections','consumers','writer'):
        if not isinstance(value[field],dict) or set(value[field])-{str(ch) for ch in range(8)}:
            raise ProtocolError('Invalid output channels')
    if value['probe'] is not None:
        parent._probe = SourceTestResult.model_validate(value['probe'])
    meta=value['metadata']
    if meta is not None:
        if set(meta) != {'status','probe'}:
            raise ProtocolError('Invalid PCM metadata')
        parent._direct_source_metadata = InputSourceStatus(source_id=parent.source_id,**meta['status'])
    binding=value['bindings']
    if binding is not None:
        if set(binding) != {'sequence','channels'} or type(binding['sequence']) is not int or binding['sequence'] < 1:
            raise ProtocolError('Invalid PCM sequence')
        if not isinstance(binding['channels'],dict) or set(binding['channels'])-{str(ch) for ch in range(8)}:
            raise ProtocolError('Invalid metadata channels')
        eligible = any(type(lease) is int and lease > 0 and
            parent._direct_intents.get(int(ch),(0,None))[1] == lease for ch,lease in binding['channels'].items())
        status=getattr(parent,'_direct_source_metadata',None)
        if eligible and status is not None and binding['sequence'] > parent._direct_metadata_sequence:
            from .process_pipeline import METADATA
            for key in METADATA:
                parent._status[key]=getattr(status,key)
            parent._direct_metadata_sequence=binding['sequence']
    parent.direct_progress=dict(parent.direct_progress,
        consumers=value['consumers'],writer=value['writer'],connections=value['connections'])
