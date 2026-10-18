"""Small diagnostics contract tests; native elements are fakes, no RF or media IO."""
import asyncio
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fmheadendd.app import argument_parser
from fmheadendd.audio_telemetry import AudioTelemetry
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.config import Settings
from fmheadendd.control import ControlExecutor
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.configuration import InputSourceConfig, InputSource
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.sources.pipeline import MockPipeline
from fmheadendd.models.dsp import neutral_stages
from fmheadendd.sources.dsp import dsp_stages
from fmheadendd.sources.dsp_telemetry import AGC_ENABLE, METERS
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.manager import SourceManager


class Element:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []

    def get_property(self, key):
        self.calls.append(('get', key, threading.current_thread().name))
        return self.values[key]

    def set_property(self, key, value):
        self.calls.append(('set', key, threading.current_thread().name))
        self.values[key] = value


def pipeline():
    p = GstPipeline(InputSourceConfig(name='station', type='HTTP', config={'uri': 'http://localhost/test'},
        use_default_audio_profile=False, audio_profile_id='default/bypass.json'))
    p.audio_profile_id = 'default/bypass.json'
    p._pipeline = Mock()
    return p


def profile_files():
    return AudioProfileFiles(Path(__file__).resolve().parents[2] / 'audio-profiles/default', 'unused-custom')


def test_settings_cli_disabled_and_rate_bounds():
    assert Settings().audio_telemetry_socket is None
    parser = argument_parser()
    assert parser.parse_args([]).audio_telemetry_socket is None
    assert parser.parse_args([]).audio_telemetry_hz == 50
    for hz in ('10', '50', '100'):
        args = parser.parse_args(['--audio-telemetry-socket', '/tmp/meters.sock', '--audio-telemetry-hz', hz])
        assert Settings(audio_telemetry_hz=args.audio_telemetry_hz).audio_telemetry_hz == float(hz)
    for value in ('nan', 'inf', '-inf', '9', '101', 'bad'):
        with pytest.raises(SystemExit):
            parser.parse_args(['--audio-telemetry-hz', value])
        with pytest.raises(ValueError):
            Settings(audio_telemetry_hz=value)


def test_live_registry_build_generation_and_teardown():
    p = pipeline()
    stages = neutral_stages()
    stages['limiter']['selected'] = 'FM_HEADEND_LIMITER'
    p.profile = AudioProfileFile.model_validate(dict(schema_version=1, description="Test profile", name='test', stages=stages))
    # Exercise the REAL builder, with native construction/linking replaced only.
    p.Gst = SimpleNamespace(Pipeline=SimpleNamespace(new=lambda unused: Mock()),
                            Caps=SimpleNamespace(from_string=lambda value: value))
    def make(factory, **properties):
        e = Mock()
        e.get_property.side_effect = Element({k.replace('_', '-'): v for k, v in properties.items()}).get_property
        return e
    p._make = make
    p._link = Mock()
    p._build()
    assert p.telemetry_snapshot() == dict(telemetry_available=False, generation=1)
    assert 'limiter' in p._dsp_elements and 'gate' not in p._dsp_elements
    old = p._dsp_elements['limiter']
    p._build()
    assert p._dsp_elements['limiter'] is not old
    assert p.telemetry_snapshot()['generation'] == 2
    p._invalidate_telemetry(discard=True)
    assert not p._dsp_elements and not p._dsp_meters
    assert not p.telemetry_snapshot()['telemetry_available']
    # Profile validation's ordinary builder does not register a live chain.
    dsp_stages(make, p.profile)
    assert not p._dsp_elements


