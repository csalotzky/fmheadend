"""M6A deterministic integration using the existing high-level RP mock."""
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig, ModulatorConfig
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.errors import DependencyUnavailable

P='/api/v1'


def seed(path):
    db=Database(path)
    old=db.get('modulators',0)
    db.save('modulators',ModulatorConfig.model_validate(old.model_dump(exclude={'id','timestamp','rds_override_profile_id'}) | {
        'gain_mode':'MANUAL','analog_gain_db':6.0,'soft_clipping':True,
        'audio_mode':'ANALOG','frequency_khz':98100,'enabled':True,'rds_enabled':True,'rds_ecc':224}),0)
    db.close()


def refresh(app):
    app.state.control.executor.submit(app.state.services.status.refresh).result()


def test_startup_loss_recovery_order_and_shutdown(tmp_path):
    path=tmp_path/'config.db';seed(path);now=[0.0]
    rp=MockRp2354Client(clock=lambda:now[0]);rp.available=False
    events=[]
    for name in ('get_info','set_tx_config','set_tx_enabled','clear_rds','get_status','push_rds'):
        original=getattr(rp,name)
        def wrap(*args,_name=name,_original=original):
            events.append((_name,*args))
            return _original(*args)
        setattr(rp,name,wrap)
    app=create_app(Settings(path),rp=rp,clock=lambda:now[0])
    with TestClient(app) as api:
        assert api.get(P+'/status').json()['system']['system_state']=='DEGRADED'
        assert api.get(P+'/modulators/0').status_code == 503
        assert api.get(P+'/status').json()['modulators'] == []
        events.clear();refresh(app)
        assert not events  # Backoff: no bus traffic.
        rp.available=True;now[0]=3;refresh(app)
        assert events[0][0]=='get_info'
        config_index=next(i for i,e in enumerate(events) if e[0]=='set_tx_config')
        clear_index=next(i for i,e in enumerate(events) if e[:2]==('clear_rds',0))
        enable_index=events.index(('set_tx_enabled',0,True))
        assert config_index<clear_index<enable_index
        replay = events[config_index][2]
        assert (replay.gain_mode, replay.analog_gain_db, replay.soft_clipping) == ('MANUAL',6.0,True)
        assert rp.enabled==[True]+[False]*7
        assert api.get(P+'/status').json()['system']['system_state']=='RUNNING'
        services=app.state.services
        previous_channels=list(services.rds.channels)
        rp.available=False;refresh(app)
        assert all(a is b for a,b in zip(previous_channels, services.rds.channels))
        assert not services.rds.suspended
        assert api.get(P+'/status').json()['modulators'][0]['hardware_tx_state'] == 'ON'
        assert api.get(P+'/status').json()['system']['controller_observation_stale']
        rp.available=True;now[0]=6;events.clear();refresh(app)
        assert events == [('get_status',)]  # No recurring GET_INFO or writes.
        assert services.recovery.ready
        services.recovery.communication_failed(DependencyUnavailable('transport failure'))
        refresh(app)
        assert api.get(P+'/status').json()['system']['system_state']=='DEGRADED'
    assert not any(rp.enabled)
    db=Database(path);assert db.get('modulators',0).enabled;db.close()
    assert not app.state.services.sources.pipelines


@pytest.mark.parametrize('failure',['incompatible','config','clear','readback'])
def test_failed_restore_never_claims_healthy(tmp_path,failure):
    path=tmp_path/'config.db';seed(path);rp=MockRp2354Client()
    if failure=='incompatible':
        rp.get_info=Mock(return_value={'rp_protocol_version':2,'modulator_count':8})
    elif failure=='config':
        rp.set_tx_config=Mock(side_effect=DependencyUnavailable('config failed'))
    elif failure=='clear':
        rp.clear_rds=Mock(side_effect=DependencyUnavailable('clear failed'))
    else:
        original=rp.get_status
        def mismatch():
            result=original();result['modulators'][0]['tx_state']='OFF';return result
        rp.get_status=mismatch
    app=create_app(Settings(path),rp=rp)
    with TestClient(app) as api:
        status=api.get(P+'/status').json()
        assert status['system']['system_state']=='DEGRADED'
        if failure == 'incompatible':
            assert status['modulators'] == []
        else:
            assert status['modulators'][0]['hardware_tx_state'] == ('OFF' if failure == 'readback' else None)
        assert app.state.services.rp is rp
        assert app.state.services.rds.suspended
        if failure != 'incompatible':
            assert status['modulators'][0]['rds_state'] == 'SUSPENDED'
            assert status['modulators'][1]['rds_state'] == 'DISABLED'
        assert not rp.rds_history
        if failure!='readback': assert not any(rp.enabled)
        if failure != 'incompatible':
            assert api.get(P+'/modulators/0').json()['enabled']
        else:
            assert api.get(P+'/modulators/0').status_code == 503


