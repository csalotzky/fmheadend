"""File selection -> existing source ownership/pipeline/mux contract."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.tests.profile_support import BUNDLED_PROFILES as DEFAULT, profile_settings
from fmheadendd.database import Database
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.output import PcmOutput
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.tests.test_audio_profile_files import document

BASE = '/api/v1/audio-profiles/files/'
SOURCE = {'name': 'Feed', 'type': 'HTTP', 'config': {'uri': 'http://localhost/live'}}


class FakePipeline:
    mocked = False
    fail_start = False
    fail_stop = False
    @staticmethod
    def validate_profile(profile): pass
    def __init__(self, source, *, profile=None, output=None, **kwargs):
        self.source, self.profile = source, profile
        self.output = output or PcmOutput()
        self.running = False
    def start(self):
        if self.fail_start: raise DependencyUnavailable('audio start failed')
        self.running = True
    def stop(self):
        if self.fail_stop: raise DependencyUnavailable('audio stop failed')
        self.running = False
        self.output.clear()
    def snapshot(self):
        return {'state': 'RUNNING' if self.running else 'ERROR', 'last_error': None}

@pytest.fixture
def api(tmp_path):
    settings = profile_settings(tmp_path / 'db')
    with TestClient(create_app(settings, pipeline_factory=FakePipeline)) as client:
        yield client

def source(api, **values):
    response = api.post('/api/v1/sources', json={**SOURCE, **values})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_corrupt_selected_profile_does_not_hide_catalog_or_change_references(api):
    created = api.post(BASE + 'custom', json=document()).json()
    path = created['path']
    sid = source(api, use_default_audio_profile=False, audio_profile_id=path)
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': path}).status_code == 200
    before_source = api.get(f'/api/v1/sources/{sid}').json()
    before_system = api.get('/api/v1/system/config').json()
    services = api.app.state.services
    def corrupt_and_publish():
        services.profile_files._path(path).write_text('{bad', encoding='utf-8')
        services.status.publish(configuration_changed=True)
    api.app.state.control.executor.submit(corrupt_and_publish).result()
    result = api.get(BASE.rstrip('/'))
    assert result.status_code == 200
    assert 'default/basic_filtering.json' in {item['path'] for item in result.json()}
    assert path not in {item['path'] for item in result.json()}
    assert api.get(BASE + path).status_code == 422
    assert api.get(f'/api/v1/sources/{sid}').json() == before_source
    assert api.get('/api/v1/system/config').json() == before_system
    assert api.get('/api/v1/status').status_code == 200
    logs = api.get('/api/v1/system/logs').json()
    assert any(path in str(row) and 'INVALID_PROFILE' in str(row) for row in logs)


def test_modular_editor_metadata_uses_existing_api_family(api):
    result=api.get(BASE+'dsp')
    assert result.status_code==200
    data=result.json()
    assert data['template']['schema_version']==1 and len(data['stages'])==12
    assert set(data['implementations']) == {'VOLUME','TRIM','CHEBYSHEV','LSP_MULTIBAND_EXPANDER','LSP_MULTIBAND_GATE','LSP_AGC','LSP_EQ','LSP_COMPRESSOR','LSP_MULTIBAND','LSP_LIMITER','FM_HEADEND_LIMITER','FM_HEADEND_CLIPPER'}


@pytest.mark.parametrize('weighting', [0, 50, 75])
def test_weighting_api_roundtrip(api, weighting):
    data = document()
    data['stages']['limiter'] = dict(selected='FM_HEADEND_LIMITER', implementations={'FM_HEADEND_LIMITER': {'weighting_us': weighting}})
    created = api.post(BASE+'custom', json=data)
    assert created.status_code == 201
    path = created.json()['path']
    loaded = api.get(BASE+path).json()['profile']
    assert loaded['stages']['limiter']['implementations']['FM_HEADEND_LIMITER']['weighting_us'] == weighting
    saved = api.put(BASE+path, json=loaded)
    assert saved.status_code == 200 and saved.json()['profile'] == loaded

def start(api, sid, tx):
    assert api.patch(f'/api/v1/modulators/{tx}', json={'input_source_id': sid, 'frequency_khz': 98100}).status_code == 200
    response = api.post(f'/api/v1/modulators/{tx}/start')
    assert response.status_code == 200, response.text


def test_source_roundtrip_shared_profile_and_reference_guard(api):
    profile = api.post(BASE + 'custom', json=document()).json()
    path = profile['path']
    ids = [source(api, use_default_audio_profile=False, input_gain_db=gain, audio_profile_id=path) for gain in (-3, -6)]
    for sid, gain in zip(ids, (-3, -6)):
        row = api.get(f'/api/v1/sources/{sid}').json()
        assert row['audio_profile_id'] == path and row['input_gain_db'] == gain
        assert row['use_default_audio_profile'] is False and 'processor_mode' not in row
    assert api.delete(BASE + path).status_code == 409
    for sid in ids:
        assert api.patch(f'/api/v1/sources/{sid}', json={'use_default_audio_profile': True, 'audio_profile_id': None}).status_code == 200
    assert api.delete(BASE + path).status_code == 204


def test_save_restarts_shared_audio_only_and_preserves_pcm_identity(api):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    a = source(api, use_default_audio_profile=False, audio_profile_id=path, input_gain_db=-6)
    b = source(api, use_default_audio_profile=False, audio_profile_id=path)
    unrelated = source(api)
    for tx, sid in enumerate((a, a, b, unrelated)): start(api, sid, tx)
    services = api.app.state.services
    before = dict(services.sources.pipelines)
    rp_methods = ['set_tx_enabled', 'set_tx_config', 'clear_rds']
    spies = []
    for name in rp_methods:
        spy = Mock(wraps=getattr(services.rp, name))
        setattr(services.rp, name, spy)
        spies.append(spy)
    data = document()
    data['stages']['input_gain']['implementations']['VOLUME']['gain_db'] = -3
    result = api.put(BASE + path, json=data)
    assert result.status_code == 200 and result.json()['applied'] is True
    for sid in (a, b):
        current = services.sources.pipelines[sid]
        assert current is not before[sid] and not before[sid].running
        assert current.output is before[sid].output
        assert current.profile.stages['input_gain'].implementations['VOLUME']['gain_db'] == -3
    assert services.sources.consumers[a] == {0, 1}
    assert services.sources.pipelines[unrelated] is before[unrelated]
    for spy in spies: spy.assert_not_called()
    assert all(api.get(f'/api/v1/modulators/{tx}').json()['enabled'] for tx in range(4))
    # STOP ALL holds stay intact on a later file save.
    api.post('/api/v1/modulators/stop-all')
    held = set(services.modulators.bulk_stopped)
    for spy in spies: spy.reset_mock()
    assert api.put(BASE + path, json=data).json()['applied']
    assert services.modulators.bulk_stopped == held
    for spy in spies: spy.assert_not_called()


@pytest.mark.parametrize('failure', ['start', 'stop'])
def test_saved_but_restart_failed_keeps_file_and_ownership(api, failure):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    sid = source(api, use_default_audio_profile=False, audio_profile_id=path)
    start(api, sid, 0)
    manager = api.app.state.services.sources
    old = manager.pipelines[sid]
    if failure == 'stop': old.fail_stop = True
    else: FakePipeline.fail_start = True
    try:
        result = api.put(BASE + path, json={**document(), 'name': 'Saved'}).json()
        assert result['saved'] and not result['applied']
        assert str(sid) in result['restart_errors']
        assert api.get(BASE + path).json()['name'] == 'Saved'
        assert manager.consumers[sid] == {0}
        assert api.get(f'/api/v1/sources/{sid}/status').json()['state'] == 'ERROR'
        assert api.app.state.services.modulators.statuses[0].tx_state == 'ON'
        if failure == 'stop': assert manager.pipelines[sid] is old
    finally:
        old.fail_stop = False
        FakePipeline.fail_start = False
    assert api.put(BASE + path, json=document()).json()['applied']


def test_plugin_preflight_precedes_save(api, monkeypatch):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    def missing(profile): raise DependencyUnavailable('Missing GStreamer element: audiocheblimit')
    monkeypatch.setattr(FakePipeline, 'validate_profile', missing)
    response = api.put(BASE + path, json={**document(), 'name': 'Not saved'})
    assert response.status_code == 503 and response.json()['error'] == 'PROFILE_PLUGIN_UNAVAILABLE'
    assert api.get(BASE + path).json()['name'] == 'Test filters'


def test_missing_invalid_profile_fail_closed_and_bypass_preserves_gain(api):
    assert api.post('/api/v1/sources', json={**SOURCE, 'use_default_audio_profile': False, 'audio_profile_id': 'custom/missing.json'}).status_code == 404
    path = api.post(BASE + 'custom', json=document()).json()['path']
    sid = source(api, use_default_audio_profile=False, audio_profile_id=path)
    api.patch('/api/v1/modulators/0', json={'input_source_id': sid, 'frequency_khz': 98100})
    api.app.state.services.profile_files._path(path).write_text('{bad')
    assert api.post('/api/v1/modulators/0/start').status_code == 503
    assert not api.app.state.services.sources.consumers
    assert api.post('/api/v1/sources', json={**SOURCE, 'processor_mode': 'bypass'}).status_code == 422
    bypass = source(api, input_gain_db=-9, use_default_audio_profile=False, audio_profile_id='default/bypass.json')
    start(api, bypass, 2)
    stages = api.app.state.services.sources.pipelines[bypass].profile.stages
    assert all(s.selected == 'OFF' for k, s in stages.items() if k not in ('input_gain', 'master_gain', 'output_trim'))
    assert api.app.state.services.sources.pipelines[bypass].source.input_gain_db == -9


def test_invalid_running_profile_reports_source_only(api):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    a = source(api, use_default_audio_profile=False, audio_profile_id=path)
    b = source(api)
    start(api, a, 0); start(api, b, 1)
    api.app.state.services.profile_files._path(path).unlink()
    # External file edits are observed at the bounded validation/publication cadence.
    manager = api.app.state.services.sources
    now = manager.clock()
    manager.clock = lambda: now + 2
    api.app.state.control.executor.submit(api.app.state.publish_status).result()
    assert api.get(f'/api/v1/sources/{a}/status').json()['state'] == 'ERROR'
    assert api.get(f'/api/v1/sources/{b}/status').json()['state'] == 'RUNNING'
    api.patch('/api/v1/modulators/2', json={'input_source_id': a, 'frequency_khz': 98900})
    assert api.post('/api/v1/modulators/2/start').status_code == 503
    assert api.app.state.services.sources.consumers[a] == {0}
    assert api.app.state.services.sources.pipelines[a].running


@pytest.mark.parametrize('builtin', [False, True])
def test_actual_gst_builder_gains_and_json_stages(tmp_path, builtin):
    files = AudioProfileFiles(DEFAULT, tmp_path)
    doc = document()
    doc['stages']['input_gain']['implementations']['VOLUME']['gain_db'] = -3
    doc['stages']['hpf']['implementations']['CHEBYSHEV']['cutoff_hz'] = 80
    saved = files.create(doc)
    db = Database(':memory:')
    source = db.save('sources', InputSourceConfig(**SOURCE, input_gain_db=-6,
        use_default_audio_profile=False, audio_profile_id=saved.path if builtin else 'default/bypass.json'))
    manager = SourceManager(db, GstPipeline, files)
    pipeline = manager._new_pipeline(source)
    class Element:
        def __init__(self, factory, props): self.factory, self.props = factory, props
        def connect(self, *args): pass
    elements, links = [], []
    def make(factory, **props):
        el = Element(factory, props); elements.append(el); return el
    pipeline.Gst = SimpleNamespace(Pipeline=SimpleNamespace(new=lambda unused: SimpleNamespace(get_bus=lambda: None)),
                                   Caps=SimpleNamespace(from_string=lambda value: value))
    pipeline._make = make
    pipeline._link = lambda *args: links.append(args)
    pipeline._build()
    gains = [el.props['volume'] for el in elements if el.factory == 'volume']
    assert gains == pytest.approx([10 ** (-6/20)] + ([10 ** (-3/20), 1, 1] if builtin else [1, 1, 1]))
    if builtin:
        assert next(e for e in elements if e.factory == 'audiocheblimit').props['cutoff'] == 80
        assert [e.factory for e in links[0]][3:7] == ['level', 'volume', 'volume', 'audiocheblimit']
    else:
        assert not any(e.factory in ('audiocheblimit', 'lsp-plug-in-plugins-lv2-limiter-stereo') for e in elements)
    db.close()


@pytest.mark.parametrize('count', [1, 3])
def test_source_hot_save_replaces_one_pipeline_preserving_all_consumers(api, count):
    sid, other = source(api), source(api)
    for tx in range(count): start(api, sid, tx)
    start(api, other, 7)
    services = api.app.state.services
    manager = services.sources
    before = dict(manager.pipelines)
    create = Mock(wraps=manager._new_pipeline)
    manager._new_pipeline = create
    spies = []
    for owner, names in [(services.rp, ('set_tx_enabled', 'set_tx_config', 'clear_rds')),
                         (services.rds, ('stop', 'reset_channel', 'suspend', 'fence')),
                         (services.modulators, ('start', 'stop'))]:
        for name in names:
            spy = Mock(wraps=getattr(owner, name)); setattr(owner, name, spy); spies.append(spy)
    response = api.patch(f'/api/v1/sources/{sid}', json={'input_gain_db': -7.125,
        'config': {'uri': 'http://localhost/changed'}})
    assert response.status_code == 200 and response.json()['saved'] and response.json()['applied']
    assert create.call_count == 1
    assert manager.pipelines[sid] is not before[sid] and not before[sid].running
    assert manager.pipelines[sid].output is before[sid].output
    assert manager.pipelines[sid].source.input_gain_db == -7.125
    assert manager.pipelines[other] is before[other]
    assert manager.consumers[sid] == set(range(count))
    for spy in spies: spy.assert_not_called()
    for tx in range(count):
        assert api.get(f'/api/v1/modulators/{tx}').json()['enabled']
        assert services.modulators.statuses[tx].tx_state == 'ON'


@pytest.mark.parametrize('failure', ['start', 'stop'])
def test_source_saved_but_apply_failed_retains_desired_and_worker_ownership(api, failure):
    sid = source(api)
    start(api, sid, 0); start(api, sid, 1)
    services = api.app.state.services
    manager = services.sources
    old = manager.pipelines[sid]
    create = Mock(wraps=manager._new_pipeline); manager._new_pipeline = create
    spies = [Mock(wraps=getattr(services.rp, name)) for name in ('set_tx_enabled', 'set_tx_config', 'clear_rds')]
    for name, spy in zip(('set_tx_enabled', 'set_tx_config', 'clear_rds'), spies): setattr(services.rp, name, spy)
    if failure == 'stop': old.fail_stop = True
    else: FakePipeline.fail_start = True
    try:
        result = api.patch(f'/api/v1/sources/{sid}', json={'input_gain_db': -9}).json()
        assert result['saved'] and not result['applied'] and 'audio' in result['apply_error']
        assert api.get(f'/api/v1/sources/{sid}').json()['input_gain_db'] == -9
        assert manager.consumers[sid] == {0, 1}
        assert manager.pipelines[sid].output is old.output
        assert api.get(f'/api/v1/sources/{sid}/status').json()['state'] == 'ERROR'
        assert all(services.modulators.statuses[tx].tx_state == 'ON' for tx in (0, 1))
        if failure == 'stop':
            assert manager.pipelines[sid] is old
            create.assert_not_called()
        for spy in spies: spy.assert_not_called()
    finally:
        old.fail_stop = False; FakePipeline.fail_start = False
    # Retrying the same saved configuration must apply it, not falsely report
    # success merely because no persistent field changed this time.
    assert api.patch(f'/api/v1/sources/{sid}', json={'input_gain_db': -9}).json()['applied']
    assert manager.pipelines[sid].running


def test_source_idle_name_only_and_stop_all_hold_do_not_start_audio(api):
    sid = source(api)
    manager = api.app.state.services.sources
    assert api.patch(f'/api/v1/sources/{sid}', json={'input_gain_db': -4}).json()['applied']
    assert not manager.pipelines
    start(api, sid, 0)
    old = manager.pipelines[sid]
    assert api.patch(f'/api/v1/sources/{sid}', json={'name': 'Rename only'}).json()['applied']
    assert manager.pipelines[sid] is old and old.running
    api.post('/api/v1/modulators/stop-all')
    services = api.app.state.services
    held = set(services.modulators.bulk_stopped)
    spy = Mock(wraps=services.rp.set_tx_enabled); services.rp.set_tx_enabled = spy
    assert api.patch(f'/api/v1/sources/{sid}', json={'input_gain_db': -5}).json()['applied']
    assert services.modulators.bulk_stopped == held
    assert not manager.pipelines
    assert api.get('/api/v1/modulators/0').json()['enabled']
    spy.assert_not_called()


def test_source_invalid_edit_does_not_save_or_replace_audio(api):
    sid = source(api); start(api, sid, 0)
    old = api.app.state.services.sources.pipelines[sid]
    response = api.patch(f'/api/v1/sources/{sid}', json={'config': {'uri': 'not a URL'}})
    assert response.status_code == 422
    assert api.app.state.services.sources.pipelines[sid] is old and old.running
    assert api.get(f'/api/v1/sources/{sid}').json()['config']['uri'] == SOURCE['config']['uri']


def test_dynamic_default_reuses_shared_restart_without_rewriting_sources(api):
    import sqlite3
    path = api.post(BASE + 'custom', json=document()).json()['path']
    inherited, another, idle = source(api), source(api), source(api)
    fixed = source(api, use_default_audio_profile=False, audio_profile_id='default/basic_filtering.json')
    for tx, sid in enumerate((inherited, inherited, another, fixed)):
        start(api, sid, tx)
    manager = api.app.state.services.sources
    before = dict(manager.pipelines)
    records = api.get('/api/v1/sources').json()
    assert records[0]['use_default_audio_profile'] is True
    assert records[0]['audio_profile_id'] is None
    assert api.get('/api/v1/system/config').json()['default_audio_profile_id'] == 'default/basic_filtering.json'
    restart = Mock(wraps=manager._restart_audio)
    manager._restart_audio = restart
    response = api.patch('/api/v1/system/config', json={'default_audio_profile_id': path})
    assert response.status_code == 200 and response.json()['applied']
    assert restart.call_count == 2
    for sid in (inherited, another):
        assert manager.pipelines[sid] is not before[sid]
        assert manager.pipelines[sid].output is before[sid].output
        assert manager.pipelines[sid].audio_profile_id == path
    assert manager.consumers[inherited] == {0, 1}
    assert manager.pipelines[fixed] is before[fixed]
    assert idle not in manager.pipelines
    assert api.get('/api/v1/sources').json() == records
    db_path = api.app.state.control.executor.submit(
        lambda: manager.database.connection.execute('PRAGMA database_list').fetchone()[2]).result()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT json_extract(data, '$.default_audio_profile_id') FROM system_config").fetchone()[0] == path
    assert next(p for p in api.get(BASE.rstrip('/')).json() if p['path'] == path)['is_system_default']
    assert api.delete(BASE + path).status_code == 409
    restart.reset_mock()
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': path}).json()['applied']
    restart.assert_not_called()
    assert api.put(BASE + path, json={**document(), 'name': 'Edited default'}).json()['applied']
    assert restart.call_count == 2
    assert manager.pipelines[inherited].profile.name == 'Edited default'
    assert manager.pipelines[fixed] is before[fixed]
    api.post('/api/v1/modulators/stop-all')
    restart.reset_mock()
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': 'default/bypass.json'}).json()['applied']
    restart.assert_not_called()
    assert not manager.pipelines


def test_default_validation_and_failed_restart_are_not_false_success(api):
    sid = source(api)
    start(api, sid, 0)
    manager = api.app.state.services.sources
    old = manager.pipelines[sid]
    before = api.get('/api/v1/system/config').json()
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': 'custom/missing.json'}).status_code == 404
    assert api.get('/api/v1/system/config').json() == before
    old.fail_stop = True
    try:
        for _ in range(2):
            result = api.patch('/api/v1/system/config', json={'default_audio_profile_id': 'default/bypass.json'}).json()
            assert not result['applied'] and str(sid) in result['restart_errors']
            assert result['default_audio_profile_id'] == 'default/bypass.json'
            assert manager.pipelines[sid] is old
    finally:
        old.fail_stop = False
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': 'default/bypass.json'}).json()['applied']


def test_default_reference_is_protected_even_without_sources(api):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': path}).json()['applied']
    assert api.delete(BASE + path).status_code == 409
    assert api.patch('/api/v1/system/config', json={'default_audio_profile_id': 'default/basic_filtering.json'}).json()['applied']
    assert api.delete(BASE + path).status_code == 204


@pytest.mark.parametrize('weighting', [0,50,75])
def test_clipper_api_roundtrip_preserves_other_stages(api, weighting):
    data = document()
    original = {k:v for k,v in data['stages'].items() if k != 'clipper'}
    data['stages']['clipper'] = dict(selected='FM_HEADEND_CLIPPER', implementations={
        'FM_HEADEND_CLIPPER': dict(threshold_dbfs=-12., weighting_us=weighting, bypass=False,
                                 clipping_mode='soft', soft_knee_db=4.5, hf_correction_us=0)})
    response = api.post(BASE+'custom',json=data)
    assert response.status_code == 201, response.text
    path = response.json()['path']
    loaded = api.get(BASE+path).json()['profile']
    assert loaded == data
    loaded['stages']['clipper']['selected'] = 'OFF'
    assert api.put(BASE+path,json=loaded).status_code == 200
    saved = api.get(BASE+path).json()['profile']
    assert saved == loaded
    assert {k:v for k,v in saved['stages'].items() if k != 'clipper'} == original
