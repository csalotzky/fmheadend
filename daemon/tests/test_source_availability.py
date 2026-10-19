"""No live sender is required for control; native construction is still required."""
from fmheadendd.tests.profile_support import profile_settings
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.errors import DependencyUnavailable, SourceUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline, RetryBackoff


SOURCES = [dict(name='Offline HTTP', type='HTTP', config={'uri': 'http://localhost/absent'}),
           dict(name='Offline UDP', type='UDP', config={'host': '127.0.0.1', 'port': 12345,
                'format': 'S16LE', 'sample_rate': 48000, 'channels': 2})]


@pytest.fixture
def native(monkeypatch):
    gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1, NULL=0),
                          StateChangeReturn=SimpleNamespace(FAILURE=-1))
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst', lambda: gst)
    return gst


class NoPcmPipeline(GstPipeline):
    """Exercise the real worker/start/stop with a constructed but silent native stub."""
    def _build(self):
        self._pipeline = SimpleNamespace(set_state=lambda state: 1)
        self._bus = SimpleNamespace(timed_pop=lambda timeout: None)
        self._convert = SimpleNamespace(get_static_pad=lambda name: SimpleNamespace(get_current_caps=lambda: None))


@pytest.mark.parametrize('body', SOURCES)
def test_offline_source_save_shared_start_resume_and_stop(native, tmp_path, body):
    with TestClient(create_app(profile_settings(tmp_path/'db'), pipeline_factory=NoPcmPipeline)) as api:
        root = '/api/v1'
        sid = api.post(root+'/sources', json=body).json()['id']
        services = api.app.state.services
        for tx in (0, 1):
            if tx == 1:
                services.sources.pipelines[sid]._update(state='ERROR', last_error='Connection refused')
            assert api.patch(root+f'/modulators/{tx}', json={'frequency_khz': 98100, 'input_source_id': sid}).status_code == 200
            response = api.post(root+f'/modulators/{tx}/start')
            assert response.status_code == 200, response.text
            assert response.json()['tx_state'] == 'ON'
            assert response.json()['source_state'] == ('CONNECTING' if tx == 0 else 'ERROR')
        assert len(services.sources.pipelines) == 1
        old = services.sources.pipelines[sid]
        assert not old._ready.is_set() and not old.probe_result().ok
        assert old.output.read(0) is None  # no invented PCM
        spies = []
        for name in ('set_tx_enabled', 'set_tx_config', 'clear_rds'):
            spy = Mock(wraps=getattr(services.rp, name))
            setattr(services.rp, name, spy)
            spies.append(spy)
        result = api.patch(root+f'/sources/{sid}', json={'input_gain_db': -3})
        assert result.status_code == 200 and result.json()['saved'] and result.json()['applied']
        assert api.get(root+f'/sources/{sid}').json()['input_gain_db'] == -3
        assert services.sources.pipelines[sid].output is old.output
        assert not old._thread.is_alive()
        assert services.sources.consumers[sid] == {0, 1}
        for spy in spies: spy.assert_not_called()
        assert api.patch(root+f'/sources/{sid}', json={'config': {}}).status_code == 422
        # The reported Save -> TX restart failure must not recur with no PCM.
        result = api.post(root+'/modulators/0/save-and-resume', json={'frequency_khz': 98300})
        assert result.status_code == 200, result.text
        assert services.modulators.statuses[0].tx_state == 'ON'
        pipeline = services.sources.pipelines[sid]
        pipeline._update(state='ERROR', last_error='No decoded audio')
        for tx in (0, 1):
            result = api.post(root+f'/modulators/{tx}/stop')
            assert result.status_code == 200 and result.json()['tx_state'] == 'OFF'
        assert not pipeline._thread.is_alive()
        assert not services.sources.pipelines and not services.sources.consumers


@pytest.mark.parametrize('body', SOURCES)
def test_offline_probe_still_requires_pcm(native, body):
    pipeline = NoPcmPipeline(InputSourceConfig(**body), reconnect=False, timeout_s=.05)
    with pytest.raises(DependencyUnavailable, match='audio|PCM'):
        pipeline.start()
    assert not pipeline._thread.is_alive() and not pipeline.probe_result().ok


