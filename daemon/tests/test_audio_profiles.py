"""AudioProfile configuration safety; hardware-independent except native tests elsewhere."""
import json
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import AudioProfileConfig

@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(Settings(tmp_path / "test.db"))) as client:
        yield client

@pytest.mark.parametrize("processor", ["NONE", "GST_BASIC"])
def test_profile_crud_and_association(api, processor):
    response = api.post('/api/v1/audio-profiles', json={'name':'Gain','processor':processor,'gain_db':-3})
    assert response.status_code == 201
    profile = response.json()
    assert 'processing_enabled' not in profile
    pid = profile['id']
    assert api.get(f'/api/v1/audio-profiles/{pid}').json()['gain_db'] == -3
    assert api.patch(f'/api/v1/audio-profiles/{pid}', json={'gain_db':-6}).json()['gain_db'] == -6
    source = api.post('/api/v1/sources', json={'name':'Feed','type':'HTTP','config':{'uri':'http://localhost/feed'},'audio_profile_id':pid}).json()
    assert source['audio_profile_id'] == pid
    assert api.delete(f'/api/v1/audio-profiles/{pid}').status_code == 409
    assert api.delete(f"/api/v1/sources/{source['id']}").status_code == 204
    assert api.delete(f'/api/v1/audio-profiles/{pid}').status_code == 204


def test_defaults_and_validation(api):
    p = api.post('/api/v1/audio-profiles', json={'name':'Default'}).json()
    assert p['processor'] == 'NONE' and p['gain_db'] == 0
    for bad in ({'gain_db':20.1}, {'processor':'GSTREAMER'}, {'processing_enabled':False}, {'processor':'LSP'}):
        assert api.post('/api/v1/audio-profiles', json={'name':'Bad',**bad}).status_code == 422


def test_active_profile_update_policy(api):
    pid = api.post('/api/v1/audio-profiles', json={'name':'Gain'}).json()['id']
    sid = api.post('/api/v1/sources', json={'name':'Feed','type':'HTTP','config':{'uri':'http://localhost/feed'},'audio_profile_id':pid}).json()['id']
    services = api.app.state.services
    for tx in (0, 1):
        assert api.patch(f'/api/v1/modulators/{tx}', json={'input_source_id':sid,'frequency_khz':98100}).status_code == 200
        assert api.post(f'/api/v1/modulators/{tx}/start').status_code == 200
    pipeline = services.sources.pipelines[sid]
    before = api.get(f'/api/v1/audio-profiles/{pid}').json()
    try:
        for patch in ({'name':'Renamed'}, {'gain_db':-3}, {'processor':'GST_BASIC'}, {'eq_config':{'x':1}}, {}):
            assert api.patch(f'/api/v1/audio-profiles/{pid}', json=patch).status_code == 409
        assert api.get(f'/api/v1/audio-profiles/{pid}').json()['gain_db'] == 0
        assert api.get(f'/api/v1/audio-profiles/{pid}').json() == before
        assert api.post('/api/v1/audio-profiles', json={'name':'New while active'}).status_code == 201
        assert services.sources.pipelines[sid] is pipeline
        assert services.sources.consumers[sid] == {0,1}
        assert api.post('/api/v1/modulators/0/stop').status_code == 200
        assert api.patch(f'/api/v1/audio-profiles/{pid}', json={'name':'Still blocked'}).status_code == 409
    finally:
        for tx in (0, 1):
            assert api.post(f'/api/v1/modulators/{tx}/stop').status_code == 200
    assert api.patch(f'/api/v1/audio-profiles/{pid}', json={'name':'Stopped', 'gain_db':-3}).status_code == 200

@pytest.mark.parametrize('enabled,processor', [(False,'NONE'),(True,'GST_BASIC')])
def test_legacy_read_normalization_and_reopen(tmp_path, enabled, processor):
    path = tmp_path/'legacy.db'
    db = Database(path)
    p = db.save('audio_profiles', AudioProfileConfig(name='Legacy',gain_db=-3))
    with db.connection:
        raw = json.loads(db.connection.execute('SELECT data FROM audio_profiles WHERE id=?',(p.id,)).fetchone()[0])
        raw.update(processing_enabled=enabled,processor='GSTREAMER')
        db.connection.execute('UPDATE audio_profiles SET data=? WHERE id=?',(json.dumps(raw),p.id))
    db.close()
    for _ in range(2):
        db = Database(path)
        profile = db.get('audio_profiles',p.id)
        assert profile.processor == processor and profile.gain_db == -3
        assert db.list('audio_profiles')[0] == profile
        assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 9
        db.save('audio_profiles',profile,p.id)
        saved = json.loads(db.connection.execute('SELECT data FROM audio_profiles WHERE id=?',(p.id,)).fetchone()[0])
        assert 'processing_enabled' not in saved
        db.close()
