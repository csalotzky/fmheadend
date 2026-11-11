"""Portable wire/cache tests and explicitly Linux-only real child-process tests."""
import os
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from fmheadendd.errors import DependencyUnavailable, SourcePending
from fmheadendd.models.configuration import InputSourceConfig, SourceTestResult
from fmheadendd.sources import audio_ipc as ipc, process_pipeline as backend
from fmheadendd.sources.output import PCM_CAPS, PcmOutput
from fmheadendd.sources.pipeline import MediaPipeline
from fmheadendd.tests.audiod_fixture import PCM

LINUX = pytest.mark.skipif(sys.platform != 'linux', reason='Linux pass_fds/socketpair process integration')


def source(name='fixture'):
    return InputSourceConfig(name=name, type='HTTP', config={'uri': 'http://127.0.0.1:9/fixture.wav'})


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail('Condition did not complete within deadline')


def finish(pipeline):
    try:
        pipeline.stop()
    except DependencyUnavailable:
        pass
    assert pipeline._done.wait(3)
    pipeline.stop()
    assert pipeline.process is None or pipeline.process.poll() is not None


@pytest.fixture
def adapter(monkeypatch):
    # Cache/validation only: no process is launched with the platform override.
    monkeypatch.setattr(backend, 'sys', SimpleNamespace(platform='linux', executable=sys.executable))
    return backend.ProcessPipeline(source())


def pcm_message(adapter, sequence=1, **changes):
    payload = dict(produced_ns=time.monotonic_ns(), format=PCM_CAPS,
                   status=dict(state='RUNNING', stream_title='rendered', output_caps=PCM_CAPS),
                   probe=SourceTestResult(ok=True, mocked=False).model_dump(mode='json'))
    payload.update(changes)
    return ipc.envelope(adapter.source_id, adapter.generation, sequence, 'pcm', payload)


def test_wire_fragmentation_and_coalescing():
    msg = ipc.envelope(1, 'a' * 32, 1, 'accepted', {'request': 1})
    packet = ipc.pack(msg)
    a, b = socket.socketpair()
    ipc.configure(a)
    ipc.configure(b)
    def send():
        for byte in packet[:12]:
            a.sendall(bytes([byte]))
        a.sendall(packet[12:] + packet)
    thread = threading.Thread(target=send)
    try:
        thread.start()
        assert ipc.receive(b) == (msg, b'')
        assert ipc.receive(b) == (msg, b'')
    finally:
        thread.join(1)
        a.close()
        b.close()


@pytest.mark.parametrize('change', [dict(version=2), dict(sequence=True), dict(sequence=0),
    dict(source_id=-1), dict(generation='old'), dict(kind='seek'), dict(payload=[]), dict(extra=True)])
def test_invalid_envelope(change):
    msg = ipc.envelope(1, 'a' * 32, 1, 'accepted') | change
    with pytest.raises(ipc.ProtocolError):
        ipc.pack(msg)


@pytest.mark.parametrize('data', [b'x', b'x' * (ipc.MAX_PCM + 4)], ids=['unaligned', 'oversized'])
def test_invalid_pcm_size(data):
    with pytest.raises(ipc.ProtocolError):
        ipc.pack(ipc.envelope(1, 'a' * 32, 1, 'pcm'), data)


def test_partial_record_deadline_and_oversize_header():
    a, b = socket.socketpair()
    ipc.configure(b)
    try:
        a.sendall(b'\0')
        with pytest.raises(ipc.ProtocolError, match='Partial'):
            ipc.receive(b)
    finally:
        a.close()
        b.close()
    a, b = socket.socketpair()
    try:
        a.sendall(struct.pack('!I', ipc.MAX_RECORD + 1))
        with pytest.raises(ipc.ProtocolError, match='limit'):
            ipc.receive(b)
    finally:
        a.close()
        b.close()


def test_backpressure_queue_and_stale_drop(monkeypatch):
    queue = ipc.PacketQueue()
    for n in range(100):
        queue.put(bytes([n]))
    assert len(queue.items) == 4 and queue.dropped == 96
    assert queue.pop() == bytes([96])
    now = time.monotonic()
    monkeypatch.setattr(ipc.time, 'monotonic', lambda: now + 1)
    assert queue.pop() is None and queue.dropped == 99