def test_mux_loss_does_not_reconfigure_rp(tmp_path):
    app=create_app(Settings(tmp_path/'config.db'))
    with TestClient(app) as api:
        s=app.state.services
        s.rp.set_tx_config=Mock(wraps=s.rp.set_tx_config)
        s.mux.available=False;refresh(app)
        assert api.get(P+'/status').json()['system']['system_state']=='DEGRADED'
        s.mux.available=True;refresh(app)
        assert api.get(P+'/status').json()['system']['system_state']=='RUNNING'
        s.rp.set_tx_config.assert_not_called()


def test_bad_final_startup_read_retries_observation_not_hardware(tmp_path):
    path = tmp_path/'startup.db'; seed(path); now = [0.]
    rp = MockRp2354Client(clock=lambda:now[0])
    original = rp.get_status
    rp.get_status = Mock(side_effect=[DependencyUnavailable('lost status'), original()])
    rp.set_tx_enabled = Mock(wraps=rp.set_tx_enabled)
    rp.set_tx_config = Mock(wraps=rp.set_tx_config)
    rp.clear_rds = Mock(wraps=rp.clear_rds)
    app = create_app(Settings(path), rp=rp, clock=lambda:now[0])
    with TestClient(app):
        assert not app.state.services.recovery.ready
        counts = [m.call_count for m in (rp.set_tx_enabled,rp.set_tx_config,rp.clear_rds)]
        rp.get_status.side_effect = original
        now[0] += .3; refresh(app)
        assert app.state.services.recovery.ready
        assert counts == [m.call_count for m in (rp.set_tx_enabled,rp.set_tx_config,rp.clear_rds)]


def test_unavailable_shutdown_preserves_desired_and_cleans_workers(tmp_path):
    path=tmp_path/'config.db';seed(path);rp=MockRp2354Client()
    app=create_app(Settings(path),rp=rp)
    with TestClient(app):
        rp.available=False
        rp.set_tx_enabled=Mock(wraps=rp.set_tx_enabled)
    assert rp.set_tx_enabled.call_count==1
    assert not app.state.services.modulators.active_sources
    assert app.state.control.executor._shutdown
    db=Database(path);assert db.get('modulators',0).enabled;db.close()


def test_desired_stop_after_failed_final_startup_observation(tmp_path):
    path = tmp_path / 'startup-desired.db'; seed(path); now = [0.]
    rp = MockRp2354Client(clock=lambda: now[0])
    original = rp.get_status
    rp.get_status = Mock(side_effect=DependencyUnavailable('lost final status'))
    app = create_app(Settings(path), rp=rp, clock=lambda: now[0])
    with TestClient(app) as api:
        s = app.state.services
        assert not s.recovery.ready and rp.enabled[0]
        rp.set_tx_enabled = Mock(wraps=rp.set_tx_enabled)
        rp.set_tx_config = Mock(wraps=rp.set_tx_config)
        rp.clear_rds = Mock(wraps=rp.clear_rds)
        response = api.post(P + '/modulators/0/stop')
        assert response.status_code == 503
        assert not api.get(P + '/modulators/0').json()['enabled']
        rp.set_tx_enabled.assert_not_called()
        rp.get_status.side_effect = original
        now[0] += .3; refresh(app)
        assert s.recovery.ready and not rp.enabled[0]
        rp.set_tx_enabled.assert_called_once_with(0, False)
        rp.set_tx_config.assert_not_called()
        rp.clear_rds.assert_not_called()  # Startup RDS refill was still suspended.
        refresh(app)
        rp.set_tx_enabled.assert_called_once_with(0, False)


