from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.rp2354.client import MockRp2354Client


def test_bulk_preserves_intent_and_recovery_hold(tmp_path):
    rp=MockRp2354Client();now=[0.0]
    app=create_app(Settings(tmp_path/'db'),rp=rp,clock=lambda:now[0])
    with TestClient(app) as api:
        source=api.post('/api/v1/sources',json={'name':'Shared','type':'HTTP','config':{'uri':'http://localhost/feed'}}).json()
        for tx in (0,1,2):
            assert api.patch(f'/api/v1/modulators/{tx}',json={'frequency_khz':98100,'input_source_id':source['id']}).status_code==200
        for tx in (0,1,2):assert api.post(f'/api/v1/modulators/{tx}/start').status_code==200
        assert api.post('/api/v1/modulators/2/stop').status_code==200
        before=api.get('/api/v1/modulators').json()
        results=api.post('/api/v1/modulators/stop-all').json()
        assert all(row['tx_state']=='OFF' for row in results)
        assert api.get('/api/v1/modulators').json()==before
        assert not app.state.services.sources.pipelines
        # RP recovery during this daemon session must not undo the operational stop.
        rp.available=False
        app.state.control.executor.submit(app.state.services.status.refresh).result()
        rp.available=True;now[0]=3
        app.state.control.executor.submit(app.state.services.status.refresh).result()
        assert not any(rp.enabled)
        results=api.post('/api/v1/modulators/start-all').json()
        assert [i for i,enabled in enumerate(rp.enabled) if enabled]==[0,1]
        assert api.get('/api/v1/modulators').json()==before
        assert app.state.services.sources.consumers[source['id']]=={0,1}
        # An explicit individual STOP changes persistent intent; bulk START respects it.
        api.post('/api/v1/modulators/1/stop')
        api.post('/api/v1/modulators/stop-all')
        api.post('/api/v1/modulators/start-all')
        assert [i for i,enabled in enumerate(rp.enabled) if enabled]==[0]
        assert api.get('/api/v1/modulators/1').json()['enabled'] is False
        api.post('/api/v1/modulators/2/start')
        assert api.get('/api/v1/modulators/2').json()['enabled'] is True


def test_stop_all_dependency_failure_retains_flags(tmp_path):
    rp=MockRp2354Client()
    app=create_app(Settings(tmp_path/'db'),rp=rp)
    with TestClient(app) as api:
        api.patch('/api/v1/modulators/0',json={'frequency_khz':98100,'audio_mode':'ANALOG'})
        api.post('/api/v1/modulators/0/start')
        before=api.get('/api/v1/modulators').json()
        rp.available=False
        result=api.post('/api/v1/modulators/stop-all').json()
        assert any('error' in row for row in result)
        assert api.get('/api/v1/modulators').json()==before