@pytest.mark.parametrize('failure', [DependencyUnavailable('Missing GStreamer element: fmheadendlimiter'),
                                    ValueError('Unsupported property'), RuntimeError('broken construction')])
def test_construction_errors_are_not_source_outages(native, failure):
    class Broken(NoPcmPipeline):
        def _build(self): raise failure
    pipeline = Broken(InputSourceConfig(**SOURCES[0]), timeout_s=.2)
    with pytest.raises(DependencyUnavailable, match=str(failure)):
        pipeline.start()
    assert pipeline.fatal_error == str(failure)
    assert pipeline.snapshot()['state'] == 'ERROR'
    assert pipeline.snapshot()['error_scope'] == 'INTERNAL'
    assert not pipeline._thread.is_alive()


@pytest.mark.parametrize('permanent', [False, True])
def test_failed_playing_distinguishes_unavailable_sender_from_missing_plugin(native, permanent, caplog):
    native.MessageType = SimpleNamespace(ERROR=1, ELEMENT=2)
    native.CoreError = SimpleNamespace(quark=lambda: 'core', MISSING_PLUGIN=1)
    native.StreamError = SimpleNamespace(quark=lambda: 'stream', CODEC_NOT_FOUND=2)
    error = SimpleNamespace(message='Missing plugin' if permanent else 'Connection refused',
                            matches=lambda domain, code: permanent and domain == 'core')
    class FailedPlaying(NoPcmPipeline):
        def _build(self):
            super()._build()
            self._pipeline.set_state = lambda state: -1 if state == native.State.PLAYING else 0
            structure = SimpleNamespace(get_name=lambda: 'missing-plugin',
                to_string=lambda: 'missing-plugin, type=decoder, detail=audio/example;')
            messages = iter([SimpleNamespace(type=2, get_structure=lambda: structure),
                             SimpleNamespace(type=1, parse_error=lambda: (error, None))])
            self._bus.timed_pop_filtered = lambda timeout, kind: next(messages)
    pipeline = FailedPlaying(InputSourceConfig(**SOURCES[0]), timeout_s=.2)
    try:
        if permanent:
            with pytest.raises(DependencyUnavailable, match='Missing plugin'):
                pipeline.start()
            assert pipeline.fatal_error and not pipeline._prepared.is_set()
            assert 'type=decoder, detail=audio/example' in caplog.text
        else:
            pipeline.start()
            assert pipeline._prepared.is_set() and not pipeline.fatal_error
            assert pipeline._thread.is_alive()
    finally:
        pipeline.stop()


def test_local_missing_folder_preserves_external_failure_through_worker(native, tmp_path):
    from fmheadendd.sources.local_playback import LocalPlayback
    from fmheadendd.models.configuration import LocalConfig
    class MissingFolder(NoPcmPipeline):
        def _build(self):
            super()._build()
            self._local = LocalPlayback(LocalConfig.model_validate(self.source.config).to_core(), self._audio_ready)
            self._media_idle = self._local.initial() is None
    pipeline = MissingFolder(InputSourceConfig(name='Missing', type='LOCAL',
        config=dict(mode='folder', path=str(tmp_path/'missing'))), timeout_s=.2)
    with pytest.raises(SourceUnavailable, match='FOLDER_UNREADABLE'):
        pipeline.start()
    assert pipeline.snapshot()['error_scope'] == 'SOURCE'
    assert not pipeline._thread.is_alive()


@pytest.mark.parametrize('domain,external', [('resource', True), ('stream', True), ('core', False)])
def test_gst_error_provenance_and_debug_are_preserved(native, caplog, domain, external):
    native.CoreError = SimpleNamespace(quark=lambda: 'core', MISSING_PLUGIN=1)
    native.StreamError = SimpleNamespace(quark=lambda: 'stream', CODEC_NOT_FOUND=2)
    native.ResourceError = SimpleNamespace(quark=lambda: 'resource')
    error = SimpleNamespace(domain=domain, code=99, message='Detailed failure',
                            matches=lambda d, c: d == domain and c == 99)
    pipeline = NoPcmPipeline(InputSourceConfig(**SOURCES[0]))
    pipeline.Gst = native
    message = SimpleNamespace(src='decoder0', parse_error=lambda: (error, 'caps=audio/test; decoder detail'))
    failure = pipeline._bus_failure(message)
    assert isinstance(failure, SourceUnavailable) is external
    assert f'domain={domain} code=99' in caplog.text
    assert 'decoder0' in caplog.text and 'caps=audio/test; decoder detail' in caplog.text


