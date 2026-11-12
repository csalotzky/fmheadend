"""Portable catalog/sequence/metadata boundary checks; no native substitution."""
from threading import Event
from types import SimpleNamespace
import random
import time

import pytest

from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSource
from fmheadendd.sources import local_worker as bridge
from fmheadendd.sources.audiod import TransportOutput
from fmheadendd.sources.audio_ipc import ProtocolError, unpack
from fmheadendd.sources.local_jobs import LocalJobs
from fmheadendd.sources.local_media import LocalMediaConfig
from fmheadendd.sources.output import PcmQueue
from fmheadendd.models.configuration import SourceTestResult
from fmheadendd.tests.test_audio_worker import until
from fmheadendd.sources.worker_recovery import WorkerRecovery
from unittest.mock import Mock


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, 'scan_jobs', LocalJobs(1))
    monkeypatch.setattr(bridge, 'cycle_jobs', LocalJobs(1))
    source = InputSource(id=1, timestamp='2026-11-11T00:00:00', name='local', type='LOCAL',
                         config={'mode': 'folder', 'path': str(tmp_path)})
    parent, worker = bridge.ParentCatalog(source), bridge.WorkerCatalog()
    yield parent, worker
    parent.close()


def pump(parent, worker):
    for _ in range(3):
        request = worker.poll()
        if request is None:
            break
        parent.accept(request)
    response = parent.poll()
    if response:
        worker.accept(response)


@pytest.mark.parametrize('order', ['sequential', 'random'])
def test_remote_policy_cycles_refresh_and_prefetch(pair, tmp_path, order):
    parent, worker = pair
    for n in range(24):
        (tmp_path / f'track{n}.wav').touch()
    config = LocalMediaConfig('folder', tmp_path, order=order)
    p = worker.playback(config, Event(), rng=random.Random(42), source_id=1)
    def settle():
        def step():
            p._service_jobs()
            pump(parent, worker)
            return p.scan is None and p.build is None
        until(step)
    try:
        settle()
        first = p.initial()
        assert p.rendering is None and p.prepared is not None
        assert p.stream_start() == first
        assert not p.cycle.played  # Submission and STREAM_START are not PCM.
        p.pcm()
        settle()
        seen = [first]
        old_cycle = p.cycle
        (tmp_path / 'track100.wav').touch()
        while len(seen) < 24:
            seen.append(p.resume(failed=False))
            p.stream_start(); p.pcm()
            settle()
            assert p.cycle is old_cycle or len(seen) == 24
        assert len(set(seen)) == 24 and not any('track100.wav' in x for x in seen)
        if order == 'sequential':
            assert seen == [(tmp_path / f'track{n}.wav').as_uri() for n in range(24)]
        else:
            assert seen != sorted(seen)
        # The prefetched next cycle incorporates the refreshed snapshot atomically.
        assert p.prepared.cycle is not old_cycle
        next_seen = []
        for _ in range(25):
            next_seen.append(p.resume(failed=False))
            p.stream_start(); p.pcm(); settle()
        assert len(set(next_seen)) == 25
        assert (tmp_path / 'track100.wav').as_uri() in next_seen
        if order == 'random':
            assert next_seen[0] != seen[-1]
    finally:
        p.close()


def test_catalog_atomic_revision_and_malformed_chunks(pair):
    _, worker = pair
    job = worker.submit('scan', lambda: None, Event())
    request = worker.poll()['request']
    worker.accept(dict(op='catalog', request=request, offset=0, items=[['url', 'https://a/b']], final=False))
    assert not job.done.is_set() and job.value is None
    with pytest.raises(ProtocolError):
        worker.accept(dict(op='catalog', request=request, offset=0, items=[['url', 'https://a/c']], final=True))
    with pytest.raises(ProtocolError):
        worker.accept(dict(op='catalog', request=request - 1, offset=1, items=[['url', 'https://a/c']], final=True))
    worker.accept(dict(op='catalog', request=request, offset=1, items=[['url', 'https://a/c']], final=True))
    assert job.done.is_set() and [x.locator for x in job.value] == ['https://a/b', 'https://a/c']


def test_missing_directory_retry_without_replacing_playback(pair, tmp_path):
    parent, worker = pair
    missing = tmp_path / 'usb'
    parent.config = LocalMediaConfig('folder', missing)
    now = [0.0]
    p = worker.playback(parent.config, Event(), source_id=1, clock=lambda: now[0])
    try:
        def step():
            p._service_jobs(); pump(parent, worker)
            return p.retry_at > now[0]
        until(step)
        assert p.initial() is None and p.error == 'FOLDER_UNREADABLE'
        missing.mkdir(); (missing / 'track.wav').touch()
        now[0] = 6
        def available():
            p._service_jobs(); pump(parent, worker)
            return p.next_queue is not None
        until(available)
        assert p.initial() == (missing / 'track.wav').as_uri()
    finally:
        p.close()


