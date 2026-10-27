"""Polling is a reader of bounded publications, never audio/control work."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock, patch

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
                                 '/system/logs']
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
        assert stale.system.snapshot_stale and stale.system.controller_observation_stale
        assert stale.system.controller_observation_age_s == old.system.controller_observation_age_s + 3
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


@pytest.mark.parametrize('active', [False, True])
def test_periodic_control_reads_execute_no_sql(tmp_path, active):
    now = [0.0]
    app = configured(tmp_path, lambda: now[0])
    with TestClient(app) as api:
        sid = start(api)
        if not active:
            assert api.post(P+'/modulators/stop-all').status_code == 200

        def check():
            services = app.state.services
            services.status.refresh()
            services.rt.effective(0)
            queries = []
            services.database.connection.set_trace_callback(queries.append)
            try:
                for _ in range(20):
                    now[0] += .11
                    services.rt.expire()
                    services.rds.tick()
                    services.status.refresh()
                assert queries == []
                assert services.status.published_source(sid).state == ('RUNNING' if active else 'IDLE')
            finally:
                services.database.connection.set_trace_callback(None)

        app.state.control.executor.submit(check).result(timeout=10)


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


def test_profile_listing_is_on_demand_and_failure_does_not_break_status(tmp_path,monkeypatch):
    now=[0.0]
    app=configured(tmp_path,lambda:now[0])
    with TestClient(app) as api:
        s=app.state.services
        def publish(): s.status.publish()
        with monkeypatch.context() as m:
            failure=Mock(side_effect=DomainError(422,'INVALID_PROFILE','Malformed JSON'))
            m.setattr(s.profile_files,'list',failure)
            m.setattr(s.profile_files, 'get', Mock(side_effect=AssertionError('idle profile read')))
            m.setattr(s.profile_files, 'validate', Mock(side_effect=AssertionError('idle validation')))
            for elapsed in (1, 5, 60, 3600):
                now[0] = elapsed
                app.state.control.executor.submit(publish).result()
            failure.assert_not_called()
            for _ in range(3):
                result=api.get(P+'/audio-profiles/files')
                assert result.status_code==422 and result.json()['error']=='INVALID_PROFILE'
            assert failure.call_count == 3
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


def test_publication_reuses_inventory_and_observations_for_radiotext(tmp_path):
    app = configured(tmp_path, lambda: 0.0)
    with TestClient(app) as api:
        sid = start(api)
        assert api.patch(f'{P}/sources/{sid}', json={'rds_profile': {
            'rds_rt': 'Source fallback', 'rds_metadata_to_rt': True}}).status_code == 200
        services = app.state.services

        def check_publications():
            # Two running TXes and one off TX share the same source and metadata.
            for tx in (0, 1, 2):
                config = services.database.get('modulators', tx)
                services.database.save('modulators', config.model_copy(update={
                    'input_source_id': sid, 'rds_follow_source': True}), tx)
            pipeline = services.sources.pipelines[sid]
            observed = {'state': 'RUNNING', 'stream_title': 'First track'}
            pcm = {'staging': {'accepted_bytes': 100}, 'consumers': {
                0: {'accepted_bytes': 100}, 1: {'accepted_bytes': 100}}}
            with patch.object(pipeline, 'snapshot', side_effect=lambda: dict(observed)), \
                    patch.object(pipeline, 'pcm_snapshot', create=True, side_effect=lambda: pcm):
                services.status.publish()  # Load this committed configuration generation.
                previous = None
                for title, accepted in [('First track', 100), ('Next track', 200)]:
                    observed['stream_title'] = title
                    pcm['staging']['accepted_bytes'] = accepted
                    with patch.object(services.database, 'get', wraps=services.database.get) as get, \
                            patch.object(services.database, 'effective_rds_profile', wraps=services.database.effective_rds_profile) as resolve, \
                            patch.object(services.sources, 'status', wraps=services.sources.status) as status, \
                            patch.object(services.rt, 'effective', wraps=services.rt.effective) as effective:
                        queries = []
                        services.database.connection.set_trace_callback(queries.append)
                        try:
                            services.status.publish()
                        finally:
                            services.database.connection.set_trace_callback(None)
                        assert queries == []
                        get.assert_not_called()
                        resolve.assert_not_called()
                        status.assert_called_once()
                        assert status.call_args.args == (sid,) and 'source' in status.call_args.kwargs
                        assert effective.call_count == 8
                    generation = services.status.published_snapshot()
                    source = services.status.published_source(sid)
                    assert source.stream_title == title and source.pcm.staging.accepted_bytes == accepted
                    assert [r.rds_rt_effective for r in generation.modulators[:3]] == [title] * 3
                    assert generation.modulators[2].tx_state == 'OFF'
                    assert generation.modulators[0].pcm_staging.accepted_bytes == accepted
                    if previous is not None:
                        old_generation, old_source = previous
                        assert old_generation.modulators[0].rds_rt_effective == old_source.stream_title == 'First track'
                        assert old_generation.modulators[0].pcm_staging.accepted_bytes == old_source.pcm.staging.accepted_bytes == 100
                    previous = generation, source

        # Scope spies to one owner-thread job, excluding unrelated maintenance.
        app.state.control.executor.submit(check_publications).result(timeout=10)


def test_publication_loads_shared_dsp_profile_once_per_due_generation(tmp_path):
    now = [0.0]
    app = configured(tmp_path, lambda: now[0])
    with TestClient(app) as api:
        for tx in range(4):
            sid = api.post(P+'/sources', json={'name': f'Feed {tx}', 'type': 'HTTP',
                'config': {'uri': 'http://localhost/live'}, 'use_default_audio_profile': False,
                'audio_profile_id': 'default/light_normal.json'}).json()['id']
            assert api.patch(f'{P}/modulators/{tx}', json={
                'input_source_id': sid, 'frequency_khz': 98100}).status_code == 200
            assert api.post(f'{P}/modulators/{tx}/start').status_code == 200
        services = app.state.services

        def check():
            with patch.object(services.profile_files, 'get', wraps=services.profile_files.get) as get, \
                    patch.object(services.profile_files, 'validate', wraps=services.profile_files.validate) as validate:
                for generation in (1, 2):
                    now[0] = float(generation)
                    services.status.publish()
                    assert get.call_count == validate.call_count == generation
                    assert all(row.source_state == 'RUNNING'
                               for row in services.status.published_snapshot().modulators[:4])
                assert all(c.args == ('default/light_normal.json',) for c in get.call_args_list)

        app.state.control.executor.submit(check).result(timeout=10)
