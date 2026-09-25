"""File selection -> existing source ownership/pipeline/mux contract."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.output import PcmOutput
from fmheadendd.sources.gst_pipeline import GstPipeline

DEFAULT = Path(__file__).resolve().parents[2] / 'audio-profiles/default'
BASE = '/api/v1/audio-profiles/files/'
SOURCE = {'name': 'Feed', 'type': 'HTTP', 'config': {'uri': 'http://localhost/live'}}

def document():
    return json.loads((DEFAULT / 'basic.json').read_text())

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
    settings = Settings(tmp_path / 'db', audio_profiles_default_dir=DEFAULT,
                        audio_profiles_custom_dir=tmp_path / 'custom')
    with TestClient(create_app(settings, pipeline_factory=FakePipeline)) as client:
        yield client

def source(api, **values):
    response = api.post('/api/v1/sources', json={**SOURCE, **values})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_modular_editor_metadata_uses_existing_api_family(api):
    result=api.get(BASE+'dsp')
    assert result.status_code==200
    data=result.json()
    assert data['template']['schema_version']==2 and len(data['stages'])==11
    assert data['implementations']['SINC']['available'] is False
    assert 'sinc tests' in data['implementations']['SINC']['reason']

def start(api, sid, tx):
    assert api.patch(f'/api/v1/modulators/{tx}', json={'input_source_id': sid, 'frequency_khz': 98100}).status_code == 200
    response = api.post(f'/api/v1/modulators/{tx}/start')
    assert response.status_code == 200, response.text


def test_source_roundtrip_shared_profile_and_reference_guard(api):
    profile = api.post(BASE + 'custom', json=document()).json()
    path = profile['path']
    ids = [source(api, processor_mode='builtin', input_gain_db=gain, audio_profile_path=path) for gain in (-3, -6)]
    for sid, gain in zip(ids, (-3, -6)):
        row = api.get(f'/api/v1/sources/{sid}').json()
        assert row['audio_profile_path'] == path and row['input_gain_db'] == gain
        assert 'audio_profile_id' not in row
    assert api.delete(BASE + path).status_code == 409
    for sid in ids:
        assert api.patch(f'/api/v1/sources/{sid}', json={'processor_mode': 'bypass', 'audio_profile_path': None}).status_code == 200
    assert api.delete(BASE + path).status_code == 204


def test_save_restarts_shared_audio_only_and_preserves_pcm_identity(api):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    a = source(api, processor_mode='builtin', audio_profile_path=path, input_gain_db=-6)
    b = source(api, processor_mode='builtin', audio_profile_path=path)
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
    data['stages']['gain']['implementations']['GST_VOLUME']['gain_db'] = -3
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
    sid = source(api, processor_mode='builtin', audio_profile_path=path)
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
    def missing(profile): raise DependencyUnavailable('Missing GStreamer element: rglimiter')
    monkeypatch.setattr(FakePipeline, 'validate_profile', missing)
    response = api.put(BASE + path, json={**document(), 'name': 'Not saved'})
    assert response.status_code == 503 and response.json()['error'] == 'PROFILE_PLUGIN_UNAVAILABLE'
    assert api.get(BASE + path).json()['name'] == 'GST Basic'


def test_missing_invalid_stereotool_fail_closed(api):
    assert api.post('/api/v1/sources', json={**SOURCE, 'processor_mode': 'builtin', 'audio_profile_path': 'custom/missing.json'}).status_code == 404
    path = api.post(BASE + 'custom', json=document()).json()['path']
    sid = source(api, processor_mode='builtin', audio_profile_path=path)
    api.patch('/api/v1/modulators/0', json={'input_source_id': sid, 'frequency_khz': 98100})
    api.app.state.services.profile_files._path(path).write_text('{bad')
    assert api.post('/api/v1/modulators/0/start').status_code == 503
    assert not api.app.state.services.sources.consumers
    stereo = source(api, processor_mode='stereotool', audio_profile_path='custom/test.sts')
    api.patch('/api/v1/modulators/1', json={'input_source_id': stereo, 'frequency_khz': 98300})
    response = api.post('/api/v1/modulators/1/start')
    assert response.status_code == 503 and 'not implemented' in response.json()['message']
    bypass = source(api, input_gain_db=-9)
    start(api, bypass, 2)
    assert api.app.state.services.sources.pipelines[bypass].profile is None
    assert api.app.state.services.sources.pipelines[bypass].source.input_gain_db == -9


def test_invalid_running_profile_reports_source_only(api):
    path = api.post(BASE + 'custom', json=document()).json()['path']
    a = source(api, processor_mode='builtin', audio_profile_path=path)
    b = source(api)
    start(api, a, 0); start(api, b, 1)
    api.app.state.services.profile_files._path(path).unlink()
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
    doc['stages']['gain']['implementations']['GST_VOLUME']['gain_db'] = -3
    doc['stages']['processor']['implementations']['GST_BASIC']['highpass_hz'] = 80
    saved = files.create(doc)
    db = Database(':memory:')
    source = db.save('sources', InputSourceConfig(**SOURCE, input_gain_db=-6,
        processor_mode='builtin' if builtin else 'bypass', audio_profile_path=saved.path if builtin else None))
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
    assert gains == pytest.approx([10 ** (-6/20)] + ([10 ** (-3/20), 1, 1] if builtin else []))
    if builtin:
        assert next(e for e in elements if e.factory == 'audiocheblimit').props['cutoff'] == 80
        assert [e.factory for e in links[0]][3:6] == ['volume', 'volume', 'audiocheblimit']
    else:
        assert not any(e.factory in ('audiocheblimit', 'equalizer-3bands', 'audiodynamic', 'rglimiter') for e in elements)
    db.close()
