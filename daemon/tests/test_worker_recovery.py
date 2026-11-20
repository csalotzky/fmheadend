import time
from unittest.mock import Mock

import pytest

from fmheadendd.errors import SourcePending
from fmheadendd.database import Database
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import simulated_process, source, until, finish, PCM


@pytest.fixture
def managed(simulated_process, tmp_path):
    db = Database(tmp_path / 'recovery.db')
    m = SourceManager(db, pipeline_factory=ProcessPipeline, profile_files=profile_store(tmp_path))
    m.worker_recovery.policy = RecoveryPolicy()
    a, b = [db.save('sources', source(name)) for name in ('one', 'two')]
    for sid, tx in ((a.id, 0), (a.id, 1), (b.id, 2)):
        try:
            m.acquire(sid, tx)
        except SourcePending:
            pass
    for sid, tx in ((a.id, 0), (b.id, 2)):
        until(lambda: m.pipelines[sid].output.read(tx))
    yield m, a, b
    workers = list(m.pipelines.values())
    m.close()
    for worker in workers:
        finish(worker)
    db.close()


def service_until(m, condition):
    def step():
        m.worker_recovery.service()
        return condition()
    return until(step)


def test_crash_restarts_shared_source_without_touching_sibling(managed):
    m, a, b = managed
    old, sibling = m.pipelines[a.id], m.pipelines[b.id]
    old.process.terminate()
    service_until(m, lambda: m.pipelines[a.id] is not old)
    new = m.pipelines[a.id]
    assert old._done.is_set() and old.process.poll() is not None
    assert new.output is old.output and new.generation != old.generation
    from fmheadendd.tests.test_audio_worker import pcm_message
    new._accept('pcm', pcm_message(old, 999999), PCM)
    assert new.stale_messages == 1
    assert m.consumers[a.id] == {0, 1}
    assert until(lambda: new.output.read(0)) == PCM
    assert until(lambda: new.output.read(1)) == PCM
    assert m.pipelines[b.id] is sibling
    assert until(lambda: sibling.output.read(2)) == PCM


@pytest.mark.parametrize('state,age,restart', [
    ('RUNNING', 16, True), ('BUFFERING', 20, False),
    ('CONNECTING', 91, True), ('BUFFERING', 91, True), ('RUNNING', 1, False)])
def test_progress_deadlines_with_responsive_control(managed, state, age, restart):
    m, a, b = managed
    old = m.pipelines[a.id]
    now = m.clock()
    old.recovery_observation = lambda: (now, now-age, state, None, False)
    m.worker_recovery.service()
    assert (a.id in m.worker_recovery.pending) == restart


def test_status_loss_and_silent_pcm(managed):
    from fmheadendd.tests.test_audio_worker import pcm_message
    m, a, b = managed
    old = m.pipelines[a.id]
    old._accept('pcm', pcm_message(old, 1000000), bytes(3840))
    status, pcm_at, state, error, done = old.recovery_observation()
    assert m.clock() - pcm_at < 1
    m.worker_recovery.service()
    assert a.id not in m.worker_recovery.pending
    # The phase-aware status decision now reads the adapter under its report
    # lock; make the actual accepted-report clock stale, not only a tuple stub.
    with old._lock:
        old._last_status = m.clock()-6
    m.worker_recovery.service()
    assert a.id in m.worker_recovery.pending


def test_stop_during_recovery_and_latest_apply_complete(managed):
    m, a, b = managed
    old = m.pipelines[a.id]
    changed = a.model_copy(update={'input_gain_db': -6})
    assert 'pending' in m._restart_audio(changed)
    latest = a.model_copy(update={'input_gain_db': -9})
    m._restart_audio(latest)
    service_until(m, lambda: m.pipelines[a.id] is not old)
    assert m.pipelines[a.id].source.input_gain_db == -9
    m.release(a.id, 0)
    m.release(a.id, 1)
    service_until(m, lambda: a.id not in m.pipelines)
    assert not m.consumers.get(a.id)
    for _ in range(3):
        m.worker_recovery.service()
    assert a.id not in m.pipelines


def test_web_control_and_rf_unchanged_during_recovery(simulated_process, tmp_path):
    from fastapi.testclient import TestClient
    from fmheadendd.app import create_app
    from fmheadendd.tests.profile_support import profile_settings
    with TestClient(create_app(profile_settings(tmp_path / 'web.db'), pipeline_factory=ProcessPipeline)) as api:
        services = api.app.state.services
        m = services.sources
        executor = api.app.state.control.executor
        sid = api.post('/api/v1/sources', json=source().model_dump(mode='json')).json()['id']
        def begin():
            m.worker_recovery.policy = RecoveryPolicy()
            try:
                m.acquire(sid, 0)
            except SourcePending:
                pass
        executor.submit(begin).result(timeout=2)
        old = m.pipelines[sid]
        until(lambda: old.output.read(0))
        until(lambda: not old.startup_pending)
        assert api.patch('/api/v1/modulators/0', json={'input_source_id': sid,
            'frequency_khz': 98100}).status_code == 200
        assert api.post('/api/v1/modulators/0/start').status_code == 200
        rf = Mock(wraps=services.rp.set_tx_enabled)
        services.rp.set_tx_enabled = rf
        old.process.terminate()
        started = time.monotonic()
        assert api.get('/api/v1/status').status_code == 200
        assert time.monotonic() - started < 1
        until(lambda: m.pipelines[sid] is not old)
        assert until(lambda: m.pipelines[sid].output.read(0)) == PCM
        rf.assert_not_called()
        assert api.post('/api/v1/modulators/0/stop').status_code == 200
        until(lambda: sid not in m.pipelines)