def test_global_cycle_slot_retained_until_release_or_reap(pair):
    parent, worker = pair
    entered, release = Event(), Event()
    def operation():
        entered.set()
        assert release.wait(3)
        return 'built'
    job = worker.submit('cycle', operation, Event())
    try:
        pump(parent, worker)
        assert entered.wait(1)
        assert bridge.cycle_jobs.reserve(2) is None
        release.set()
        assert job.done.wait(1) and job.value == 'built'
        assert bridge.cycle_jobs.reserve(2) is None  # Completion has not crossed IPC.
        pump(parent, worker)
        assert not bridge.cycle_jobs.active
        worker.submit('cycle', lambda: 1, Event())
        pump(parent, worker)
        parent.close()  # Invoked by owner only after actual child reap.
        assert not bridge.cycle_jobs.active
    finally:
        release.set()


def test_retired_scan_keeps_global_key_until_actual_return(pair, monkeypatch):
    parent, worker = pair
    entered, release = Event(), Event()
    def blocked(_):
        entered.set(); assert release.wait(3)
        raise DomainError(503, 'FOLDER_UNREADABLE', 'gone')
    monkeypatch.setattr(bridge, 'resolve_media', blocked)
    worker.submit('scan', lambda: None, Event())
    try:
        pump(parent, worker)
        assert entered.wait(1)
        parent.close()
        assert bridge.scan_jobs.submit(parent.owner, lambda: (), Event()) is None
    finally:
        release.set()
    until(lambda: not bridge.scan_jobs.active)


def test_m3u_and_natural_folder_catalog(pair, tmp_path):
    parent, worker = pair
    playlist = tmp_path / 'list.m3u'
    playlist.write_text('# comment\ntrack10.wav\ntrack2.wav\nhttps://a/x\ntrack2.wav\n')
    parent.config = LocalMediaConfig('playlist', playlist)
    job = worker.submit('scan', lambda: None, Event())
    def step():
        pump(parent, worker)
        return job.done.is_set()
    until(step)
    assert [str(i.locator) for i in job.value] == [str(tmp_path / 'track10.wav'),
        str(tmp_path / 'track2.wav'), 'https://a/x', str(tmp_path / 'track2.wav')]


def test_staging_carries_pcm_identity_not_latest_snapshot():
    state = {'state': 'RUNNING', 'title': 'old'}
    output = TransportOutput(1, 'a' * 32)
    output.pipeline = SimpleNamespace(snapshot=lambda: dict(state),
        probe_result=lambda: SourceTestResult(ok=True, mocked=False))
    staging = PcmQueue()
    staging.put(output.capture(b'\x01\x00' * 64))
    state['title'] = 'prefetched-new'
    output.publish(staging.pop())
    packet = output.queue.pop()
    message, data = unpack(packet[4:])
    assert message['payload']['status']['title'] == 'old'
    assert data == b'\x01\x00' * 64


@pytest.mark.parametrize('waiting,status_age,restart', [(True, 0, False), (True, 6, True), (False, 0, True)])
def test_expected_discovery_wait_never_masks_control_loss(waiting, status_age, restart):
    old = SimpleNamespace(recovery_observation=lambda: (1000-status_age, 0, 'ERROR', None, False),
        expected_media_wait=waiting, recovery_exhausted=False, _fail=Mock(), stop=Mock())
    manager = SimpleNamespace(clock=lambda: 1000, pipelines={1: old}, consumers={1: {0}})
    recovery = WorkerRecovery(manager)
    recovery.service()
    assert (1 in recovery.pending) == restart
    assert old.stop.call_count == int(restart)


def test_large_unicode_catalog_chunks_fit_wire_and_publish_once(pair, monkeypatch):
    from fmheadendd.sources.audio_ipc import envelope, pack
    from fmheadendd.sources.local_media import MediaItem
    parent, worker = pair
    candidates = [MediaItem('https://example.org/' + '\u00e9' * 7000 + str(i)) for i in range(4)]
    monkeypatch.setattr(bridge, 'resolve_media', lambda config: candidates)
    job = worker.submit('scan', lambda: None, Event())
    parent.accept(worker.poll())
    chunks = 0
    while not job.done.is_set():
        response = until(parent.poll)
        chunks += 1
        pack(envelope(1, 'a' * 32, chunks, 'local_response', response))
        worker.accept(response)
        if not response['final']:
            assert job.value is None
    assert chunks == 4 and list(job.value) == candidates


def test_explicit_selection_preserves_http_only_option(monkeypatch):
    from fmheadendd.sources import process_pipeline, gst_pipeline
    monkeypatch.setattr(process_pipeline, 'ProcessPipeline', lambda *a, **kw: 'process')
    monkeypatch.setattr(gst_pipeline, 'GstPipeline', lambda *a, **kw: 'inprocess')
    legacy = process_pipeline.ExperimentalHttpFactory()
    extended = process_pipeline.ExperimentalAudioFactory()
    for kind in ('HTTP', 'LOCAL', 'PODCAST', 'ALSA', 'UDP', 'RTP'):
        source = SimpleNamespace(type=kind)
        assert legacy(source) == ('process' if kind == 'HTTP' else 'inprocess')
        assert extended(source) == ('process' if kind in ('HTTP', 'LOCAL') else 'inprocess')
