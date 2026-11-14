"""Experimental HTTP/LOCAL/PODCAST/ALSA/UDP/RTP child with inherited socketpairs.

Native lifecycle runs on a daemon thread. The control thread can always observe
parent EOF and exit without acquiring any native lock. No DB/TX/API ownership.
"""
import argparse
import os
import socket
import select
import threading
import time

from ..models.configuration import InputSource, InputSourceConfig
from ..models.audio_profile_file import AudioProfileFile
from ..errors import SourceUnavailable, SourcePending
from .audio_ipc import (IO_TIMEOUT, PacketQueue, PcmEncoder, ProtocolError, configure,
                        envelope, pack, receive)
from .output import PCM_CAPS


class CapturedPcm(bytes):
    """Worker-private bytes carrying the serialized sink metadata snapshot."""


class TransportOutput:
    def __init__(self, source_id, generation):
        self.source_id, self.generation = source_id, generation
        self.queue = PacketQueue()
        self.sequence = 0
        self.encoder = PcmEncoder(source_id, generation)
        self.pipeline = None

    def capture(self, data):
        # Carry the serialized sink identity *through* native staging. A later
        # track's callback must not relabel an older queued PCM block at drain.
        result = CapturedPcm(data)
        result.status = self._snapshot()
        result.produced_ns = time.monotonic_ns()
        return result

    def publish(self, data):
        # GstPipeline's existing staging/drain path calls this, never socket IO.
        self.sequence += 1
        produced = getattr(data, 'produced_ns', None)
        if produced is None:
            produced = time.monotonic_ns()
        status = getattr(data, 'status', None) or self._snapshot()
        cached_probe = getattr(self.pipeline, 'transport_probe', None)
        probe = (cached_probe(status) if cached_probe is not None else
                 self.pipeline.probe_result().model_dump(mode='json'))
        self.queue.put(self.encoder.pack(self.sequence, produced,
            status, probe, data))

    def clear(self):
        self.queue.clear()

    def _snapshot(self):
        snapshot = getattr(self.pipeline, 'transport_snapshot', self.pipeline.snapshot)
        return snapshot()

    def snapshot(self):
        return {}


