"""Small diagnostics contract tests; native elements are fakes, no RF or media IO."""
import asyncio
import json
import os
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fmheadendd.app import argument_parser
from fmheadendd.audio_telemetry import AudioTelemetry
from fmheadendd.config import Settings
from fmheadendd.control import ControlExecutor
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.configuration import InputSourceConfig
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
    p = GstPipeline(InputSourceConfig(name='station', type='HTTP', config={'uri': 'http://localhost/test'}))
    p._pipeline = Mock()
    return p


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
    p.profile = AudioProfileFile.model_validate(dict(schema_version=1, name='test', stages=stages))
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
    assert len(element.calls) == 6
    assert all(c[2] == 'source-test' for c in element.calls)
    sample = snapshots[-1]
    assert sample['sample_monotonic_ns'] == now[0]
    assert sample['collect_duration_ns'] == 0  # Injected clock; no native cost claim.
    assert sample['stages']['clipper']['values'] == dict(values, **{'max-clipping-db': None})
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
    manager = SourceManager(Mock(), pipeline_factory=GstPipeline)
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
