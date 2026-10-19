"""LOCAL scan isolation: controlled I/O, synthetic PCM, no native/hardware access."""
import random
import subprocess
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.local_jobs import LocalJobs
from fmheadendd.sources.local_media import LocalMediaConfig, MediaItem
from fmheadendd.sources.local_playback import LocalPlayback
from fmheadendd.tests.local_support import initial, settle


def items(*names):
    return [MediaItem('https://example.org/' + name) for name in names]


@pytest.fixture
def pools(monkeypatch):
    from fmheadendd.sources import local_playback
    scans, cycles = LocalJobs(limit=2), LocalJobs(limit=2)
    monkeypatch.setattr(local_playback, 'scan_jobs', scans)
    monkeypatch.setattr(local_playback, 'cycle_jobs', cycles)
    return scans, cycles


def prepare_cycle(p):
    """Wait only for CPU work in the test; the scan is allowed to remain blocked."""
    for _ in range(4):
        p._service_jobs()
        if p.build is None:
            return
        assert p.build[0].done.wait(2)
    pytest.fail('cycle preparation did not settle')


class Sink:
    def __init__(self):
        self.data = bytearray()
        self.received = Event()

    def send(self, data):
        self.data.extend(data)
        self.received.set()
        return len(data)

    def close(self):
        pass


@pytest.mark.parametrize('mode', ['folder', 'playlist'])
def test_blocked_refresh_keeps_pcm_and_other_source_running(tmp_path, pools, monkeypatch, mode):
    entered, release = Event(), Event()
    calls = []

    def resolve(config):
        assert config.mode == mode
        calls.append(True)
        if len(calls) > 1:
            entered.set()
            assert release.wait(5)
        return items('a', 'b', 'c')

    p = LocalPlayback(LocalMediaConfig(mode, tmp_path, order='random'), Event(), resolver=resolve, source_id=1)
    initial(p); p.stream_start(); p.pcm(); p._service_jobs()
    assert entered.wait(2)
    scan = p.scan
    pipeline = GstPipeline(InputSourceConfig(name='local', type='LOCAL', config=dict(mode=mode, path=str(tmp_path))))
    pipeline._local = p
    pipeline.Gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1), StateChangeReturn=SimpleNamespace(FAILURE=0))
    pipeline._pipeline = SimpleNamespace(set_state=lambda state: 1)
    pipeline._convert = SimpleNamespace(get_static_pad=lambda name: SimpleNamespace(get_current_caps=lambda: None))
    pipeline._build = lambda: None
    pipeline._bus = SimpleNamespace(timed_pop=lambda timeout: None)
    sinks = [Sink(), Sink()]
    mux = UnixMuxClient(lambda source_id: pipeline.output)
    monkeypatch.setattr(mux, '_connect', lambda channel: sinks[channel])
    for channel in range(2):
        pipeline.output.attach(channel)
        mux.attach(channel, 1)
    failures = []

    def stream():
        try:
            pipeline._attempt()
        except BaseException as exc:
            failures.append(exc)

    worker = Thread(target=stream)
    worker.start()
    other = LocalPlayback(LocalMediaConfig('folder', tmp_path), Event(), resolver=lambda _: items('other'), source_id=2)
    try:
        assert initial(other).endswith('/other')
        expected = bytearray()
        for i in range(64):
            block = i.to_bytes(4, 'little') * 128
            expected.extend(block)
            for sink in sinks:
                sink.received.clear()
            assert pipeline.staging.put(block)
            for sink in sinks:
                assert sink.received.wait(2), 'PCM stalled behind discovery'
                assert sink.data == expected
        assert not scan.done.is_set() and len(calls) == 2
        assert pipeline.staging.snapshot()['dropped_blocks'] == 0
        assert all(row['dropped_blocks'] == row['buffered_bytes'] == 0
                   for row in pipeline.output.snapshot().values())
        assert all(row['disconnects'] == 0 for row in mux.pcm_snapshot().values())
        assert pipeline._pipeline is not None and not failures
    finally:
        pipeline._stop.set(); pipeline._audio_ready.set()
        worker.join(2)
        assert not worker.is_alive(), 'PCM worker waited for the blocked scan on stop'
        p.close(); other.close(); mux.close()
        release.set()
        assert scan.done.wait(2)