def test_worker_loop_raw_cache_deadline_and_disable(monkeypatch):
    p = pipeline()
    values = {'detector-peak-dbfs': -1.25, 'clip-count': 2**63 + 7, 'max-clipping-db': float('nan')}
    element = Element(values)
    p._register_dsp('clipper', 'FM_HEADEND_CLIPPER', element, {})
    # Also exercise missing properties and configured band count (including band 0).
    p._register_dsp('multiband', 'LSP_MULTIBAND', Element(), {'crossovers_hz': [100, 1000]})
    assert 'rl-0' in p._dsp_meters['multiband'].properties
    assert 'rl-2' in p._dsp_meters['multiband'].properties
    assert 'rl-3' not in p._dsp_meters['multiband'].properties
    p.request_telemetry(True)
    assert not element.calls  # Request thread cannot read GstElements.
    p.Gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1), StateChangeReturn=SimpleNamespace(FAILURE=0))
    p._build = lambda: None
    p._bus = SimpleNamespace(timed_pop=lambda timeout: None)
    now = [1_000_000_000]
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.time.monotonic_ns', lambda: now[0])
    snapshots, waits = [], []
    def wait(timeout):
        waits.append(timeout)
        snapshots.append(p.telemetry_snapshot())
        if len(waits) == 1:
            now[0] += 1_000_000_000  # Late pass: exactly one sample, no catch-up.
        else:
            p._stop.set()
    p._audio_ready.wait = wait
    worker = threading.Thread(target=p._attempt, name='source-test')
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert waits == pytest.approx([.02, .02])
    assert len(element.calls) == 8
    assert all(c[2] == 'source-test' for c in element.calls)
    sample = snapshots[-1]
    assert sample['sample_monotonic_ns'] == now[0]
    assert sample['collect_duration_ns'] == 0  # Injected clock; no native cost claim.
    assert sample['stages']['clipper']['values'] == dict(values, **{'max-clipping-db': None, 'buffer-max-clipping-db': None})
    assert sample['stages']['multiband']['errors']['rl-0'] == 'unavailable'
    json.dumps(sample, allow_nan=False)
    sample['stages'].clear()
    assert p.telemetry_snapshot()['stages']
    p.request_telemetry(False)
    assert not p.telemetry_snapshot()['telemetry_available']


def test_agc_enable_restore_only_on_worker_and_null_invalidation(monkeypatch):
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.time.monotonic_ns', lambda: 1_000_000_000)
    p = pipeline()
    original = {name: i == 0 for i, name in enumerate(AGC_ENABLE)}
    element = Element(original | {name: .73 for name in METERS['LSP_AGC']})
    p._register_dsp('agc', 'LSP_AGC', element, {})
    p._telemetry_tick()
    assert not element.calls
    p.request_telemetry(True)
    assert not element.calls
    p._telemetry_tick()
    assert all(element.values[prop] is True for prop in AGC_ENABLE)
    assert p.telemetry_snapshot()['stages']['agc']['values']['g-g'] == .73
    calls = len(element.calls)
    p._telemetry_tick()
    assert len(element.calls) == calls
    p.request_telemetry(False)
    assert all(element.values[prop] is True for prop in AGC_ENABLE)
    p._telemetry_tick()
    assert {prop: element.values[prop] for prop in AGC_ENABLE} == original
    p.request_telemetry(True)
    p._telemetry_tick()
    p.Gst = SimpleNamespace(State=SimpleNamespace(NULL=0, PLAYING=1))
    p._bus = Mock()
    p._local = Mock()
    p._local.resume.return_value = 'http://localhost/next'
    def state_change(state):
        if state == 0:
            assert not p.telemetry_snapshot()['telemetry_available']
            assert {prop: element.values[prop] for prop in AGC_ENABLE} == original
    p._pipeline.set_state.side_effect = state_change
    p._local_resume(failed=False)
    assert not p.telemetry_snapshot()['telemetry_available']


def test_manager_current_wrapper_consumers_and_new_source_request():
    manager = SourceManager(Mock(), pipeline_factory=GstPipeline, profile_files=profile_files())
    first = pipeline()
    manager.pipelines[3] = first
    manager.consumers[3] = {7, 2}
    manager.set_telemetry(True, 100)
    records = manager.telemetry_snapshot()
    assert len(records) == 1 and records[0]['consumer_tx_ids'] == [2, 7]
    assert records[0]['source_id'] == 3
    assert not records[0]['telemetry_available']
    replacement = manager._new_pipeline(first.source)
    assert replacement._telemetry_request == (True, 100)
    manager.pipelines[3] = replacement
    first._telemetry_cache = dict(stages={'old': {}})
    assert not manager.telemetry_snapshot()[0]['telemetry_available']
    manager.set_telemetry(False)
    assert replacement._telemetry_request[0] is False


