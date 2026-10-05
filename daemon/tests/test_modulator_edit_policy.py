"""Configuration is separate from lifecycle for every PATCH client."""
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.rp2354.protocol import QnError, Command, Status
import sqlite3


@pytest.mark.parametrize('mode',['DIGITAL','ANALOG'])
def test_active_patch_is_atomic_and_stop_edit_start_is_explicit(tmp_path,mode):
    app=create_app(Settings(tmp_path/'config.db'))
    with TestClient(app) as api:
        p='/api/v1/modulators/0'
        source=api.post('/api/v1/sources',json={'name':'Shared','type':'HTTP','config':{'uri':'http://example.org/live'}}).json()
        config={'audio_mode':mode,'input_source_id':source['id'] if mode=='DIGITAL' else None,'frequency_khz':98100,'rds_enabled':True}
        assert api.patch(p,json=config).status_code==200
        assert api.post(p+'/start').status_code==200
        s=app.state.services
        app.state.control.executor.submit(s.status.refresh).result()
        rp=s.rp
        rp.set_tx_config=Mock(wraps=rp.set_tx_config)
        rp.set_tx_enabled=Mock(wraps=rp.set_tx_enabled)
        before=api.get(p).json()
        for patch in ({'frequency_khz':99500},{'gain_mode':'MANUAL'},{'enabled':False}):
            response=api.patch(p,json=patch)
            assert response.status_code==409
            assert response.json()['error'] in ('MODULATOR_ACTIVE', 'LIFECYCLE_REQUIRED')
            assert api.get(p).json()==before
        rp.set_tx_config.assert_not_called();rp.set_tx_enabled.assert_not_called()
        assert rp.enabled[0]
        assert api.post(p+'/stop').status_code==200
        rp.set_tx_config.reset_mock();rp.set_tx_enabled.reset_mock()
        assert api.patch(p,json={'frequency_khz':99500}).status_code==200
        assert not api.get(p).json()['enabled'] and not rp.enabled[0]
        rp.set_tx_config.assert_not_called();rp.set_tx_enabled.assert_not_called()
        assert api.patch(p,json={'enabled':True}).status_code==409
        assert api.post(p+'/start').status_code==200
        assert rp.configs[0].frequency_khz==99500


@pytest.fixture
def running(tmp_path):
    app = create_app(Settings(tmp_path/'save.db'), clock=lambda: 0.)
    with TestClient(app) as api:
        sid = api.post('/api/v1/sources', json={'name':'Shared', 'type':'HTTP', 'config':{'uri':'http://example.org/live'}}).json()['id']
        for tx in (0, 1):
            assert api.patch(f'/api/v1/modulators/{tx}', json={'input_source_id':sid,'frequency_khz':98100,'rds_enabled':True}).status_code == 200
            assert api.post(f'/api/v1/modulators/{tx}/start').status_code == 200
        yield api, app.state.services, sid


@pytest.mark.parametrize('endpoint', ['', '/save-and-resume'])
def test_stop_all_save_preserves_desired_hold_and_next_start(running, endpoint):
    api, s, _ = running
    assert all('error' not in row for row in api.post('/api/v1/modulators/stop-all').json())
    s.rp.set_tx_enabled = Mock(wraps=s.rp.set_tx_enabled)
    s.rp.set_tx_config = Mock(wraps=s.rp.set_tx_config)
    s.rp.clear_rds = Mock(wraps=s.rp.clear_rds)
    response = api.request('POST' if endpoint else 'PATCH', '/api/v1/modulators/0'+endpoint, json={'frequency_khz':99500,'rf_attenuation_db':9})
    assert response.status_code == 200
    if endpoint: assert response.json()['saved'] and not response.json()['restarted']
    config = api.get('/api/v1/modulators/0').json()
    assert config['enabled'] and config['frequency_khz'] == 99500
    assert 0 in s.modulators.bulk_stopped and not s.rp.enabled[0]
    s.rp.set_tx_enabled.assert_not_called(); s.rp.set_tx_config.assert_not_called(); s.rp.clear_rds.assert_not_called()
    api.post('/api/v1/modulators/start-all')
    assert s.rp.enabled[:2] == [True,True] and not any(s.rp.enabled[2:])
    assert s.rp.configs[0].frequency_khz == 99500


