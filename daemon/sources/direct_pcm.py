"""Private direct-output opt-ins; no mux control or native calls here."""
import threading
import time

from ..errors import DependencyUnavailable, SourcePending
from ..mux.unix_client import MuxPcmWriter, UnixMuxClient
from ..models.configuration import SourceTestResult
from ..models.runtime_status import InputSourceStatus
from .audio_ipc import ProtocolError, PCM_MAX_AGE
from .output import PcmOutput, PCM_CAPS


def validate_assignment(payload):
    if (not isinstance(payload, dict) or set(payload) not in ({'revision', 'lease'}, {'channel', 'revision', 'lease'})
            or type(payload.get('channel', 0)) is not int or not 0 <= payload.get('channel', 0) < 8
            or type(payload['revision']) is not int or not 0 < payload['revision'] < 2**53
            or (payload['lease'] is not None and
                (type(payload['lease']) is not int or not 0 < payload['lease'] < 2**53))):
        raise ProtocolError('Invalid output assignment')
    return payload['revision'], payload['lease']


class _Writer(MuxPcmWriter):
    def __init__(self, output, runtime, owner):
        super().__init__(lambda _: output, runtime)
        self.owner = owner
        self.serviced = time.monotonic()

    def _drain(self):
        self.serviced = time.monotonic()
        return super()._drain()

    def _connect(self, channel):
        try:
            sock = super()._connect(channel)
        except OSError:
            self._connection(channel, False)
            raise
        self._connection(channel, True)
        return sock

    def _disconnect(self, producer, now, error):
        changed = producer.sock is not None or self.last_error != str(error)
        super()._disconnect(producer, now, error)
        if changed:
            with self.owner.lock:
                for channel, current in self.producers.items():
                    if current is producer:
                        self.owner.connections[str(channel)] = False
                self.owner.dirty = True

    def _connection(self, channel, value):
        with self.owner.lock:
            if self.owner.connections.get(str(channel)) != value:
                self.owner.connections[str(channel)] = value
                self.owner.dirty = True

    def frame_sent(self, channel, data):
        with self.owner.lock:
            old = self.owner.rendered.get(str(channel))
            if old is not None and old['lease'] == self.owner.leases[channel] and old['status'] is data.status and old['probe'] is data.probe:
                return
            self.owner.dirty = True
            self.owner.rendered[str(channel)] = dict(status=data.status, probe=data.probe,
                lease=self.owner.leases[channel], sequence=data.sequence)