@pytest.mark.skipif(os.name != 'posix', reason='asyncio AF_UNIX server is a Linux feature')
def test_socket_ndjson_reconnect_and_client_failure(tmp_path):
    async def run():
        control = ControlExecutor()
        sources = Mock()
        sources.telemetry_snapshot.return_value = [{'source_id': 3, 'telemetry_available': False}]
        server = AudioTelemetry(tmp_path / 'meters.sock', 50, control, sources)
        await server.start()
        try:
            for abrupt in (True, False):
                reader, writer = await asyncio.open_unix_connection(server.path)
                value = json.loads(await asyncio.wait_for(reader.readline(), 2))
                assert value['schema'] == 1 and value['sources'][0]['source_id'] == 3
                active = server.client
                if abrupt:
                    writer.transport.abort()
                else:
                    writer.close()
                    await writer.wait_closed()
                await asyncio.wait_for(active, 2)
                assert server.client is None
            assert [c.args[0] for c in sources.set_telemetry.call_args_list] == [True, False, True, False]
        finally:
            await server.close()
            control.close()
        assert not server.path.exists()
    asyncio.run(run())


@pytest.mark.skipif(os.name != 'posix', reason='Unix socket ownership uses POSIX file locks')
def test_socket_stale_ownership_and_refuse_other_files(tmp_path):
    async def run():
        path = tmp_path / 'meters.sock'
        control = ControlExecutor()
        sources = Mock()
        first = AudioTelemetry(path, 50, control, sources)
        await first.start()
        # Simulate process exit leaving its socket + marker, without a live listener.
        first.server.close()
        await first.server.wait_closed()
        if not path.exists():  # Python 3.13+ can clean up a closed asyncio listener.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
                stale.bind(str(path))
            info = path.stat()
            first._owner.seek(0)
            first._owner.truncate()
            json.dump(dict(application='fmheadend-audio-telemetry', dev=info.st_dev, ino=info.st_ino), first._owner)
            first._owner.flush()
        first._owner.close()
        replacement = AudioTelemetry(path, 50, control, sources)
        try:
            await replacement.start()
            assert path.exists()
        finally:
            await replacement.close()
        path.write_text('unrelated')
        refused = AudioTelemetry(path, 50, control, sources)
        try:
            with pytest.raises(OSError, match='unowned'):
                await refused.start()
        finally:
            await refused.close()
            control.close()
        assert path.read_text() == 'unrelated'
    asyncio.run(run())