def test_individually_stopped_save_does_not_resume(running):
    api, s, _ = running
    api.post('/api/v1/modulators/0/stop')
    s.rp.set_tx_enabled = Mock(wraps=s.rp.set_tx_enabled)
    s.rp.set_tx_config = Mock(wraps=s.rp.set_tx_config)
    response = api.post('/api/v1/modulators/0/save-and-resume', json={'frequency_khz':99500}).json()
    assert response['saved'] and not response['restarted'] and response['stop_confirmed']
    assert not response['configuration']['enabled']
    s.rp.set_tx_enabled.assert_not_called(); s.rp.set_tx_config.assert_not_called()


def test_running_save_serializes_stop_save_start_and_preserves_shared_source(running):
    api, s, sid = running
    events, queued = [], []
    pipeline = s.sources.pipelines[sid]
    enable, config, clear, save = s.rp.set_tx_enabled, s.rp.set_tx_config, s.rp.clear_rds, s.database.save
    def enabled(tx, on):
        events.append(('enable',tx,on)); enable(tx,on)
        if not on:
            queued.append(api.app.state.control.executor.submit(lambda: events.append(('other control work',))))
    def configured(tx, value): events.append(('config',tx,value.frequency_khz)); return config(tx,value)
    def cleared(tx): events.append(('clear',tx)); return clear(tx)
    def saved(table, value, tx=None):
        events.append(('save',tx,value.enabled)); return save(table,value,tx)
    s.rp.set_tx_enabled, s.rp.set_tx_config, s.rp.clear_rds, s.database.save = enabled, configured, cleared, saved
    response = api.post('/api/v1/modulators/0/save-and-resume', json={'frequency_khz':99500})
    for future in queued: future.result()
    assert response.status_code == 200 and response.json()['saved'] and response.json()['restarted']
    assert events.index(('enable',0,False)) < events.index(('save',0,True)) < events.index(('config',0,99500)) < events.index(('enable',0,True)) < events.index(('other control work',))
    assert all(e[1] == 0 for e in events if e[0] != 'other control work')
    assert s.rp.enabled[:2] == [True, True] and s.sources.pipelines[sid] is pipeline
    assert s.sources.consumers[sid] == {0,1} and 0 not in s.modulators.bulk_stopped
    s.rp.set_tx_enabled, s.rp.set_tx_config, s.rp.clear_rds, s.database.save = enable, config, clear, save


@pytest.mark.parametrize('stage', ['stop', 'save', 'start'])
def test_save_partial_outcomes_and_ambiguous_stop_fencing(running, stage):
    api, s, _ = running
    enable, save = s.rp.set_tx_enabled, s.database.save
    def enabled(tx,on):
        if tx == 0 and ((stage == 'stop' and not on) or (stage == 'start' and on)):
            if stage == 'stop': raise DependencyUnavailable('lost OFF ACK; outcome unknown')
            raise QnError(Command.SET_TX_ENABLED, Status.QN_ERROR)
        return enable(tx,on)
    def saved(table, value, tx=None):
        if table == 'modulators' and tx == 0 and stage == 'save': raise sqlite3.OperationalError('disk full')
        return save(table,value,tx)
    s.rp.set_tx_enabled = Mock(side_effect=enabled)
    s.database.save = saved
    s.rp.set_tx_config = Mock(wraps=s.rp.set_tx_config)
    result = api.post('/api/v1/modulators/0/save-and-resume', json={'frequency_khz':99500}).json()
    assert result['failed_stage'] == stage and not result['restarted']
    assert result['saved'] == (stage == 'start')
    assert result['stop_confirmed'] == (stage != 'stop')
    config = api.get('/api/v1/modulators/0').json()
    assert config['enabled'] and config['frequency_khz'] == (99500 if stage == 'start' else 98100)
    assert s.rp.enabled[1]
    if stage != 'start':
        s.rp.set_tx_config.assert_not_called()
        s.rp.set_tx_enabled.assert_called_once_with(0,False)
        assert 0 in s.modulators.bulk_stopped
    if stage == 'stop':
        assert s.recovery.pending == {0:'enable'}
        assert api.patch('/api/v1/modulators/0', json={'frequency_khz':99700}).status_code == 409
    if stage == 'start': assert 'Configuration saved, but TX restart failed' in result['message']
    s.rp.set_tx_enabled, s.database.save = enable, save


def test_failed_bulk_stop_and_unknown_state_cannot_save(running):
    api, s, _ = running
    s.rp.available = False
    api.post('/api/v1/modulators/stop-all')
    for method, suffix in [('PATCH',''), ('POST','/save-and-resume')]:
        response = api.request(method, '/api/v1/modulators/0'+suffix, json={'frequency_khz':99500})
        assert response.status_code in (409,503)
    assert api.get('/api/v1/modulators/0').json()['frequency_khz'] == 98100
    assert set(s.modulators.active_sources) == {0,1}