def test_blocked_refresh_preserves_repeated_shuffle_cycles(tmp_path, pools):
    entered, release = Event(), Event()
    calls = []
    candidates = items('a', 'b', 'c', 'd')

    def resolve(_):
        calls.append(True)
        if len(calls) > 1:
            entered.set()
            assert release.wait(5)
        return candidates

    p = LocalPlayback(LocalMediaConfig('folder', tmp_path, order='random'), Event(),
                      resolver=resolve, rng=random.Random(42))
    settle(p)
    previous = None
    try:
        for cycle in range(6):
            prepare_cycle(p)
            tickets = [p._candidate() for _ in candidates]
            uris = [t.uri for t in tickets]
            assert sorted(uris) == sorted(i.locator for i in candidates)
            assert uris[0] != previous
            previous = uris[-1]
            tickets[-1].cycle.played = True  # Model decoded PCM, not URI assignment.
            p._service_jobs()
            assert entered.wait(2)
            assert len(calls) == 2
        assert not p.scan.done.is_set()
    finally:
        scan = p.scan
        p.close(); release.set()
        assert scan.done.wait(2)


def test_completed_snapshot_waits_for_cycle_boundary(tmp_path, pools):
    candidates = items('a', 'b', 'c')
    p = LocalPlayback(LocalMediaConfig('folder', tmp_path), Event(), resolver=lambda _: candidates[:])
    try:
        settle(p)
        a = p._candidate()
        a.cycle.played = True
        candidates[:] = items('new', 'c')  # Remove a/b and add new during the cycle.
        settle(p)
        assert [p._candidate().uri for _ in range(2)] == [i.locator for i in items('b', 'c')]
        assert [p._candidate().uri for _ in range(2)] == [i.locator for i in items('new', 'c')]
    finally:
        p.close()


def test_refresh_failure_retains_current_track_and_valid_catalog(tmp_path, pools, caplog):
    calls = []
    now = [0.0]

    def resolve(_):
        calls.append(True)
        if len(calls) > 1:
            raise DomainError(503, 'FOLDER_UNREADABLE', 'private NAS path')
        return items('a', 'b')

    p = LocalPlayback(LocalMediaConfig('folder', tmp_path), Event(), resolver=resolve, clock=lambda: now[0])
    try:
        initial(p); p.stream_start(); p.pcm()
        rendering = p.rendering
        settle(p)
        assert p.rendering is rendering and p.error == 'FOLDER_UNREADABLE'
        assert 'FOLDER_UNREADABLE' in caplog.text and 'private NAS path' not in caplog.text
        assert p.resume(failed=False).endswith('/b')
        p.stream_start(); p.pcm()
        prepare_cycle(p)
        assert p.resume(failed=False).endswith('/a')
        p.stream_start(); p.pcm()
        for _ in range(10):
            p.service(None)
        assert len(calls) == 2  # No busy retry while playing the fallback.
        now[0] = 5.0
        settle(p)
        assert len(calls) == 3
    finally:
        p.close()


def test_restarts_reject_old_results_and_preserve_global_bound(tmp_path, pools):
    entered, release = Event(), Event()
    scans, _ = pools

    def stuck(_):
        entered.set()
        assert release.wait(5)
        return items('obsolete')

    config = LocalMediaConfig('folder', tmp_path)
    old = LocalPlayback(config, Event(), resolver=stuck, source_id=1)
    assert old.initial() is None and entered.wait(2)
    job = old.scan
    old.close()  # Must return while the OS operation is still blocked.
    now = [0.0]
    new = None
    try:
        for _ in range(20):
            new = LocalPlayback(config, Event(), resolver=lambda _: items('new'), source_id=1, clock=lambda: now[0])
            assert new.initial() is None and new.error == 'LOCAL_DISCOVERY_BUSY'
            assert len(scans.active) == 1
            new.close()
        # A distinct blocked source fills the second slot; no third job queues.
        second = scans.submit(2, lambda: release.wait(5), Event())
        assert second is not None
        assert scans.submit(3, lambda: None, Event()) is None
        new = LocalPlayback(config, Event(), resolver=lambda _: items('new'), source_id=1, clock=lambda: now[0])
        assert new.initial() is None
    finally:
        release.set()
    assert job.done.wait(2) and job.cancelled and job.value is None
    assert second.done.wait(2)
    now[0] = 5.0
    try:
        assert initial(new).endswith('/new')
        assert old.queue is None and old.catalog is None
    finally:
        new.close()


def test_blocked_daemon_scan_does_not_join_at_interpreter_exit():
    # A disposable Python child verifies interpreter exit, not process-based discovery.
    code = '''
from threading import Event
from fmheadendd.sources.local_jobs import LocalJobs
entered = Event()
def blocked():
    entered.set()
    Event().wait()
jobs = LocalJobs()
job = jobs.submit(1, blocked, Event())
assert entered.wait(2)
job.cancel()
print('closed', flush=True)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == 'closed', result.stderr