def test_control_blockage_does_not_interrupt_connected_telemetry(tmp_path):
    async def run():
        control = ControlExecutor()
        sources = Mock()
        requests = []
        def request(enabled, hz):
            assert threading.current_thread().name.startswith('headend-control')
            requests.append(enabled)
        def snapshot():
            assert not threading.current_thread().name.startswith('headend-control')
            return []
        sources.set_telemetry.side_effect = request
        sources.telemetry_snapshot.side_effect = snapshot
        server = AudioTelemetry(tmp_path / 'cadence.sock', 50, control, sources)
        if os.name == 'posix':
            await server.start()
            reader, writer = await asyncio.open_unix_connection(server.path)
        else:
            # Same service/stream handling on Windows, where asyncio lacks AF_UNIX.
            server.server = await asyncio.start_server(server._connected, '127.0.0.1', 0)
            reader, writer = await asyncio.open_connection(*server.server.sockets[0].getsockname())
        entered, release = threading.Event(), threading.Event()
        times = []
        blocked = None
        try:
            await asyncio.wait_for(reader.readline(), 2)  # Enable completed first.
            def occupy():
                times.append(time.monotonic_ns())
                entered.set()
                assert release.wait(2)
                times.append(time.monotonic_ns())
            blocked = asyncio.create_task(control.call(occupy))
            assert await asyncio.to_thread(entered.wait, 2)
            async def unblock():
                await asyncio.sleep(.5)
                release.set()
            timer = asyncio.create_task(unblock())
            stamps = []
            while True:
                record = json.loads(await asyncio.wait_for(reader.readline(), 2))
                stamps.append(record['emit_monotonic_ns'])
                if len(times) == 2 and stamps[-1] > times[1]:
                    break
            await timer
            await blocked
            during = [stamp for stamp in stamps if times[0] < stamp < times[1]]
            assert len(during) >= 8, 'frames must be emitted WHILE control is blocked'
            intervals = [(b-a)/1e9 for a, b in zip(stamps, stamps[1:])]
            assert all(interval > 0 for interval in intervals)
            assert max(intervals) < .25, 'continuous output, not a control-sized gap'
            active = server.client
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(active, 2)
            assert requests == [True, False]
        finally:
            release.set()
            if blocked is not None:
                await blocked
            writer.close()
            await server.close()
            control.close()
    asyncio.run(run())


def test_emit_deadline_excludes_work_and_does_not_catch_up(monkeypatch):
    from fmheadendd import audio_telemetry
    now, stamps, sleeps = [0.0], [], []
    class Finished(Exception):
        pass
    class Writer:
        def write(self, data):
            stamps.append(now[0])
            if len(stamps) == 4:
                raise Finished()
        async def drain(self):
            now[0] += .08 if len(stamps) == 3 else .006
    async def wait_for(awaitable, timeout):
        assert timeout == .25
        return await awaitable
    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    monkeypatch.setattr(audio_telemetry, 'asyncio', SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: now[0]), wait_for=wait_for, sleep=sleep))
    sources, control = Mock(), Mock()
    sources.telemetry_snapshot.return_value = []
    service = AudioTelemetry('unused', 50, control, sources)
    with pytest.raises(Finished):
        asyncio.run(service._emit(Writer()))
    assert stamps == pytest.approx([0, .02, .04, .14])
    assert sleeps == pytest.approx([.014, .014, .02])
    control.call.assert_not_called()


def test_registry_snapshot_replacement_release_and_failed_stop():
    source = InputSource(id=3, timestamp=datetime.now(timezone.utc), name='source',
                         type='HTTP', config={'uri': 'http://localhost/test'},
                         use_default_audio_profile=False, audio_profile_id='default/bypass.json')
    database = Mock()
    database.get.return_value = source
    manager = None
    reading, finish_read = threading.Event(), threading.Event()
    def unlocked():
        assert manager._registry_lock.acquire(blocking=False), 'lock spans pipeline/cache work'
        manager._registry_lock.release()
    class Pipeline(MockPipeline):
        pause = False
        fail_stop = False
        def start(self):
            unlocked()
            super().start()
        def stop(self):
            unlocked()
            if self.fail_stop:
                raise DependencyUnavailable('stop failed')
            super().stop()
        def telemetry_snapshot(self):
            unlocked()
            if self.pause:
                reading.set()
                assert finish_read.wait(2)
            return dict(telemetry_available=False, generation=1)
    manager = SourceManager(database, pipeline_factory=Pipeline, profile_files=profile_files())
    manager.acquire(3, 7)
    manager.acquire(3, 2)
    old = manager.pipelines[3]
    old.pause = True
    snapshots, errors = [], []
    def read():
        try:
            snapshots.append(manager.telemetry_snapshot())
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=read)
    thread.start()
    try:
        assert reading.wait(2)
        assert manager._restart_audio(source) is None
        replacement = manager.pipelines[3]
        assert replacement is not old
        assert manager.telemetry_snapshot()[0]['consumer_tx_ids'] == [2, 7]
        manager.release(3, 2)
        assert manager.telemetry_snapshot()[0]['consumer_tx_ids'] == [7]
        replacement.fail_stop = True
        with pytest.raises(DependencyUnavailable):
            manager.release(3, 7)
        assert manager.pipelines[3] is replacement
        assert manager.telemetry_snapshot()[0]['consumer_tx_ids'] == [7]
        replacement.fail_stop = False
        manager.release(3, 7)
        assert manager.telemetry_snapshot() == []
    finally:
        finish_read.set()
        thread.join(2)
    assert not thread.is_alive() and not errors
    assert len(snapshots[0]) == 1
    assert snapshots[0][0]['source_id'] == 3
    assert snapshots[0][0]['consumer_tx_ids'] == [2, 7]
    manager.acquire(3, 1)
    manager.close()
    assert manager.telemetry_snapshot() == []