class DirectOutput:
    def __init__(self, source_id, runtime, consumer_ms):
        self.pipeline = None
        self.output = PcmOutput(consumer_ms)
        self.lock = threading.Lock()
        self.writer = _Writer(self.output, runtime, self)
        self.source_id = source_id
        self.revisions = [0]*8
        self.leases = [None]*8
        self.blocks = self.bytes = self.produced_ns = 0
        self.rendered = {}
        self.connections = {}
        self.probe = None
        self.dirty = True
        self._capture_key = object()
        self._captured = None
        self._sent_render_identity = None
        self._sent_probe = None
        self._sink_key = object()
        self._sink_status = None

    def capture(self, data):
        """Sink identity/time travels through native staging, as in PCM1."""
        from .audiod import CapturedPcm
        key = self.pipeline.transport_revision
        if key != self._sink_key:
            self._sink_status = self.pipeline.transport_snapshot()
            self._sink_key = key
        block = CapturedPcm(data)
        block.status = self._sink_status
        block.produced_ns = time.monotonic_ns()
        return block

    def publish(self, data):
        from .audiod import CapturedPcm
        produced = getattr(data, 'produced_ns', time.monotonic_ns())
        if not data or len(data) % 4 or not 0 <= time.monotonic_ns()-produced <= PCM_MAX_AGE*1e9:
            return
        block = CapturedPcm(data)
        block.produced_ns = produced
        captured = getattr(data, 'status', None)
        key = ('sink', id(captured)) if captured is not None else getattr(self.pipeline, 'transport_revision', None)
        if key is None or key != self._capture_key:
            status = getattr(data, 'status', None) or getattr(self.pipeline, 'transport_snapshot', self.pipeline.snapshot)()
            probe = getattr(self.pipeline, 'transport_probe', None)
            result = probe(status) if probe else self.pipeline.probe_result().model_dump(mode='json')
            with self.lock:
                if result != self.probe:
                    self.dirty = True
            self._capture_key, self._captured = key, (status, result)
        block.status, block.probe = self._captured
        with self.lock:
            self.blocks += 1
            self.bytes += len(block)
            self.produced_ns = produced
            self.probe = block.probe
            block.sequence = self.blocks
        self.output.publish(block)

    @property
    def lease(self):
        return self.leases[0]

    @lease.setter
    def lease(self, value):
        self.leases[0] = value

    def assign(self, payload):
        revision, lease = validate_assignment(payload)
        channel = payload.get('channel', 0)
        if revision <= self.revisions[channel]:
            raise ProtocolError('Stale output revision')
        self.revoke(channel)
        self.revisions[channel] = revision
        with self.lock:
            self.leases[channel] = lease
        if lease is not None:
            self.output.attach(channel)
            self.writer.attach(channel, self.source_id)

    def revoke(self, channel=None):
        # Serialized control caller; writer lock fences all sends/reconnects.
        for ch in range(8) if channel is None else (channel,):
            self.writer.detach(ch)
            self.output.detach(ch)
            with self.lock:
                self.leases[ch] = None
                self.rendered.pop(str(ch), None)
                self.connections.pop(str(ch), None)
                self.dirty = True

    def clear(self):
        self.output.clear()

    def snapshot(self):
        return self.output.snapshot()

    def heartbeat(self):
        with self.lock:
            return dict(blocks=self.blocks, bytes=self.bytes, produced_ns=self.produced_ns,
                        writer_at=self.writer.serviced)

    def changes(self, force=False):
        with self.lock:
            if not self.dirty and not force:
                return None
            self.dirty = False
            newest = max(self.rendered.values(), key=lambda x:x["sequence"], default=None)
            identity = None if newest is None else (newest['status'], newest['probe'])
            changed = identity is not None and (force or self._sent_render_identity is None or
                identity[0] is not self._sent_render_identity[0] or identity[1] is not self._sent_render_identity[1])
            metadata = dict(status=newest['status'], probe=newest['probe']) if changed else None
            if identity is not None:
                self._sent_render_identity = identity
            bindings = None if newest is None else dict(sequence=newest['sequence'],
                channels={ch:v['lease'] for ch,v in self.rendered.items() if v['status'] is newest['status']})
            probe = self.probe if force or self.probe is not self._sent_probe else None
            self._sent_probe = self.probe
            result = dict(probe=probe, metadata=metadata, bindings=bindings, connections=dict(self.connections))
        result.update(consumers=self.output.snapshot(), writer=self.writer.pcm_snapshot())
        return result

    def progress(self):
        with self.lock:
            result = dict(blocks=self.blocks, bytes=self.bytes, produced_ns=self.produced_ns,
                          probe=self.probe, rendered=dict(self.rendered))
        result.update(consumers=self.output.snapshot(), writer=self.writer.pcm_snapshot(),
                      writer_at=self.writer.serviced, writer_error=self.writer.last_error)
        return result


def accept_progress(parent, p):
    if set(p) != {'blocks', 'bytes', 'produced_ns', 'probe', 'rendered', 'consumers',
                  'writer', 'writer_at', 'writer_error'}:
        raise ProtocolError('Invalid direct progress')
    for key in ('blocks', 'bytes', 'produced_ns'):
        if type(p[key]) is not int or not 0 <= p[key] < 2**63 or p[key] < parent.direct_progress.get(key, 0):
            raise ProtocolError('Invalid PCM progress counter')
    now = time.monotonic()
    if p['blocks'] > parent.direct_progress.get('blocks', 0):
        if not 0 <= time.monotonic_ns()-p['produced_ns'] <= 5_000_000_000:
            raise ProtocolError('Invalid PCM progress timestamp')
        parent._last_pcm = p['produced_ns']/1e9
        parent._probe = SourceTestResult.model_validate(p['probe'])
        parent._has_pcm = True
        parent._ready.set()
    rendered = p['rendered']
    if not isinstance(rendered, dict) or set(rendered)-{str(ch) for ch in range(8)}:
        raise ProtocolError('Invalid rendered channels')
    for channel, value in rendered.items():
        if set(value) != {'lease', 'status', 'probe', 'sequence'} or type(value['sequence']) is not int:
            raise ProtocolError('Invalid rendered metadata')
        intent = parent._direct_intents.get(int(channel), (0, None))
        if value['lease'] == intent[1] and value['lease'] is not None and value['sequence'] > getattr(parent, '_direct_metadata_sequence', 0):
            from .process_pipeline import METADATA
            status = InputSourceStatus(source_id=parent.source_id, **value['status'])
            for key in METADATA:
                parent._status[key] = getattr(status, key)
            parent._direct_metadata_sequence = value['sequence']
    if type(p['writer_at']) not in (float, int) or not 0 <= now-p['writer_at'] < float('inf'):
        raise ProtocolError('Invalid writer timestamp')
    if any(lease is not None for _, lease in parent._direct_intents.values()) and now-max(
            p['writer_at'], getattr(parent, '_direct_writer_required_since', 0)) > 5:
        raise ProtocolError('Direct writer stalled')
    parent.direct_progress = p