def test_systemd_contract():
    root=Path(__file__).resolve().parents[2]/'systemd'
    mux=(root/'fmheadend-muxd.service').read_text()
    daemon=(root/'fmheadendd.service').read_text()
    assert 'RuntimeDirectory=fmheadend' in mux
    assert 'fmheadendd.service' not in mux
    assert 'Wants=network-online.target fmheadend-muxd.service' in daemon
    assert 'After=network-online.target fmheadend-muxd.service' in daemon
    for unit in (mux,daemon):
        assert 'Restart=on-failure' in unit
        assert 'StandardOutput=journal' in unit
        assert 'TimeoutStopSec=30' in unit


def test_rds_transport_failure_fences_only_target(tmp_path):
    path=tmp_path/'config.db';seed(path);now=[0.0]
    app=create_app(Settings(path),clock=lambda:now[0])
    with TestClient(app):
        s=app.state.services
        s.rp.push_rds=Mock(side_effect=DependencyUnavailable('ambiguous push'))
        now[0]=1
        app.state.control.executor.submit(s.rds.tick).result()
        assert s.recovery.ready and not s.rds.suspended
        assert s.rds.channels[0].needs_clear and s.rds.channels[0].pending is None
        assert not any(c.needs_clear for c in s.rds.channels[1:])
        assert s.rp.push_rds.call_count==1
        app.state.control.executor.submit(s.rds.tick).result()
        assert s.rp.push_rds.call_count==1
        assert s.status.snapshot().system.system_state=='DEGRADED'


def test_uptime_decrease_alone_does_not_restore_hardware(tmp_path):
    now=[0.0];rp=MockRp2354Client();uptime=[1000]
    original=rp.get_status
    def status():
        result=original();result['uptime_ms']=uptime[0];return result
    rp.get_status=status
    app=create_app(Settings(tmp_path/'config.db'),rp=rp,clock=lambda:now[0])
    with TestClient(app):
        rp.set_tx_enabled=Mock(wraps=rp.set_tx_enabled)
        rp.set_tx_config=Mock(wraps=rp.set_tx_config)
        uptime[0]=10;refresh(app)
        assert app.state.services.recovery.ready
        now[0]=3;refresh(app)
        assert app.state.services.recovery.ready
        rp.set_tx_enabled.assert_not_called()
        rp.set_tx_config.assert_not_called()


def test_stream_error_keeps_rf_and_pipeline_during_rp_recovery(tmp_path):
    now=[0.0]
    app=create_app(Settings(tmp_path/'config.db'),clock=lambda:now[0])
    with TestClient(app) as api:
        sid=api.post(P+'/sources',json={'name':'http','type':'HTTP','config':{'uri':'http://example.org/audio'}}).json()['id']
        api.patch(P+'/modulators/0',json={'input_source_id':sid,'frequency_khz':98100})
        assert api.post(P+'/modulators/0/start').status_code==200
        s=app.state.services;pipeline=s.sources.pipelines[sid]
        from fmheadendd.models.runtime_status import SourceState
        s.sources.statuses[sid].state=SourceState.ERROR
        s.rp.set_tx_config=Mock(wraps=s.rp.set_tx_config)
        refresh(app)
        assert s.rp.enabled[0] and s.recovery.ready
        s.rp.set_tx_config.assert_not_called()
        s.rp.available=False;refresh(app)
        assert s.sources.pipelines[sid] is pipeline
        s.rp.available=True;now[0]=3;refresh(app)
        assert s.sources.pipelines[sid] is pipeline
        assert s.rp.enabled[0]


@pytest.mark.parametrize('enabled', [set(), {0}, {0, 2, 5, 7}])
def test_explicit_user_choices_survive_normal_shutdown_and_restart(tmp_path, enabled):
    path = tmp_path / 'restart.db'
    rp = MockRp2354Client()
    with TestClient(create_app(Settings(path), rp=rp)) as api:
        for channel in range(8):
            assert api.patch(P+f'/modulators/{channel}', json={
                'audio_mode': 'ANALOG', 'frequency_khz': 98100}).status_code == 200
            # Exercise both explicit commands, including OFF after a prior ON.
            assert api.post(P+f'/modulators/{channel}/start').status_code == 200
            assert api.get(P+f'/modulators/{channel}').json()['enabled'] is True
            if channel not in enabled:
                assert api.post(P+f'/modulators/{channel}/stop').status_code == 200
            assert api.get(P+f'/modulators/{channel}').json()['enabled'] == (channel in enabled)
        assert rp.enabled == [i in enabled for i in range(8)]
    assert rp.enabled == [False] * 8  # Hardware cleanup is not a desired-state edit.
    db = Database(path)
    assert [m.enabled for m in db.list('modulators')] == [i in enabled for i in range(8)]
    db.close()
    restored = MockRp2354Client()
    with TestClient(create_app(Settings(path), rp=restored)) as api:
        refresh(api.app)
        assert api.app.state.services.recovery.ready
        assert restored.enabled == [i in enabled for i in range(8)]


