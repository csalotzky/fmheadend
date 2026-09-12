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
    db.save('modulators',ModulatorConfig.model_validate(old.model_dump(exclude={'id','timestamp'}) | {
        'gain_mode':'MANUAL','analog_gain_db':6.0,'digital_trim_db':1,'soft_clipping':True,
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
        assert api.get(P+'/modulators/0').json()['enabled']
        assert api.get(P+'/status').json()['modulators'][0]['hardware_tx_state'] is None
        assert api.get(P+'/status').json()['modulators'][0]['source_state'] is None
        events.clear();refresh(app)
        assert not events  # Backoff: no bus traffic.
        rp.available=True;now[0]=3;refresh(app)
        assert events[0][0]=='get_info'
        config_index=next(i for i,e in enumerate(events) if e[0]=='set_tx_config')
        clear_index=next(i for i,e in enumerate(events) if e[:2]==('clear_rds',0))
        enable_index=events.index(('set_tx_enabled',0,True))
        assert config_index<clear_index<enable_index
        replay = events[config_index][2]
        assert (replay.gain_mode, replay.analog_gain_db, replay.digital_trim_db, replay.soft_clipping) == ('MANUAL',6.0,1,True)
        assert rp.enabled==[True]+[False]*7
        assert api.get(P+'/status').json()['system']['system_state']=='RUNNING'
        services=app.state.services
        services.rds.channels[0].pending=('stale',None)
        rp.available=False;refresh(app)
        assert all(c.pending is None for c in services.rds.channels)
        assert services.rds.suspended
        assert api.get(P+'/status').json()['modulators'][0]['hardware_tx_state'] is None
        rp.available=True;now[0]=6;events.clear();refresh(app)
        assert ('clear_rds',0) in events
        assert not any(e[0]=='push_rds' for e in events)
        assert services.recovery.ready
        services.recovery.lost(ValueError('invalid hardware configuration'))
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
        assert status['modulators'][0]['hardware_tx_state'] is None
        assert app.state.services.rp is rp
        assert app.state.services.rds.suspended
        assert not rp.rds_history
        if failure!='readback': assert not any(rp.enabled)
        assert api.get(P+'/modulators/0').json()['enabled']


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


def test_rds_transport_failure_suspends_all_channels(tmp_path):
    path=tmp_path/'config.db';seed(path);now=[0.0]
    app=create_app(Settings(path),clock=lambda:now[0])
    with TestClient(app):
        s=app.state.services
        s.rp.push_rds=Mock(side_effect=DependencyUnavailable('ambiguous push'))
        now[0]=1
        app.state.control.executor.submit(s.rds.tick).result()
        assert not s.recovery.ready and s.rds.suspended
        assert all(c.pending is None for c in s.rds.channels)
        assert s.rp.push_rds.call_count==1
        app.state.control.executor.submit(s.rds.tick).result()
        assert s.rp.push_rds.call_count==1
        assert s.status.snapshot().system.system_state=='DEGRADED'


def test_rp_reset_detected_by_uptime(tmp_path):
    now=[0.0];rp=MockRp2354Client();uptime=[1000]
    original=rp.get_status
    def status():
        result=original();result['uptime_ms']=uptime[0];return result
    rp.get_status=status
    app=create_app(Settings(tmp_path/'config.db'),rp=rp,clock=lambda:now[0])
    with TestClient(app):
        rp.get_info=Mock(wraps=rp.get_info)
        uptime[0]=10;refresh(app)
        assert not app.state.services.recovery.ready
        now[0]=3;refresh(app)
        assert app.state.services.recovery.ready
        assert rp.get_info.called


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