class DirectHttpFactory:
    mocked = False

    def __init__(self, runtime, *, local=False, podcast=False, alsa=False, network=False):
        self.runtime = str(runtime)
        self.local = local
        self.podcast = podcast
        self.alsa = alsa
        self.network = network

    def __call__(self, source, **options):
        from .process_pipeline import ProcessPipeline
        allowed = (('HTTP',) + (('LOCAL',) if self.local else ()) +
                   (('PODCAST',) if self.podcast else ()) + (('ALSA',) if self.alsa else ()) +
                   (('UDP', 'RTP') if self.network else ()))
        if source.type not in allowed:
            raise DependencyUnavailable('Source not enabled in direct prototype')
        return ProcessPipeline(source, direct_runtime=self.runtime, **options)

    @staticmethod
    def validate_profile(profile):
        from .gst_pipeline import GstPipeline
        return GstPipeline.validate_profile(profile)


class DirectMux(UnixMuxClient):
    """Control-thread-owned independent channel ledgers; inherited control only."""
    def __init__(self, sources, runtime, clock=time.monotonic):
        super().__init__(sources.output_for, runtime)
        from types import SimpleNamespace
        self.sources, self.clock = sources, clock
        self.channels = [SimpleNamespace(desired=None, owner=None, revision=0, revoking=None) for _ in range(8)]

    @property
    def owner(self):
        return self.channels[0].owner

    def attach(self, channel, source_id):
        if type(channel) is not int or not 0 <= channel < 8:
            raise DependencyUnavailable('Direct PCM channel must be 0..7')
        entry = self.channels[channel]
        entry.desired = source_id
        self.service()
        if entry.owner is not self.sources.pipelines.get(source_id) or entry.revoking:
            raise SourcePending('Direct PCM ownership transfer pending')

    def detach(self, channel):
        if type(channel) is not int or not 0 <= channel < 8:
            raise ValueError('Direct PCM channel must be 0..7')
        self.channels[channel].desired = None
        self.service()

    def service(self):
        for channel, entry in enumerate(self.channels):
            self._service_channel(channel, entry)

    def _service_channel(self, channel, entry):
        candidate = self.sources.pipelines.get(entry.desired)
        if entry.owner is not None and (entry.revoking is not None or entry.owner is not candidate or entry.owner._stop.is_set()):
            if entry.revoking is None:
                entry.revision += 1
                entry.owner.set_output_lease(entry.revision, None, channel)
                entry.revoking = (entry.revision, self.clock())
            revision, since = entry.revoking
            applied = entry.owner.direct_applied if channel == 0 else entry.owner.direct_applied_channels.get(channel)
            if entry.owner._done.is_set() or applied == (revision, None):
                entry.owner = None
                entry.revoking = None
            elif self.clock()-since >= 1:
                try:
                    entry.owner.stop()
                except DependencyUnavailable:
                    pass
                return  # Timeout is never permission to grant.
            else:
                return
        if entry.owner is None and candidate is not None and not candidate._stop.is_set():
            if candidate.direct_runtime is None:
                raise DependencyUnavailable('Direct mux requires a direct backend')
            entry.revision += 1
            candidate.set_output_lease(entry.revision, entry.revision, channel)
            entry.owner = candidate

    def pcm_snapshot(self):
        return {ch: entry.owner.direct_progress['writer'][str(ch)]
            for ch, entry in enumerate(self.channels) if entry.owner is not None
            and str(ch) in entry.owner.direct_progress.get('writer', {})}

    def close(self):
        for entry in self.channels:
            entry.desired = None
        self.service()
        for owner in {e.owner for e in self.channels if e.owner is not None}:
            try:
                owner.stop()
            except DependencyUnavailable:
                pass
        super().close()