@pytest.mark.parametrize('disable_before_resume', [False, True])
def test_global_stop_survives_restart_and_start_keeps_resume_selection(tmp_path, disable_before_resume):
    path = tmp_path / 'hold.db'
    seed(path)
    db = Database(path)
    config = db.get('modulators', 0)
    db.save('modulators', ModulatorConfig.model_validate(
        config.model_dump(exclude={'id', 'timestamp', 'rds_override_profile_id'})), 2)
    db.close()
    with TestClient(create_app(Settings(path))) as api:
        for _ in range(2):
            assert api.post(P+'/modulators/stop-all').status_code == 200
        assert api.get(P+'/status').json()['system']['global_stopped'] is True
        assert [m['id'] for m in api.get(P+'/modulators').json() if m['enabled']] == [0, 2]
    rp = MockRp2354Client()
    rp.set_tx_enabled = Mock(wraps=rp.set_tx_enabled)
    with TestClient(create_app(Settings(path), rp=rp)) as api:
        refresh(api.app)
        assert api.app.state.services.recovery.ready
        assert not any(rp.enabled)
        assert all(not call.args[1] for call in rp.set_tx_enabled.call_args_list)
        assert api.get(P+'/status').json()['system']['global_stopped'] is True
        assert api.post(P+'/modulators/stop-all').status_code == 200
        # Ordinary system edits cannot release the hold.
        assert api.patch(P+'/system/config', json={'log_level': 'DEBUG'}).status_code == 200
        assert api.patch(P+'/system/config', json={'global_stopped': False}).status_code == 422
        if disable_before_resume:
            assert api.post(P+'/modulators/2/stop').status_code == 200
        assert api.post(P+'/modulators/start-all').status_code == 200
        assert rp.enabled == [i == 0 or (i == 2 and not disable_before_resume) for i in range(8)]
        assert api.get(P+'/status').json()['system']['global_stopped'] is False
    with TestClient(create_app(Settings(path))) as api:
        refresh(api.app)
        assert api.app.state.services.rp.enabled == [
            i == 0 or (i == 2 and not disable_before_resume) for i in range(8)]


@pytest.mark.parametrize('start', [False, True])
def test_global_command_persistence_failure_is_not_accepted(tmp_path, start):
    import sqlite3
    from unittest.mock import patch
    path = tmp_path / 'save-failure.db'
    seed(path)
    with TestClient(create_app(Settings(path))) as api:
        services = api.app.state.services
        if start:
            assert api.post(P+'/modulators/stop-all').status_code == 200
        before = list(services.rp.enabled)
        holds = set(services.modulators.bulk_stopped)
        with patch.object(services.database, 'save_system', side_effect=sqlite3.OperationalError('disk full')):
            result = api.post(P+'/modulators/'+('start-all' if start else 'stop-all'))
        assert result.status_code == 503
        assert result.json()['error'] == 'GLOBAL_STATE_SAVE_FAILED'
        assert services.rp.enabled == before
        assert services.modulators.bulk_stopped == holds
        assert api.get(P+'/system/config').json()['global_stopped'] is start
        assert api.get(P+'/status').json()['system']['global_stopped'] is start


def test_failed_desired_startup_survives_restart_without_erasing_preference(tmp_path):
    path = tmp_path / 'failed.db'
    seed(path)
    rp = MockRp2354Client()
    rp.set_tx_config = Mock(side_effect=DependencyUnavailable('configuration unavailable'))
    with TestClient(create_app(Settings(path), rp=rp)) as api:
        assert not api.app.state.services.recovery.ready
        assert 'configuration unavailable' in str(api.app.state.services.recovery.error)
        assert api.get(P+'/modulators/0').json()['enabled'] is True
    with TestClient(create_app(Settings(path))) as api:
        refresh(api.app)
        assert api.app.state.services.rp.enabled == [True] + [False] * 7


