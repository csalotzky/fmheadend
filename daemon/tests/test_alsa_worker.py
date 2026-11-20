"""ALSA routing/configuration and existing supervisor integration, no hardware."""
from types import SimpleNamespace
import time

import pytest

from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources import process_pipeline as backend
from fmheadendd.tests.test_audio_worker import simulated_process, until, finish, pcm_message, PCM
from fmheadendd.errors import SourcePending
from fmheadendd.sources.audio_ipc import ProtocolError, envelope
from fmheadendd.sources.worker_recovery import RecoveryPolicy, WorkerRecovery


def source(device='plughw:CARD=USB,DEV=0'):
    return InputSourceConfig(name='capture', type='ALSA',
        config={'device': device, 'channel_selection': 'right'})


def test_routing_and_exact_device_transfer(monkeypatch):
    monkeypatch.setattr(backend.sys, 'platform', 'linux')
    s = source()
    p = backend.ExperimentalAudioFactory()(s)
    assert isinstance(p, backend.ProcessPipeline)
    assert p._create['source']['config'] == s.config
    assert p._catalog is None and p._podcast_service is None
    assert not isinstance(backend.ExperimentalHttpFactory()(s), backend.ProcessPipeline)
    q = p.fresh()
    assert q.output is p.output and q.generation != p.generation and q.source.config == s.config


def test_silent_pcm_is_progress_and_stale_pcm_is_rejected(monkeypatch):
    monkeypatch.setattr(backend.sys, 'platform', 'linux')
    p = backend.ProcessPipeline(source())
    p.output.attach(0)
    before = p.recovery_observation()[1]
    message = pcm_message(p)
    p._accept('pcm', message, bytes(len(PCM)))
    assert p.output.read(0) == bytes(len(PCM))
    assert p.recovery_observation()[1] >= before
    message['generation'] = 'a' * 32
    p._accept('pcm', message, PCM)
    assert p.stale_messages == 1 and p.output.read(0) is None


def alsa_report(pipeline, sequence, retry, generation=None):
    payload = {} if retry is None else {'alsa_retry': retry}
    pipeline._accept('control', envelope(pipeline.source_id, generation or pipeline.generation,
                                         sequence, 'heartbeat', payload))


def test_alsa_retry_is_immutable_generation_scoped_and_pcm_consumes_it(monkeypatch):
    monkeypatch.setattr(backend.sys, 'platform', 'linux')
    p = backend.ProcessPipeline(source())
    completed = time.monotonic() - 1
    marker = (completed, completed + 2)
    alsa_report(p, 1, marker, 'a'*32)
    assert p.alsa_retry is None
    alsa_report(p, 1, marker)
    alsa_report(p, 2, (completed, completed + 30))
    alsa_report(p, 3, (completed - 1, completed + 29))
    alsa_report(p, 4, None)
    assert p.alsa_retry == marker and p.stale_messages == 1
    deadline = marker[1] + RecoveryPolicy().buffering_timeout
    assert p.alsa_retry_allows_wait(lambda: deadline - .001, 90)
    assert not p.alsa_retry_allows_wait(lambda: deadline, 90)
    p.output.attach(0)
    p._accept('pcm', pcm_message(p), PCM)
    assert not p.alsa_retry_allows_wait(time.monotonic, 90)
    assert p.fresh().alsa_retry is None
    p.stop()
    alsa_report(p, 5, (completed + .1, completed + 1.1))
    assert p.alsa_retry == marker and not p.alsa_retry_allows_wait(time.monotonic, 90)


@pytest.mark.parametrize('bad', ['future', 'oversized', 'nan', 'boolean', 'arity'])
def test_alsa_retry_rejects_invalid_evidence(monkeypatch, bad):
    monkeypatch.setattr(backend.sys, 'platform', 'linux')
    p = backend.ProcessPipeline(source())
    now = time.monotonic()
    invalid = {'future': (now + 60, now + 61), 'oversized': (now, now + 31),
               'nan': (now, float('nan')), 'boolean': (True, now), 'arity': (now,)}
    with pytest.raises(ProtocolError, match='ALSA retry'):
        alsa_report(p, 1, invalid[bad])
    assert p.alsa_retry is None


@pytest.mark.parametrize('state', ['ERROR', 'CONNECTING'])
@pytest.mark.parametrize('failure', [None, 'frozen', 'status', 'fatal', 'exit'])
def test_supervisor_alsa_retry_only_protects_pcm(monkeypatch, state, failure):
    monkeypatch.setattr(backend.sys, 'platform', 'linux')
    p = backend.ProcessPipeline(source())
    completed = time.monotonic()
    alsa_report(p, 1, (completed, completed + 30))
    now = completed + (120 if failure == 'frozen' else 119)
    p._status['state'] = state
    p._last_status = now - 6 if failure == 'status' else now
    if failure == 'fatal':
        p._fatal_error = 'Direct writer stalled'
    if failure == 'exit':
        p._done.set()
    manager = SimpleNamespace(clock=lambda: now, pipelines={p.source_id: p},
                              consumers={p.source_id: {0}})
    recovery = WorkerRecovery(manager)
    requests = []
    recovery.request = lambda sid, **kw: requests.append((sid, kw))
    recovery.service()
    assert bool(requests) == (failure is not None)
    assert p.fatal_error == {None: None, 'frozen': 'Worker PCM progress timeout',
                            'status': 'Worker status timeout', 'fatal': 'Direct writer stalled',
                            'exit': 'Worker exited'}[failure]


def test_shared_capture_recovery_apply_and_stop(simulated_process, tmp_path):
    from fmheadendd.database import Database
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.sources.worker_recovery import RecoveryPolicy
    from fmheadendd.tests.profile_support import profile_store
    from fmheadendd.tests.test_worker_recovery import service_until
    db = Database(tmp_path/'alsa.db')
    m = SourceManager(db, pipeline_factory=backend.ProcessPipeline, profile_files=profile_store(tmp_path))
    m.worker_recovery.policy = RecoveryPolicy()
    a = db.save('sources', source())
    b = db.save('sources', InputSourceConfig(name='sibling', type='HTTP', config={'uri':'http://localhost/a'}))
    owners = []
    try:
        for sid, tx in ((a.id, 0), (b.id, 2)):
            try: m.acquire(sid, tx)
            except SourcePending: pass
        old, sibling = m.pipelines[a.id], m.pipelines[b.id]
        owners.extend((old,sibling))
        until(lambda: old.output.read(0))
        until(lambda: not old.startup_pending)
        m.acquire(a.id, 1)
        old.process.terminate()
        service_until(m, lambda: m.pipelines[a.id] is not old)
        new = m.pipelines[a.id]; owners.append(new)
        assert old.process.poll() is not None and new.output is old.output
        assert new.generation != old.generation and m.consumers[a.id] == {0,1}
        until(lambda: new.output.read(0) and new.output.read(1))
        assert until(lambda: sibling.output.read(2))
        m._restart_audio(a.model_copy(update={'input_gain_db':-6}))
        service_until(m, lambda: m.pipelines[a.id] is not new)
        current = m.pipelines[a.id]; owners.append(current)
        until(lambda: current.output.read(0))
        assert current.output is old.output and current.source.input_gain_db == -6
        m.release(a.id,0); m.release(a.id,1)
        service_until(m, lambda: a.id not in m.pipelines)
        assert m.pipelines[b.id] is sibling
    finally:
        m.close()
        for p in owners: finish(p)
        db.close()