def test_blocked_socket_send_is_bounded():
    a, b = socket.socketpair()
    ipc.configure(a)
    start = time.monotonic()
    try:
        with pytest.raises(socket.timeout):
            for _ in range(128):
                a.sendall(b'x' * (1024 * 1024))
        assert time.monotonic() - start < 3
    finally:
        a.close()
        b.close()


def test_cached_contract_and_metadata_order(adapter):
    assert isinstance(adapter, MediaPipeline)
    adapter.output.attach(0)
    status = ipc.envelope(0, adapter.generation, 1, 'status',
        dict(status=dict(state='BUFFERING', stream_title='premature'), staging={}, fatal_error=None))
    adapter._accept('control', status)
    assert not adapter.snapshot().get('stream_title')
    adapter._accept('pcm', pcm_message(adapter), PCM)
    assert adapter.output.read(0) == PCM
    assert adapter.snapshot()['stream_title'] == 'rendered'
    cached = adapter.snapshot()
    cached['stream_title'] = 'mutated'
    assert adapter.snapshot()['stream_title'] == 'rendered'
    adapter.stop()
    adapter._accept('pcm', pcm_message(adapter, 2), PCM)
    assert adapter.output.read(0) is None


@pytest.mark.parametrize('mutation', ['generation', 'source_id', 'sequence'])
def test_stale_generation_source_and_duplicate_rejected(adapter, mutation):
    adapter.output.attach(0)
    adapter._accept('pcm', pcm_message(adapter), PCM)
    assert adapter.output.read(0) == PCM
    message = pcm_message(adapter, 2)
    message[mutation] = {'generation': 'f' * 32, 'source_id': 999, 'sequence': 1}[mutation]
    adapter._accept('pcm', message, PCM)
    assert adapter.output.read(0) is None and adapter.stale_messages == 1


def test_stale_pcm_and_format_rejected(adapter):
    adapter.output.attach(0)
    adapter._accept('pcm', pcm_message(adapter, produced_ns=time.monotonic_ns() - 1_000_000_000), PCM)
    assert adapter.stale_pcm == 1 and adapter.output.read(0) is None
    with pytest.raises(ipc.ProtocolError, match='format'):
        adapter._accept('pcm', pcm_message(adapter, 2, format='F32LE'), PCM)


def test_native_failure_scope_survives_disconnect(adapter):
    adapter.output.attach(0)
    adapter._accept('pcm', pcm_message(adapter), PCM)
    adapter._accept('control', ipc.envelope(0, adapter.generation, 1, 'terminated',
        dict(error='HTTP source unavailable', error_scope='SOURCE')))
    adapter._fail('Audio channel closed')
    assert adapter.snapshot() == dict(state='ERROR', last_error='HTTP source unavailable', error_scope='SOURCE')
    assert adapter.output.read(0) is None
    assert not adapter.probe_result().ok


def test_huge_wire_timestamp_is_dropped_without_killing_reader(adapter):
    adapter.output.attach(0)
    a, b = socket.socketpair()
    ipc.configure(a)
    ipc.configure(b)
    try:
        a.sendall(ipc.pack(pcm_message(adapter, produced_ns=10**400), PCM))
        adapter._accept('pcm', *ipc.receive(b))
        assert adapter.stale_pcm == 1
        assert adapter.output.read(0) is None
        adapter._accept('pcm', pcm_message(adapter, 2), PCM)
        assert adapter.output.read(0) == PCM
    finally:
        a.close()
        b.close()


def test_owner_thread_start_failure_is_terminal_and_cleanable(adapter, monkeypatch):
    def fail_start(thread):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with pytest.raises(DependencyUnavailable, match='owner thread'):
        adapter.start()
    assert adapter.process is None and adapter._done.is_set()
    assert adapter.snapshot()['state'] == 'ERROR'
    adapter.stop()
    with pytest.raises(DependencyUnavailable):
        adapter.start()


def test_receiver_thread_start_failure_reaps_and_completes(simulated_process, monkeypatch):
    start = threading.Thread.start
    def fail_receiver(thread):
        if thread.name.startswith('audiod-receive-'):
            raise RuntimeError("can't start new thread")
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_receiver)
    pipeline = backend.ProcessPipeline(source())
    try:
        try:
            pipeline.start()
        except DependencyUnavailable:
            pass
        assert pipeline._done.wait(3)
        pipeline._thread.join(1)
        assert not pipeline._thread.is_alive()
        assert pipeline.process.poll() is not None
        assert pipeline.fatal_error == "can't start new thread"
        pipeline.stop()
    finally:
        pipeline._stop.set()