def test_cooldown_and_pending_reap_are_bounded(managed):
    m, a, b = managed
    clock = [m.clock()]
    m.clock = lambda: clock[0]
    old = m.pipelines[a.id]
    old.process.terminate()
    assert old._done.wait(3)
    m.worker_recovery.service()
    m.worker_recovery.service()
    assert m.worker_recovery.pending[a.id][2] == clock[0] + 1
    assert m.pipelines[a.id] is old
    clock[0] += 1
    m.worker_recovery.service()
    new = m.pipelines[a.id]
    assert new is not old
    until(lambda: new.process is not None)
    new.process.terminate()
    assert new._done.wait(3)
    m.worker_recovery.service()
    m.worker_recovery.service()
    assert m.worker_recovery.pending[a.id][2] == clock[0] + 2
    clock[0] += 2
    m.worker_recovery.service()
    assert m.pipelines[a.id] is not new

def test_pending_reap_never_launches_and_control_remains_responsive(managed):
    m, a, b = managed
    old = m.pipelines[a.id]
    original = old.recovery_observation
    old.recovery_observation = lambda: (*original()[:-1], False)
    m._restart_audio(a)
    started = time.monotonic()
    for _ in range(100):
        m.worker_recovery.service()
    assert time.monotonic() - started < 0.5
    assert m.pipelines[a.id] is old
    old.recovery_observation = original
    service_until(m, lambda: m.pipelines[a.id] is not old)


def test_stop_cancels_queued_restart(managed):
    m, a, b = managed
    old = m.pipelines[a.id]
    m.worker_recovery.policy = RecoveryPolicy()
    old.process.terminate()
    assert old._done.wait(3)
    m.worker_recovery.service()
    assert a.id in m.worker_recovery.pending
    m.release(a.id, 0)
    m.release(a.id, 1)
    m.worker_recovery.service()
    assert a.id not in m.pipelines and a.id not in m.worker_recovery.pending


def test_failed_replacement_publishes_error_without_rapid_retry_loop(managed, monkeypatch):
    m, a, b = managed
    old = m.pipelines[a.id]
    m._restart_audio(a)
    assert old._done.wait(3)
    monkeypatch.setattr(m, '_new_pipeline', Mock(side_effect=ValueError('invalid replacement profile')))
    m.worker_recovery.service()
    assert m.status(a.id).state == 'ERROR'
    assert 'invalid replacement profile' in m.status(a.id).last_error
    m.worker_recovery.service()
    assert m._new_pipeline.call_count == 1


def test_api_stop_disables_tx_during_cooldown_and_new_start_has_no_inherited_delay(simulated_process, tmp_path):
    from fastapi.testclient import TestClient
    from fmheadendd.app import create_app
    from fmheadendd.tests.profile_support import profile_settings
    with TestClient(create_app(profile_settings(tmp_path / 'cooldown-api.db'), pipeline_factory=ProcessPipeline)) as api:
        services=api.app.state.services;m=services.sources;executor=api.app.state.control.executor
        sid=api.post('/api/v1/sources',json=source().model_dump(mode='json')).json()['id']
        def acquire():
            try:m.acquire(sid,0)
            except SourcePending:pass
        executor.submit(acquire).result(timeout=2)
        old=m.pipelines[sid];until(lambda:old._has_pcm);until(lambda:not old.startup_pending)
        assert api.patch('/api/v1/modulators/0',json={'input_source_id':sid,'frequency_khz':98100}).status_code==200
        assert api.post('/api/v1/modulators/0/start').status_code==200
        executor.submit(lambda:m.worker_recovery.attempts.update({sid:6})).result(timeout=2)
        old.process.terminate();assert old._done.wait(3)
        until(lambda:sid in m.worker_recovery.pending and m.worker_recovery.pending[sid][2] is not None)
        assert m.worker_recovery.pending[sid][2]-m.clock()>119
        assert api.post('/api/v1/modulators/0/stop').status_code==200
        assert api.get('/api/v1/modulators/0').json()['enabled'] is False
        until(lambda:sid not in m.pipelines)
        def advance():
            original=m.clock
            try:
                m.clock=lambda:original()+10000
                m.worker_recovery.service()
            finally:m.clock=original
        executor.submit(advance).result(timeout=2)
        assert sid not in m.pipelines and sid not in m.worker_recovery.pending
        assert api.post('/api/v1/modulators/0/start').status_code==200
        assert m.pipelines[sid] is not old and sid not in m.worker_recovery.pending
        until(lambda:m.pipelines[sid]._has_pcm)
        assert api.post('/api/v1/modulators/0/stop').status_code==200
        until(lambda:sid not in m.pipelines)
