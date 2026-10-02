"""Polling is a reader of bounded publications, never audio/control work."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.errors import DomainError
from fmheadendd.sources.output import PcmOutput
from fmheadendd.tests.test_audio_profiles import FakePipeline

P = '/api/v1'
DEFAULT = Path(__file__).resolve().parents[2] / 'audio-profiles/default'


def configured(tmp_path, clock):
    return create_app(Settings(tmp_path/'db', audio_profiles_default_dir=DEFAULT,
                               audio_profiles_custom_dir=tmp_path/'custom'),
                      pipeline_factory=FakePipeline, clock=clock)


def start(api):
    sid = api.post(P+'/sources', json={'name':'Feed','type':'HTTP',
                    'config':{'uri':'http://localhost/live'}}).json()['id']
    for tx in (0, 1):
        assert api.patch(f'{P}/modulators/{tx}', json={'input_source_id':sid, 'frequency_khz':98100}).status_code == 200
        assert api.post(f'{P}/modulators/{tx}/start').status_code == 200
    return sid


def test_all_periodic_reads_bypass_busy_control_and_audio_locks(tmp_path, monkeypatch):
    now = [0.0]
    app = configured(tmp_path, lambda:now[0])
    with TestClient(app) as api:
        sid = start(api)
        services = app.state.services
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            assert release.wait(10), 'test failed to release control executor'
        blocker = app.state.control.executor.submit(hold)
        assert entered.wait(2)
        output = services.sources.pipelines[sid].output
        with ThreadPoolExecutor(max_workers=8) as pool:
            try:
                # Lock out the actual audio ownership and queue snapshot paths.
                with output._lock:
                    with monkeypatch.context() as m:
                        for obj, method in ((services.status, 'snapshot'), (services.sources, 'status'),
                                (services.profile_files, 'list'), (services.rp, 'get_status'),
                                (services.mux, 'get_status'), (services.config, 'list'),
                                (services.rt, 'effective')):
                            m.setattr(obj, method, Mock(side_effect=AssertionError('poll touched owner')))
                        paths = ['/status', '/status', '/sources', '/modulators',
                                 f'/sources/{sid}/status', '/system/info',
                                 '/audio-profiles/files', '/system/logs']
                        futures = [pool.submit(api.get, P+path) for path in paths]
                        replies = [f.result(timeout=3) for f in futures]
                        assert all(r.status_code == 200 for r in replies)
                        assert replies[0].json()['modulators'][0]['source_state'] == 'RUNNING'
                        assert replies[4].json()['state'] == 'RUNNING'
                        for obj, method in ((services.sources,'status'), (services.profile_files,'list'),
                                            (services.rp,'get_status'), (services.mux,'get_status')):
                            getattr(obj,method).assert_not_called()
            finally:
                release.set()
                blocker.result(timeout=2)
        # Shutdown still owns/joins the original pipelines; no new worker exists.
    assert services.sources.pipelines == {}


def test_expiration_retains_values_without_inventing_hardware_failure(tmp_path):
    now=[0.0]
    app=configured(tmp_path,lambda:now[0])
    with TestClient(app) as api:
        sid=start(api)
        status=app.state.services.status
        old=status.published_snapshot()
        now[0]=3.0
        stale=status.published_snapshot()
        assert stale.system.snapshot_age_s == 3
        assert stale.system.snapshot_stale and stale.system.rp_observation_stale
        assert stale.system.rp_observation_age_s == old.system.rp_observation_age_s + 3
        assert stale.modulators == old.modulators
        assert stale.system.system_state == old.system.system_state
        assert status.published_source(sid).observation_stale
        assert not old.system.snapshot_stale
        app.state.control.executor.submit(app.state.publish_status).result()
        assert not status.published_snapshot().system.snapshot_stale


def test_publication_owns_source_models_and_mutations_are_immediately_visible(tmp_path):
    app=configured(tmp_path,lambda:0.0)
    with TestClient(app) as api:
        sid=start(api)
        before=app.state.services.status.published_source(sid)
        assert api.post(P+'/modulators/stop-all').status_code == 200
        rows=api.get(P+'/modulators').json()
        assert rows[0]['enabled'] and rows[1]['enabled']
        assert api.get(P+'/status').json()['modulators'][0]['tx_state'] == 'OFF'
        assert api.get(f'{P}/sources/{sid}/status').json()['state'] == 'IDLE'
        assert before.state == 'RUNNING'
        assert api.patch(f'{P}/sources/{sid}',json={'name':'Renamed'}).status_code == 200
        assert api.get(P+'/sources').json()[0]['name'] == 'Renamed'
        assert api.get(f'{P}/sources/999/status').status_code == 404


def test_publication_failure_does_not_fail_confirmed_stop(tmp_path,monkeypatch):
    app=configured(tmp_path,lambda:0.0)
    with TestClient(app) as api:
        start(api)
        with monkeypatch.context() as m:
            m.setattr(app.state.services.status,'publish',Mock(side_effect=RuntimeError('collection failed')))
            result=api.post(P+'/modulators/0/stop')
            assert result.status_code == 200 and result.json()['tx_state'] == 'OFF'
            assert not app.state.services.rp.enabled[0]
            assert api.get(P+'/status').json()['system']['snapshot_stale']


def test_profile_list_bounded_reuse_and_typed_error_do_not_break_status(tmp_path,monkeypatch):
    now=[0.0]
    app=configured(tmp_path,lambda:now[0])
    with TestClient(app) as api:
        s=app.state.services
        def publish(): s.status.publish()
        with monkeypatch.context() as m:
            failure=Mock(side_effect=DomainError(422,'INVALID_PROFILE','Malformed JSON'))
            m.setattr(s.profile_files,'list',failure)
            now[0]=4
            app.state.control.executor.submit(publish).result()
            assert api.get(P+'/audio-profiles/files').status_code == 200
            assert float(api.get(P+'/audio-profiles/files').headers['X-Observation-Age-Seconds']) == 4
            failure.assert_not_called()
            now[0]=5
            app.state.control.executor.submit(publish).result()
            for _ in range(3):
                result=api.get(P+'/audio-profiles/files')
                assert result.status_code==422 and result.json()['error']=='INVALID_PROFILE'
            failure.assert_called_once()
            assert api.get(P+'/status').status_code==200


def test_validation_cache_never_substitutes_for_join_validation(tmp_path,monkeypatch):
    now=[0.0]
    app=configured(tmp_path,lambda:now[0])
    with TestClient(app) as api:
        sid=start(api)
        manager=app.state.services.sources
        with monkeypatch.context() as m:
            validation=Mock(side_effect=DomainError(404,'PROFILE_NOT_FOUND','removed'))
            m.setattr(manager,'validate_config',validation)
            # Read-only observations reuse the just-checked result for one second.
            app.state.control.executor.submit(manager.status,sid).result()
            validation.assert_not_called()
            # Joining a running Source is still a synchronous strict boundary.
            with pytest.raises(Exception,match='PROFILE_NOT_FOUND'):
                app.state.control.executor.submit(manager.acquire,sid,2).result()
            assert manager.consumers[sid]=={0,1}
            now[0]=1
            observed=app.state.control.executor.submit(manager.status,sid).result()
            assert observed.state=='ERROR' and 'PROFILE_NOT_FOUND' in observed.last_error


def test_published_rt_uses_existing_selector_and_preserves_snapshot(tmp_path):
    now = [0.0]
    app = configured(tmp_path, lambda: now[0])
    with TestClient(app) as api:
        sid = api.post(P+'/sources', json={'name':'Feed', 'type':'HTTP',
            'config':{'uri':'http://localhost/live'}}).json()['id']
        services = app.state.services
        assert api.patch(P+'/modulators/0', json={
            'input_source_id': sid, 'rds_rt': 'Fallback', 'rds_metadata_to_rt': True}).status_code == 200

        def selected_text():
            return api.get(P+'/status').json()['modulators'][0]['rds_rt_effective']

        def metadata(text):
            services.sources.status(sid).stream_title = text
            app.state.publish_status()

        assert selected_text() == 'Fallback'
        old = services.status.published_snapshot()
        app.state.control.executor.submit(metadata, 'Artist - Track').result()
        assert selected_text() == 'Artist - Track'
        assert old.modulators[0].rds_rt_effective == 'Fallback'
        assert api.put(P+'/modulators/0/rds/rt', json={
            'text': 'Override', 'ttl_s': 10}).status_code == 200
        assert selected_text() == 'Override'
        now[0] = 10.0
        app.state.control.executor.submit(app.state.publish_status).result()
        assert selected_text() == 'Artist - Track'
        # An explicit empty override must not become frontend fallback text.
        assert api.put(P+'/modulators/0/rds/rt', json={'text': ''}).status_code == 200
        assert selected_text() == ''
        assert api.delete(P+'/modulators/0/rds/rt').status_code == 204
        assert selected_text() == 'Artist - Track'
        assert api.patch(P+'/modulators/0', json={'rds_metadata_to_rt': False}).status_code == 200
        assert selected_text() == 'Fallback'