@pytest.mark.parametrize('race', ['connected', 'drain', 'eof', 'busy', 'blocked_control'])
def test_close_owns_connected_client_and_is_bounded(tmp_path, race):
    async def run():
        control = ControlExecutor()
        sources = SourceManager(Mock())
        p = pipeline()
        sources.pipelines[3] = p
        sources.consumers[3] = {2, 7}
        service = AudioTelemetry(tmp_path / 'shutdown.sock', 50, control, sources)
        if os.name == 'posix':
            await service.start()
            reader, writer = await asyncio.open_unix_connection(service.path)
        else:
            service.server = await asyncio.start_server(service._connected, '127.0.0.1', 0)
            reader, writer = await asyncio.open_connection(*service.server.sockets[0].getsockname())
        blocked = None
        release = threading.Event()
        try:
            await asyncio.wait_for(reader.readline(), 1)
            assert p._telemetry_request[0]
            active = service.client
            if race == 'drain':
                entered = asyncio.Event()
                async def drain():
                    entered.set()
                    await asyncio.Future()
                service._writer.drain = drain
                await asyncio.wait_for(entered.wait(), 1)
            if race == 'blocked_control':
                entered = threading.Event()
                def occupy():
                    entered.set()
                    assert release.wait(5)
                blocked = asyncio.create_task(control.call(occupy))
                assert await asyncio.to_thread(entered.wait, 1)
            if race == 'busy':
                control.pending = 32
                writer.close()  # Natural EOF cleanup is already retrying disable.
                await writer.wait_closed()
                await asyncio.sleep(.06)
            if race == 'eof':
                writer.close()
            # Connected/drain/blocked-control cases leave the REMOTE client open.
            await asyncio.wait_for(asyncio.gather(service.close(), service.close()), 1)
            assert active.done()
            assert service.client is None and service._writer is None
            assert not service.server.is_serving()
            assert not p._telemetry_request[0]
            assert not sources._telemetry_request[0]
            assert not p.telemetry_snapshot()['telemetry_available']
            if race not in ('eof', 'busy'):
                try:
                    await asyncio.wait_for(reader.read(), .5)
                    assert reader.at_eof()
                except ConnectionResetError:
                    pass  # An abort may reset a connection with pending data.
            if os.name == 'posix':
                assert not service.path.exists()
                assert service._owner.closed
                with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
                    await asyncio.open_unix_connection(service.path)
            await asyncio.wait_for(service.close(), .2)
        finally:
            release.set()
            if blocked is not None:
                await blocked
            if race == 'busy':
                control.pending = 0
            writer.close()
            await service.close()
            control.close()
    asyncio.run(run())


def test_terminal_disable_fences_queued_enables_and_unpublished_wrapper():
    manager = SourceManager(Mock(), pipeline_factory=GstPipeline, profile_files=profile_files())
    manager.set_telemetry(True)
    pending = manager._new_pipeline(pipeline().source)
    assert pending._telemetry_request[0]
    manager.shutdown_telemetry()
    manager.set_telemetry(True)  # A formerly queued control request.
    manager.consumers[3] = set()
    manager._publish_pipeline(3, pending, 7)
    assert not pending._telemetry_request[0]
    assert not manager._telemetry_request[0]