def serve(control, pcm, pipeline_factory=None):
    """Also used by deterministic process tests with an injected decoder."""
    configure(control)
    if pcm is not None:
        configure(pcm)
    # CREATE has a finite deadline even if the parent disappears before sending.
    message, _ = receive(control)
    if message['kind'] != 'create' or message['sequence'] != 1:
        raise ProtocolError('Expected CREATE')
    source_id, generation = message['source_id'], message['generation']
    payload = message['payload']
    direct_runtime = payload.get('direct_runtime')
    telemetry_configured = payload.get('telemetry_configured', False)
    if type(telemetry_configured) is not bool:
        raise ProtocolError('Invalid telemetry configuration')
    if set(payload) != ({'source', 'profile', 'options'} | ({'direct_runtime'} if direct_runtime is not None else set()) | ({'telemetry_configured'} if 'telemetry_configured' in payload else set())):
        raise ProtocolError('Invalid CREATE payload')
    raw = payload['source']
    source = (InputSource if 'id' in raw else InputSourceConfig).model_validate(raw)
    if source.type not in ('HTTP', 'LOCAL', 'PODCAST', 'ALSA', 'UDP', 'RTP') or getattr(source, 'id', 0) != source_id:
        raise ProtocolError('Worker only supports its assigned HTTP/LOCAL/PODCAST/ALSA/UDP/RTP source')
    profile = AudioProfileFile.model_validate(payload['profile']) if payload['profile'] else None
    options = payload['options']
    if not isinstance(options, dict) or set(options) != {
            'timeout_s', 'reconnect', 'pcm_staging_buffer_ms',
            'pcm_staging_max_age_ms', 'pcm_consumer_buffer_ms'}:
        raise ProtocolError('Invalid pipeline options')
    if (type(options['reconnect']) is not bool
            or type(options['timeout_s']) not in (int, float)
            or not 1 <= options['timeout_s'] <= 30
            or any(type(options[key]) is not int or not 1 <= options[key] <= 10000
                   for key in ('pcm_staging_buffer_ms', 'pcm_staging_max_age_ms', 'pcm_consumer_buffer_ms'))):
        raise ProtocolError('Pipeline options outside PoC bounds')
    if pipeline_factory is None:
        from .gst_pipeline import GstPipeline
        pipeline_factory = GstPipeline
    # Child owns native retries; directory lookup and persistence stay parent-owned.
    source = source.model_copy(deep=True)
    if direct_runtime is not None:
        if source.type not in ("HTTP", "LOCAL", "PODCAST", "ALSA", "UDP", "RTP") or pcm is not None or not isinstance(direct_runtime, str) or not 0 < len(direct_runtime) < 100:
            raise ProtocolError("Invalid direct source configuration")
        from .direct_pcm import DirectOutput
        output = DirectOutput(source_id, direct_runtime, options["pcm_consumer_buffer_ms"])
    else:
        if pcm is None:
            raise ProtocolError("Missing PCM transport")
        output = TransportOutput(source_id, generation)
    catalog = None
    if source.type == 'LOCAL':
        from .local_worker import WorkerCatalog
        catalog = WorkerCatalog()
        options = dict(options, local_playback_factory=catalog.playback)
    if source.type == 'PODCAST':
        from .podcast_worker import WorkerPodcast
        from ..models.configuration import PodcastConfig
        catalog = WorkerPodcast(PodcastConfig.model_validate(source.config))
        options = dict(options, podcast_runtime_factory=catalog.playback)
    if telemetry_configured:
        options = dict(options, telemetry_configured=True)
    pipeline = pipeline_factory(source, profile=profile, output=output, **options)
    output.pipeline = pipeline
    prepared = threading.Event()
    shutdown = threading.Event()
    completed = threading.Event()
    transport_failed = threading.Event()
    failure = []

    def lifecycle():
        try:
            try:
                pipeline.start()
            except SourcePending:
                if source.type not in ('LOCAL', 'PODCAST'):
                    raise
            prepared.set()
            shutdown.wait()
            pipeline.stop()
        except Exception as exc:
            failure.append(dict(error=str(exc)[:4096],
                                error_scope='SOURCE' if isinstance(exc, SourceUnavailable) else 'INTERNAL'))
        finally:
            completed.set()

    def send_pcm():
        try:
            while not shutdown.is_set():
                packet = output.queue.pop(timeout=IO_TIMEOUT)
                if packet is None:
                    continue
                else:
                    # Partial send timeout closes the entire generation.
                    pcm.sendall(packet)
        except (OSError, ValueError):
            transport_failed.set()

    sequence = 0

    def event(kind, data):
        nonlocal sequence
        sequence += 1
        control.sendall(pack(envelope(source_id, generation, sequence, kind, data)))

    event('accepted', {'request': 1})
    threading.Thread(target=lifecycle, name='audiod-native', daemon=True).start()
    if pcm is not None:
        threading.Thread(target=send_pcm, name='audiod-pcm', daemon=True).start()
    from .worker_reporting import WorkerReporter
    reporter = WorkerReporter(pipeline, output if direct_runtime is not None else None, catalog, event)
    announced = False
    request = 1
    while True:
        if transport_failed.is_set():
            raise ProtocolError('PCM transport stalled or disconnected')
        if completed.is_set():
            event('terminated', failure[0] if failure else dict(error=None, error_scope='SOURCE'))
            return
        if prepared.is_set() and not announced:
            event('prepared', {'request': 1})
            announced = True
        if catalog is not None:
            # Bounded local requests; callbacks only enqueue, never send/wait.
            for _ in range(3):
                data = catalog.poll()
                if data is None:
                    break
                event('podcast_played' if source.type == 'PODCAST' else 'local_request', data)
        reporter.poll()
        try:
            # Fast subscription cadence must not shorten the framed-record
            # timeout: a partially received command retains IO_TIMEOUT.
            enabled, hz = getattr(pipeline, '_telemetry_request', (False, 50.0))
            if not select.select([control], [], [], min(IO_TIMEOUT, 1/hz) if enabled else IO_TIMEOUT)[0]:
                continue
            command, _ = receive(control)
        except socket.timeout:
            continue
        if command['source_id'] != source_id or command['generation'] != generation:
            continue
        if command['sequence'] <= request:
            raise ProtocolError('Invalid lifecycle command')
        request = command['sequence']
        if command['kind'] == 'radio_command' and not shutdown.is_set():
            from .worker_services import radio_command
            radio_command(pipeline, command['payload'])
            continue
        if command['kind'] == 'telemetry_request' and not shutdown.is_set():
            value = command['payload']
            from ..config import audio_telemetry_hz
            if (set(value) != {'revision','enabled','hz'} or type(value['revision']) is not int
                    or value['revision'] < 0 or type(value['enabled']) is not bool):
                raise ProtocolError('Invalid telemetry request')
            hz = audio_telemetry_hz(value['hz'])
            pipeline._worker_telemetry_revision = value['revision']
            if hasattr(pipeline, 'request_telemetry'):
                pipeline.request_telemetry(value['enabled'], hz)
            continue
        if command['kind'] == 'state_request' and not command['payload'] and not shutdown.is_set():
            reporter.poll(force=True)
            continue
        if command['kind'] == 'output_lease' and direct_runtime is not None and not shutdown.is_set():
            output.assign(command['payload'])
            event('output_applied', command['payload'])
            continue
        if command['kind'] == ('podcast_snapshot' if source.type == 'PODCAST' else 'local_response') and catalog is not None and not shutdown.is_set():
            catalog.accept(command['payload'])
            continue
        if command['kind'] != 'shutdown' or command['payload']:
            raise ProtocolError('Invalid lifecycle command')
        event('accepted', {'request': request})
        if direct_runtime is not None:
            output.revoke()
        shutdown.set()
        # Parent owns the bounded grace/TERM/KILL deadline; no join here.


def main():
    parser = argparse.ArgumentParser(description='Private fmheadendd HTTP/LOCAL audio worker')
    parser.add_argument('--control-fd', type=int, required=True)
    parser.add_argument('--pcm-fd', type=int)
    args = parser.parse_args()
    code = 0
    try:
        from contextlib import nullcontext
        with socket.socket(fileno=args.control_fd) as control, (socket.socket(fileno=args.pcm_fd) if args.pcm_fd is not None else nullcontext()) as pcm:
            serve(control, pcm)
    except Exception:
        # No arbitrary native destructor/atexit calls after loss of ownership.
        code = 1
    os._exit(code)


if __name__ == '__main__':
    main()
