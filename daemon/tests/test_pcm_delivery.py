"""PCM scheduling/ownership regressions without hardware or timing thresholds."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.output import PcmOutput, PcmQueue
from fmheadendd.mux.unix_client import UnixMuxClient, Producer


def test_concurrent_queue_preserves_pcm_without_capacity_or_age_loss():
    queue = PcmQueue(clock=lambda: 0)
    done = threading.Event()
    received = []
    def consume():
        while not done.is_set() or queue.buffered_bytes:
            item = queue.pop()
            if item is not None:
                received.append(item)
            else:
                done.wait(.001)
    thread = threading.Thread(target=consume)
    thread.start()
    expected = [i.to_bytes(4, 'little') for i in range(1000)]
    try:
        for block in expected:
            assert queue.put(block)
    finally:
        done.set()
        thread.join(2)
    assert not thread.is_alive()
    assert received == expected
    assert queue.snapshot()['dropped_blocks'] == 0


def test_queue_drop_reasons_and_notifications_are_independent():
    now = [0]
    wake = threading.Event()
    queue = PcmQueue(max_bytes=8, clock=lambda: now[0], wakeup=wake)
    for block in (b'aaaa', b'bbbb', b'cccc'):
        queue.put(block)
    assert wake.is_set()
    assert queue.pop() == b'bbbb'
    now[0] = 1
    assert queue.pop() is None
    assert not queue.put(b'bad')
    queue.put(b'dddd')
    queue.clear()
    assert queue.snapshot() == dict(accepted_bytes=16, buffered_bytes=0,
        capacity_dropped_bytes=4, stale_dropped_bytes=4, invalid_dropped_bytes=3,
        cleared_bytes=4, dropped_blocks=3)


def test_pop_rechecks_age_after_waiting_for_queue_lock():
    now = [0.0]
    queue = PcmQueue(clock=lambda: now[0])
    assert queue.put(b'x' * 4608)
    entered = threading.Event()
    original = queue._lock
    class ObservedLock:
        def __enter__(self):
            entered.set()
            original.acquire()
        def __exit__(self, *args):
            original.release()
    queue._lock = ObservedLock()
    original.acquire()
    result = []
    reader = threading.Thread(target=lambda: result.append(queue.pop()))
    reader.start()
    try:
        assert entered.wait(1)
        assert result == []
        now[0] = .101
    finally:
        original.release()
        reader.join(1)
    assert not reader.is_alive()
    assert result == [None], 'audio expired while pop waited for its lock'
    assert queue.snapshot()['stale_dropped_bytes'] == 4608


def test_source_worker_bus_never_blocks_pcm_and_idle_wakes_on_staging():
    pipeline = GstPipeline(InputSourceConfig(name='test', type='HTTP', config={'uri': 'http://localhost/live'}))
    pipeline.output.attach(0)
    pipeline.Gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1), StateChangeReturn=SimpleNamespace(FAILURE=0))
    pipeline._pipeline = SimpleNamespace(set_state=lambda state: 1)
    pipeline._convert = SimpleNamespace(get_static_pad=lambda name: SimpleNamespace(get_current_caps=lambda: None))
    pipeline._build = lambda: None
    pipeline._bus = SimpleNamespace(timed_pop=lambda timeout: None if timeout == 0 else (_ for _ in ()).throw(AssertionError('blocking bus')))
    waits = []
    real_wait = pipeline._audio_ready.wait
    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            pipeline.staging.put(b'1234')
            assert real_wait(0), 'producer must wake the worker'
        else:
            assert pipeline.output.read(0) == b'1234'
            pipeline._stop.set()
    pipeline._audio_ready.wait = wait
    pipeline._attempt()
    assert waits == [.05, .05]
    pipeline._audio_ready.clear()
    pipeline.stop()
    assert pipeline._audio_ready.is_set()


def test_startup_bus_batches_forward_pcm_but_finish_headers_before_ready():
    pipeline = GstPipeline(InputSourceConfig(name='test', type='HTTP', config={'uri': 'http://localhost/live'}))
    pipeline.output.attach(0)
    kinds = SimpleNamespace(ERROR=1, EOS=2, ELEMENT=3, TAG=4, BUFFERING=5)
    pipeline.Gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1), StateChangeReturn=SimpleNamespace(FAILURE=0),
                                   MessageType=kinds)
    pipeline._pipeline = SimpleNamespace(set_state=lambda state: 1)
    pipeline._convert = SimpleNamespace(get_static_pad=lambda name: SimpleNamespace(get_current_caps=lambda: None))
    pipeline._build = lambda: pipeline.staging.put(b'1234')
    messages = iter([SimpleNamespace(type=kinds.ELEMENT) for _ in range(65)] + [None])
    pipeline._bus = SimpleNamespace(timed_pop=lambda timeout: next(messages))
    seen = []
    def header(message):
        seen.append(message)
        assert not pipeline._ready.is_set()
        if len(seen) == 33:
            assert pipeline.output.read(0) == b'1234', 'PCM drains between bus batches'
    pipeline._station_headers = header
    def wait(timeout):
        assert len(seen) == 65
        assert pipeline._ready.is_set()
        pipeline._stop.set()
    pipeline._audio_ready.wait = wait
    pipeline._attempt()


class Socket:
    def __init__(self, name, order, blocked=False):
        self.name, self.order, self.blocked = name, order, blocked
        self.data = bytearray()
    def send(self, data):
        self.order.append(self.name)
        if self.blocked:
            raise BlockingIOError()
        self.data.extend(data)
        return len(data)
    def close(self): pass


def test_writer_drains_backlog_fairly_and_blocked_channel_cannot_stall_others():
    output = PcmOutput()
    order = []
    client = UnixMuxClient(lambda unused: output)
    for channel in range(8):
        output.attach(channel)
        client.producers[channel] = Producer(1, output, Socket(channel, order, blocked=channel == 0))
    for _ in range(3):
        output.publish(b'1234')
    assert not client._drain()
    assert order[:8] == list(range(8))
    assert order[8:16] == list(range(8))
    for channel in range(1, 8):
        assert client.producers[channel].sock.data == b'1234' * 3
    assert client.producers[0].pending == b'1234'
    client.close()


def test_connect_is_nonblocking_before_connect_and_closes_failed_attempt(monkeypatch):
    from fmheadendd.mux import unix_client
    calls = []
    class ConnectingSocket:
        def setblocking(self, value): calls.append(('blocking', value))
        def setsockopt(self, *args): pass
        def connect(self, path):
            assert calls == [('blocking', False)]
            raise BlockingIOError()
        def close(self): calls.append('closed')
    monkeypatch.setattr(unix_client.socket, 'socket', lambda *args: ConnectingSocket())
    monkeypatch.setattr(unix_client.socket, 'AF_UNIX', 1, raising=False)
    client = UnixMuxClient(lambda unused: None)
    try:
        client._connect(0)
    except BlockingIOError:
        pass
    assert calls == [('blocking', False), 'closed']


def test_writer_idle_publish_wakes_and_close_detaches_notification(monkeypatch):
    output = PcmOutput()
    output.attach(0)
    client = UnixMuxClient(lambda unused: output)
    delivered = threading.Event()
    class NotifyingSocket(Socket):
        def send(self, data):
            result = super().send(data)
            delivered.set()
            return result
    sock = NotifyingSocket(0, [])
    monkeypatch.setattr(client, '_connect', lambda channel: sock)
    try:
        client.attach(0, 1)
        output.publish(b'1234')
        assert delivered.wait(1)
    finally:
        client.close()
    assert not client._thread.is_alive()
    client._work.clear()
    output.publish(b'5678')
    assert not client._work.is_set()


def test_status_exposes_stage_consumer_writer_and_mux_diagnostics_without_io():
    from fmheadendd.status.service import StatusService
    from fmheadendd.models.runtime_status import InputSourceStatus, ModulatorStatus, SourcePcmStatus
    from fmheadendd.mux.client import MockMuxClient
    from fmheadendd.rp2354.client import MockRp2354Client
    output = PcmOutput()
    output.attach(0)
    output.attach(1)
    output.publish(b'1234')
    pcm = SourcePcmStatus(staging=PcmQueue().snapshot(), consumers=output.snapshot())
    sources = SimpleNamespace(status=Mock(return_value=InputSourceStatus(source_id=1, state='RUNNING', pcm=pcm)))
    modulators = SimpleNamespace(statuses={i: ModulatorStatus(id=i) for i in range(8)}, active_sources={0: 1, 1: 1},
        get=lambda channel: SimpleNamespace(audio_mode='DIGITAL', rf_attenuation_db=0), desired_on=lambda channel: False)
    mux = MockMuxClient()
    raw = mux.get_status()
    raw.update(late_frames=9, transport_errors=2)
    mux.get_status = Mock(return_value=raw)
    mux.pcm_snapshot = lambda: {0: dict(disconnects=3, stale_disconnects=1, discarded_pending_bytes=4)}
    rp = MockRp2354Client()
    service = StatusService(modulators, sources, rp, mux)
    rp.get_status = Mock(side_effect=AssertionError('snapshot must not read hardware'))
    mux.get_status.reset_mock()
    sources.status.reset_mock()
    status = service.snapshot()
    assert status.system.mux_late_frames == 9 and status.system.mux_transport_errors == 2
    assert status.modulators[0].pcm_consumer.buffered_bytes == 4
    assert status.modulators[1].pcm_staging.dropped_blocks == 0
    assert status.modulators[0].pcm_writer.stale_disconnects == 1
    assert status.modulators[2].pcm_staging is None
    sources.status.assert_called_once_with(1)
    mux.get_status.assert_not_called()


def test_sampler_reports_missing_reset_counters_as_unknown():
    from fmheadendd.tools.pcm_status_probe import delta
    assert delta({'a': 10, 'b': 10, 'c': None}, {'a': 14, 'b': 1, 'c': 4, 'd': None}) == {
        'a': 4, 'b': None, 'c': None, 'd': None}