def test_missing_plugin_element_details_are_logged(native, caplog):
    pipeline = NoPcmPipeline(InputSourceConfig(**SOURCES[0]))
    structure = SimpleNamespace(get_name=lambda: 'missing-plugin',
        to_string=lambda: 'missing-plugin, type=decoder, detail=audio/x-unavailable, name=Example decoder;')
    pipeline._missing_plugin_message(SimpleNamespace(get_structure=lambda: structure))
    assert 'type=decoder' in caplog.text and 'audio/x-unavailable' in caplog.text


def test_shared_permanent_failure_still_blocks_new_consumer(native, tmp_path):
    with TestClient(create_app(profile_settings(tmp_path/'db'), pipeline_factory=NoPcmPipeline)) as api:
        root = '/api/v1'
        sid = api.post(root+'/sources', json=SOURCES[0]).json()['id']
        for tx in (0, 1):
            api.patch(root+f'/modulators/{tx}', json={'frequency_khz': 98100, 'input_source_id': sid})
        assert api.post(root+'/modulators/0/start').status_code == 200
        manager = api.app.state.services.sources
        pipeline = manager.pipelines[sid]
        pipeline.fatal_error = 'Missing decoder plugin'
        pipeline._update(state='ERROR', last_error=pipeline.fatal_error)
        response = api.post(root+'/modulators/1/start')
        assert response.status_code == 503 and 'Missing decoder plugin' in response.text
        assert manager.consumers[sid] == {0}
        assert api.post(root+'/modulators/0/stop').json()['tx_state'] == 'OFF'


def test_reconnect_before_first_pcm_and_late_audio(native, monkeypatch):
    monkeypatch.setattr(RetryBackoff, 'next_delay', lambda self: 0)
    failed = threading.Event()
    retry_allowed = threading.Event()
    recovered = threading.Event()
    class LateSender(NoPcmPipeline):
        attempts = 0
        def _attempt(self):
            self.attempts += 1
            if self.attempts == 1:
                self._update(state='CONNECTING')
                self._prepared.set()
                failed.set()
                assert retry_allowed.wait(2)
                raise DependencyUnavailable('Timed out waiting for audio (source stalled)')
            # Reuse the actual drain loop on the second attempt.
            super()._attempt()
        def _build(self):
            super()._build()
            self.staging.put(b'1234')
        def _update(self, **values):
            super()._update(**values)
            if values.get('state') == 'RUNNING': recovered.set()
    pipeline = LateSender(InputSourceConfig(**SOURCES[0]))
    pipeline.output.attach(0)
    try:
        pipeline.start()
        assert failed.is_set() and not pipeline._ready.is_set()
        retry_allowed.set()
        assert recovered.wait(2)
        assert pipeline.output.read(0) == b'1234'
        assert pipeline.snapshot()['reconnect_count'] == 1
    finally:
        retry_allowed.set()
        pipeline.stop()
    assert not pipeline._thread.is_alive()


@pytest.mark.parametrize('body', SOURCES)
def test_unavailable_source_preserves_desired_on_across_restart(native, tmp_path, body):
    settings = profile_settings(tmp_path / 'restart.db')
    with TestClient(create_app(settings, pipeline_factory=NoPcmPipeline)) as api:
        sid = api.post('/api/v1/sources', json=body).json()['id']
        assert api.patch('/api/v1/modulators/0', json={
            'frequency_khz': 98100, 'input_source_id': sid}).status_code == 200
        assert api.post('/api/v1/modulators/0/start').status_code == 200
        assert api.get('/api/v1/modulators/0').json()['enabled'] is True
    with TestClient(create_app(settings, pipeline_factory=NoPcmPipeline)) as api:
        services = api.app.state.services
        api.app.state.control.executor.submit(services.status.refresh).result()
        assert services.recovery.ready and services.rp.enabled[0]
        assert not services.sources.pipelines[sid]._ready.is_set()
        assert api.get('/api/v1/modulators/0').json()['enabled'] is True