def test_output_identity_and_default_backend(adapter, monkeypatch):
    from fmheadendd.app import argument_parser, create_app
    from fmheadendd.sources.gst_pipeline import GstPipeline
    import inspect
    output = PcmOutput()
    replacement = backend.ProcessPipeline(source(), output=output)
    assert replacement.output is output
    from fmheadendd.sources.pipeline import MockPipeline
    assert inspect.signature(create_app).parameters['pipeline_factory'].default is MockPipeline
    assert not argument_parser().parse_args([]).experimental_http_worker
    from fmheadendd import app
    import uvicorn
    calls = []
    monkeypatch.setattr(sys, 'argv', ['fmheadendd', '--mock-controller', '--mock-mux'])
    monkeypatch.setattr(app, 'create_app', lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(uvicorn, 'run', lambda *a, **kw: None)
    app.main()
    assert calls[0]['pipeline_factory'] is GstPipeline


def test_factory_routes_only_http(monkeypatch):
    from fmheadendd.sources import gst_pipeline
    calls = []
    monkeypatch.setattr(backend, 'ProcessPipeline', lambda s, **kw: calls.append(('worker', s, kw)))
    monkeypatch.setattr(gst_pipeline, 'GstPipeline', lambda s, **kw: calls.append(('gst', s, kw)))
    factory = backend.ExperimentalHttpFactory()
    factory(source(), output='stable')
    local = InputSourceConfig(name='local', type='LOCAL', config={'mode': 'folder', 'path': '/tmp'})
    factory(local, output='stable')
    assert [c[0] for c in calls] == ['worker', 'gst']
    assert all(c[2] == {'output': 'stable'} for c in calls)


class FixturePipeline(backend.ProcessPipeline):
    worker_module = 'fmheadendd.tests.audiod_fixture'


@pytest.fixture
def simulated_process(monkeypatch):
    """Real socket IO and worker service, simulated process handle on Windows.

    This is intentionally NOT evidence of process isolation or native playback.
    """
    import subprocess
    from fmheadendd.sources.audiod import serve
    from fmheadendd.tests.audiod_fixture import FakeDecoder
    endpoints = {}
    children = []

    def pair(*args):
        a, b = socket.socketpair()
        endpoints[b.fileno()] = b
        return a, b

    class Child:
        def __init__(self, command, *, pass_fds, close_fds, stdin):
            assert command[:2] == [sys.executable, '-m'] and close_fds
            self.pid = 10000 + len(children)
            self.returncode = None
            self.done = threading.Event()
            self.decoders = []
            self.sockets = [endpoints[fd].dup() for fd in pass_fds]
            children.append(self)

            def factory(*args, **kw):
                decoder = FakeDecoder(*args, **kw)
                self.decoders.append(decoder)
                return decoder

            def run():
                try:
                    serve(*self.sockets, factory)
                    self.returncode = 0
                except Exception:
                    self.returncode = 1
                finally:
                    for s in self.sockets:
                        s.close()
                    self.done.set()
            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()

        def poll(self):
            return self.returncode if self.done.is_set() else None

        def wait(self, timeout):
            if not self.done.wait(timeout):
                raise subprocess.TimeoutExpired('simulated-child', timeout)
            return self.returncode

        def terminate(self):
            for decoder in self.decoders:
                decoder.closed.set()
            for s in self.sockets:
                s.close()
            self.returncode = -15
            self.done.set()

        kill = terminate

    monkeypatch.setattr(backend, 'sys', SimpleNamespace(platform='linux', executable=sys.executable))
    monkeypatch.setattr(backend, 'socket', SimpleNamespace(socketpair=pair, AF_UNIX='simulated-unix',
        SOCK_STREAM=socket.SOCK_STREAM, timeout=socket.timeout))
    monkeypatch.setattr(backend, 'subprocess', SimpleNamespace(Popen=Child, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired))
    yield children
    for child in children:
        child.terminate()
        child.thread.join(1)


@pytest.mark.parametrize('name', ['fixture', 'hang-start', 'hang-stop'])
def test_simulated_process_stream_and_nonblocking_stop(simulated_process, name):
    pipeline = backend.ProcessPipeline(source(name))
    pipeline.output.attach(0)
    try:
        with pytest.raises(SourcePending):
            pipeline.start()
        if name != 'hang-start':
            assert until(lambda: pipeline.output.read(0)) == PCM
            until(lambda: not pipeline.startup_pending)
            pipeline.check_startup()
        start = time.monotonic()
        with pytest.raises(DependencyUnavailable, match='pending'):
            pipeline.stop()
        assert time.monotonic() - start < 0.15
    finally:
        finish(pipeline)


def test_simulated_manager_sharing_replacement_and_exit(simulated_process, tmp_path):
    from fmheadendd.database import Database
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.tests.profile_support import profile_store
    db = Database(tmp_path / 'simulated.db')
    first, second = (db.save('sources', source(name)) for name in ('first', 'second'))
    manager = SourceManager(db, pipeline_factory=backend.ProcessPipeline, profile_files=profile_store(tmp_path))
    workers = []
    try:
        for sid, tx in ((first.id, 0), (first.id, 1), (second.id, 2)):
            try:
                manager.acquire(sid, tx)
            except SourcePending:
                pass
        a, b = manager.pipelines[first.id], manager.pipelines[second.id]
        workers.extend((a, b))
        output = a.output
        assert manager.consumers[first.id] == {0, 1}
        assert until(lambda: output.read(0)) == PCM
        assert until(lambda: output.read(1)) == PCM
        assert until(lambda: b.output.read(2)) == PCM
        assert len(simulated_process) == 2
        # Saved/apply retains old ownership while reap is pending.
        assert 'pending' in manager._restart_audio(first)
        assert manager.pipelines[first.id] is a
        assert a._done.wait(3)
        assert manager._restart_audio(first) == 'HTTP audio worker preparation pending'
        replacement = manager.pipelines[first.id]
        workers.append(replacement)
        assert replacement.output is output
        assert until(lambda: output.read(0)) == PCM
        replacement.process.terminate()
        assert replacement._done.wait(3)
        assert replacement.fatal_error
        assert not replacement.snapshot().get('stream_title')
        assert until(lambda: b.output.read(2)) == PCM
    finally:
        for pipeline in workers:
            finish(pipeline)
        manager.close()
        db.close()


def test_simulated_inactive_probe_waits_for_pcm_and_reaps(simulated_process):
    pipeline = backend.ProcessPipeline(source(), reconnect=False, timeout_s=1)
    try:
        pipeline.start()
        assert pipeline.probe_result().ok
        pipeline.stop()
        assert pipeline._done.is_set()
        assert pipeline.process.poll() is not None
    finally:
        finish(pipeline)


def test_simulated_native_failure_event_precedes_eof(simulated_process):
    pipeline = backend.ProcessPipeline(source('source-error'))
    try:
        try:
            pipeline.start()
        except SourcePending:
            pass
        assert pipeline._done.wait(3)
        assert pipeline.snapshot()['error_scope'] == 'SOURCE'
        assert pipeline.fatal_error == 'Fixture HTTP error'
    finally:
        finish(pipeline)


@LINUX
@pytest.mark.parametrize('name', ['fixture', 'hang-stop', 'hang-start'])
def test_real_process_bounded_start_stop_reap(name):
    pipeline = FixturePipeline(source(name), timeout_s=1)
    try:
        start = time.monotonic()
        with pytest.raises(SourcePending):
            pipeline.start()
        assert time.monotonic() - start < 0.15
        until(lambda: pipeline.process is not None)
        if name != 'hang-start':
            until(lambda: not pipeline.startup_pending)
        start = time.monotonic()
        with pytest.raises(DependencyUnavailable, match='pending'):
            pipeline.stop()
        assert time.monotonic() - start < 0.15
    finally:
        finish(pipeline)


@LINUX
def test_real_process_sharing_independence_and_crash(tmp_path):
    from fmheadendd.database import Database
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.tests.profile_support import profile_store
    db = Database(tmp_path / 'worker.db')
    first, second = (db.save('sources', source(name)) for name in ('first', 'second'))
    manager = SourceManager(db, pipeline_factory=FixturePipeline, profile_files=profile_store(tmp_path))
    workers = []
    try:
        for sid, tx in ((first.id, 0), (first.id, 1), (second.id, 2)):
            try:
                manager.acquire(sid, tx)
            except SourcePending:
                pass
        a, b = manager.pipelines[first.id], manager.pipelines[second.id]
        workers = [a, b]
        assert manager.consumers[first.id] == {0, 1}
        for p in workers:
            until(lambda: p.process is not None and not p.startup_pending)
        assert a.process.pid != b.process.pid and a.process.pid != os.getpid()
        assert until(lambda: a.output.read(0)) == PCM
        assert until(lambda: a.output.read(1)) == PCM
        assert until(lambda: b.output.read(2)) == PCM
        manager.release(first.id, 0)
        assert a.process.poll() is None
        a.process.kill()
        assert a._done.wait(3)
        assert a.snapshot()['state'] == 'ERROR'
        assert not a.snapshot().get('stream_title')
        assert until(lambda: b.output.read(2)) == PCM
        assert b.process.poll() is None
        manager.release(first.id, 1)
        assert first.id not in manager.pipelines
    finally:
        for pipeline in workers:
            finish(pipeline)
        manager.close()
        db.close()


@LINUX
def test_real_worker_startup_deadline():
    pipeline = FixturePipeline(source('hang-start'), timeout_s=1)
    try:
        with pytest.raises(SourcePending):
            pipeline.start()
        assert pipeline._done.wait(4)
        assert 'deadline' in pipeline.fatal_error
        assert pipeline.process.poll() is not None
    finally:
        finish(pipeline)


@LINUX
def test_real_worker_parent_eof_interrupts_native_start():
    import subprocess
    pipeline = FixturePipeline(source('hang-start'))
    control, child_control = socket.socketpair()
    pcm, child_pcm = socket.socketpair()
    process = subprocess.Popen([sys.executable, '-m', pipeline.worker_module,
        '--control-fd', str(child_control.fileno()), '--pcm-fd', str(child_pcm.fileno())],
        pass_fds=(child_control.fileno(), child_pcm.fileno()), close_fds=True)
    child_control.close()
    child_pcm.close()
    try:
        ipc.configure(control)
        control.sendall(pipeline._create_packet)
        deadline = time.monotonic() + 3
        while True:
            try:
                event, _ = ipc.receive(control)
                assert event['kind'] == 'accepted'
                break
            except socket.timeout:
                assert time.monotonic() < deadline
        control.close()
        assert process.wait(timeout=2) == 1
    finally:
        control.close()
        pcm.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


@LINUX
@pytest.mark.gstreamer
def test_real_http_native_pcm_and_gain(tmp_path):
    # Local deterministic HTTP fixture, real GstPipeline/DSP in child only.
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import wave
    from fmheadendd.tests.test_gstreamer import file_profile, load_gst, require, caps_structure
    pytest.importorskip('gi', reason='Native GStreamer/PyGObject unavailable')
    try:
        gst = load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))
    require(gst, 'souphttpsrc', 'wavparse', 'uridecodebin', 'volume', 'audioconvert', 'audioresample', 'appsink')
    path = tmp_path / 'constant.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack('<hh', 8000, 8000) * 48000 * 10)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pipeline = backend.ProcessPipeline(InputSourceConfig(name='native', type='HTTP',
        config={'uri': f'http://127.0.0.1:{server.server_port}/constant.wav'}),
        profile=file_profile('minus-six', gain_db=-6), reconnect=False)
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert pipeline.process.pid != os.getpid()
        data = until(lambda: pipeline.output.read(0))
        samples = struct.unpack('<' + 'h' * (len(data) // 2), data)
        assert len(data) % 4 == 0
        # Existing float DSP -> S16 audioconvert dithers channels separately.
        # The unchanged in-process backend has the same one-LSB difference.
        expected = 8000 * 10 ** (-6 / 20)
        assert all(abs(sample - expected) <= 2 for sample in samples)
        assert all(abs(l - r) <= 1 for l, r in zip(samples[::2], samples[1::2]))
        assert pipeline.probe_result().ok
        with caps_structure(gst.Caps.from_string(pipeline.probe_result().output_caps)) as caps:
            assert caps.get_string('format') == 'S16LE'
            assert caps.get_string('layout') == 'interleaved'
            assert caps.get_value('rate') == 48000 and caps.get_value('channels') == 2
    finally:
        finish(pipeline)
        server.shutdown()
        server.server_close()
        thread.join(1)
