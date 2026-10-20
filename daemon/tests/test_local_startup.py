"""Deferred LOCAL startup through real ownership/recovery, with controlled I/O."""
import threading

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import SourceType
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.local_jobs import LocalJobs
from fmheadendd.sources.local_media import LocalMediaConfig, MediaItem
from fmheadendd.sources.local_playback import LocalPlayback
from fmheadendd.tests.profile_support import profile_settings


@pytest.mark.parametrize('outcome', ['ready', 'stop', 'FOLDER_UNREADABLE', 'NO_PLAYABLE_TRACKS'])
def test_pending_local_start_retains_worker_and_rf_safety(tmp_path, monkeypatch, caplog, outcome):
    from fmheadendd.sources import local_playback, gst_pipeline
    from types import SimpleNamespace

    entered, release, constructed, pcm = (threading.Event() for _ in range(4))
    scans, cycles = LocalJobs(), LocalJobs()
    monkeypatch.setattr(local_playback, 'scan_jobs', scans)
    monkeypatch.setattr(local_playback, 'cycle_jobs', cycles)
    now = [0.0]
    monkeypatch.setattr(gst_pipeline, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    caplog.set_level('INFO')
    instances, jobs = [], []

    def resolve(_):
        entered.set()
        assert release.wait(5)
        if outcome in ('FOLDER_UNREADABLE', 'NO_PLAYABLE_TRACKS'):
            raise DomainError(503, outcome, 'test discovery failure')
        return [MediaItem('https://example.org/track')]

    class Pipeline(GstPipeline):
        @staticmethod
        def validate_profile(profile):
            pass  # Native plugins are not part of this synthetic startup test.

        def __init__(self, source, **kwargs):
            super().__init__(source, **kwargs)
            instances.append(self)

        def _run(self):
            # Substitute native decoding only; real start/check/stop, catalog
            # jobs, SourceManager ownership and TX recovery remain in use.
            assert self.source.type == SourceType.LOCAL
            self._local = LocalPlayback(LocalMediaConfig('folder', tmp_path), self._audio_ready,
                                        resolver=resolve, source_id=self.source.id)
            try:
                self._local.initial()
                jobs.append(self._local.scan)
                while not self._stop.is_set():
                    self._audio_ready.clear()
                    self._local.service(None)
                    if self._local.prepared is not None:
                        self._prepared.set()
                        constructed.set()
                        if pcm.is_set():
                            self.output.publish(b'\x00\x00\x01\x00' * 128)
                            self._update(state='RUNNING', last_error=None)
                            self._ready.set()
                    self._audio_ready.wait(0.05)
            except Exception as exc:
                self._failure, self.fatal_error = exc, str(exc)
                self._update(state='ERROR', last_error=str(exc), error_scope='SOURCE')
            finally:
                self._local.close()
                self._finished.set()

    app = create_app(profile_settings(tmp_path / 'db'), pipeline_factory=Pipeline, clock=lambda: now[0])
    with TestClient(app) as api:
        s = app.state.services
        sid = api.post('/api/v1/sources', json={
            'name': 'Folder', 'type': 'LOCAL', 'config': {'mode': 'folder', 'path': str(tmp_path)},
        }).json()['id']
        for channel in (0, 1):
            assert api.patch(f'/api/v1/modulators/{channel}', json={
                'input_source_id': sid, 'frequency_khz': 98100,
            }).status_code == 200
        try:
            for channel in (0, 1):
                response = api.post(f'/api/v1/modulators/{channel}/start')
                assert response.status_code == 503
                assert 'PENDING' in response.json()['message']
            assert entered.wait(2)
            assert len(instances) == 1 and len(scans.active) == 1
            pipeline = instances[0]

            def service():
                s.recovery.service()
                app.state.publish_status()

            now[0] = 20.0  # Beyond the old 10-second deadline, without sleeping.
            app.state.control.executor.submit(service).result(timeout=2)
            assert s.sources.pipelines[sid] is pipeline
            assert not pipeline._stop.is_set()
            assert not any(s.rp.enabled)
            assert api.get('/api/v1/modulators/0').json()['enabled']
            assert set(s.sources.consumers[sid]) == {0, 1}
            if outcome == 'stop':
                for channel in (0, 1):
                    assert api.post(f'/api/v1/modulators/{channel}/stop').status_code == 200
                assert pipeline._finished.wait(2)
                assert sid not in s.sources.pipelines
            release.set()
            assert jobs[0].done.wait(2)
            if outcome == 'ready':
                assert constructed.wait(2)
                now[0] = 30.0
                app.state.control.executor.submit(service).result(timeout=2)
                assert not any(s.rp.enabled)  # URI/preparation alone is insufficient.
                pcm.set()
                pipeline._audio_ready.set()
                assert pipeline._ready.wait(2)
            elif outcome != 'stop':
                assert pipeline._finished.wait(2)
            now[0] = 40.0
            for _ in range(3):
                app.state.control.executor.submit(service).result(timeout=2)
            assert s.rp.enabled[:2] == ([True, True] if outcome == 'ready' else [False, False])
            assert not s.recovery.audio_pending
            assert len(instances) == 1
            for channel in (0, 1):
                assert api.get(f'/api/v1/modulators/{channel}').json()['enabled'] == (outcome == 'ready')
            if outcome == 'stop':
                assert jobs[0].cancelled and jobs[0].value is None
            elif outcome != 'ready':
                assert outcome in api.get(f'/api/v1/sources/{sid}/status').json()['last_error']
                assert 'enable intent cleared' in caplog.text
            assert 'LOCAL startup deferred' in caplog.text
        finally:
            release.set()