def test_individual_start_replaces_global_hold_selection_and_survives_restart(tmp_path):
    path = tmp_path / 'new-selection.db'
    seed(path)
    with TestClient(create_app(Settings(path))) as api:
        for channel in (1, 2):
            assert api.patch(P+f'/modulators/{channel}', json={
                'audio_mode': 'ANALOG', 'frequency_khz': 98100}).status_code == 200
        # Normal individual START preserves the other enabled TX.
        assert api.post(P+'/modulators/2/start').status_code == 200
        assert api.app.state.services.rp.enabled == [True, False, True] + [False] * 5
        assert api.post(P+'/modulators/stop-all').status_code == 200
        before = api.get(P+'/modulators').json()
        rp = api.app.state.services.rp
        rp.set_tx_enabled = Mock(wraps=rp.set_tx_enabled)
        assert api.post(P+'/modulators/1/start').status_code == 200
        assert rp.set_tx_enabled.call_args_list == [((1, True),)]
        assert rp.enabled == [False, True] + [False] * 6
        assert api.get(P+'/status').json()['system']['global_stopped'] is False
        assert api.app.state.services.modulators.bulk_stopped == set()
        after = api.get(P+'/modulators').json()
        assert [m['id'] for m in after if m['enabled']] == [1]
        for old, new in zip(before, after):
            assert {k: v for k, v in old.items() if k not in ('enabled', 'timestamp')} == {
                k: v for k, v in new.items() if k not in ('enabled', 'timestamp')}
    rp = MockRp2354Client()
    rp.set_tx_enabled = Mock(wraps=rp.set_tx_enabled)
    with TestClient(create_app(Settings(path), rp=rp)) as api:
        refresh(api.app)
        assert rp.enabled == [False, True] + [False] * 6
        assert all(call.args[0] == 1 for call in rp.set_tx_enabled.call_args_list if call.args[1])
        assert api.get(P+'/status').json()['system']['global_stopped'] is False
        assert api.post(P+'/modulators/stop-all').status_code == 200
        assert api.post(P+'/modulators/start-all').status_code == 200
        assert rp.enabled == [False, True] + [False] * 6


def test_individual_selection_save_failure_preserves_global_hold(tmp_path):
    import sqlite3
    from unittest.mock import patch
    path = tmp_path / 'selection-failure.db'
    seed(path)
    with TestClient(create_app(Settings(path))) as api:
        assert api.patch(P+'/modulators/1', json={
            'audio_mode': 'ANALOG', 'frequency_khz': 98100}).status_code == 200
        assert api.post(P+'/modulators/stop-all').status_code == 200
        services = api.app.state.services
        before = api.get(P+'/modulators').json()
        services.rp.set_tx_enabled = Mock(wraps=services.rp.set_tx_enabled)
        with patch.object(services.database, 'select_single_modulator',
                          side_effect=sqlite3.OperationalError('disk full')):
            result = api.post(P+'/modulators/1/start')
        assert result.status_code == 503
        assert result.json()['error'] == 'GLOBAL_STATE_SAVE_FAILED'
        assert api.get(P+'/modulators').json() == before
        assert api.get(P+'/status').json()['system']['global_stopped'] is True
        assert services.modulators.bulk_stopped == set(range(8))
        services.rp.set_tx_enabled.assert_not_called()
        # Invalid explicit START must not replace the selection either.
        assert api.post(P+'/modulators/7/start').status_code == 409
        assert api.get(P+'/modulators').json() == before


def test_new_individual_selection_survives_runtime_start_failure(tmp_path):
    path = tmp_path / 'selection-runtime-failure.db'
    seed(path)
    with TestClient(create_app(Settings(path))) as api:
        assert api.patch(P+'/modulators/1', json={
            'audio_mode': 'ANALOG', 'frequency_khz': 98100}).status_code == 200
        assert api.post(P+'/modulators/stop-all').status_code == 200
        services = api.app.state.services
        services.rp.set_tx_config = Mock(side_effect=DependencyUnavailable('unavailable'))
        assert api.post(P+'/modulators/1/start').status_code == 503
        assert [m['id'] for m in api.get(P+'/modulators').json() if m['enabled']] == [1]
        assert api.get(P+'/status').json()['system']['global_stopped'] is False
    with TestClient(create_app(Settings(path))) as api:
        refresh(api.app)
        assert api.app.state.services.rp.enabled == [False, True] + [False] * 6
