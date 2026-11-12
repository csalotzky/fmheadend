"""Experimental HTTP/LOCAL/PODCAST/ALSA/UDP/RTP child with inherited socketpairs.

Native lifecycle runs on a daemon thread. The control thread can always observe
parent EOF and exit without acquiring any native lock. No DB/TX/API ownership.
"""
import argparse
import os
import socket
import threading
import time

from ..models.configuration import InputSource, InputSourceConfig
from ..models.audio_profile_file import AudioProfileFile
from ..errors import SourceUnavailable, SourcePending
from .audio_ipc import (PacketQueue, ProtocolError, configure,
                        envelope, pack, receive)
from .output import PCM_CAPS


class CapturedPcm(bytes):
    """Worker-private bytes carrying the serialized sink metadata snapshot."""


class TransportOutput:
    def __init__(self, source_id, generation):
        self.source_id, self.generation = source_id, generation
        self.queue = PacketQueue()
        self.sequence = 0
        self.pipeline = None

    def capture(self, data):
        # Carry the serialized sink identity *through* native staging. A later
        # track's callback must not relabel an older queued PCM block at drain.
        result = CapturedPcm(data)
        result.status = self.pipeline.snapshot()
        result.produced_ns = time.monotonic_ns()
        return result

    def publish(self, data):
        # GstPipeline's existing staging/drain path calls this, never socket IO.
        self.sequence += 1
        payload = dict(produced_ns=getattr(data, 'produced_ns', time.monotonic_ns()), format=PCM_CAPS,
                       status=getattr(data, 'status', None) or self.pipeline.snapshot(),
                       probe=self.pipeline.probe_result().model_dump(mode='json'))
        self.queue.put(pack(envelope(self.source_id, self.generation,
                                    self.sequence, 'pcm', payload), data))

    def clear(self):
        self.queue.clear()

    def snapshot(self):
        return {}


def serve(control, pcm, pipeline_factory=None):
    """Also used by deterministic process tests with an injected decoder."""
    configure(control)
    configure(pcm)
    # CREATE has a finite deadline even if the parent disappears before sending.
    message, _ = receive(control)
    if message['kind'] != 'create' or message['sequence'] != 1:
        raise ProtocolError('Expected CREATE')
    source_id, generation = message['source_id'], message['generation']
    payload = message['payload']
    if set(payload) != {'source', 'profile', 'options'}:
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
    # Radio Browser replacement is deliberately unavailable in this PoC. Keep
    # the native HTTP reconnect policy, but do not create an unserviced URL port.
    source = source.model_copy(deep=True)
    source.config.pop('radio_browser_uuid', None)
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
                packet = output.queue.pop()
                if packet is None:
                    shutdown.wait(0.005)
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
    threading.Thread(target=send_pcm, name='audiod-pcm', daemon=True).start()
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
        status = dict(status=pipeline.snapshot(),
                             staging=pipeline.pcm_snapshot()['staging'],
                             fatal_error=pipeline.fatal_error)
        if catalog is not None:
            status['local_waiting'] = time.monotonic() < pipeline.local_wait_until
        event('status', status)
        try:
            command, _ = receive(control)
        except socket.timeout:
            continue
        if command['source_id'] != source_id or command['generation'] != generation:
            continue
        if command['sequence'] <= request:
            raise ProtocolError('Invalid lifecycle command')
        request = command['sequence']
        if command['kind'] == ('podcast_snapshot' if source.type == 'PODCAST' else 'local_response') and catalog is not None and not shutdown.is_set():
            catalog.accept(command['payload'])
            continue
        if command['kind'] != 'shutdown' or command['payload']:
            raise ProtocolError('Invalid lifecycle command')
        event('accepted', {'request': request})
        shutdown.set()
        # Parent owns the bounded grace/TERM/KILL deadline; no join here.


def main():
    parser = argparse.ArgumentParser(description='Private fmheadendd HTTP/LOCAL audio worker')
    parser.add_argument('--control-fd', type=int, required=True)
    parser.add_argument('--pcm-fd', type=int, required=True)
    args = parser.parse_args()
    code = 0
    try:
        with socket.socket(fileno=args.control_fd) as control, socket.socket(fileno=args.pcm_fd) as pcm:
            serve(control, pcm)
    except Exception:
        # No arbitrary native destructor/atexit calls after loss of ownership.
        code = 1
    os._exit(code)


if __name__ == '__main__':
    main()
