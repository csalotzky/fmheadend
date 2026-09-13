"""Configuration is separate from lifecycle for every PATCH client."""
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings


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
        for patch in ({'frequency_khz':99500},{'gain_mode':'MANUAL'},{'rds_rt':'New'},{'enabled':False}):
            response=api.patch(p,json=patch)
            assert response.status_code==409
            assert response.json()['error']=='MODULATOR_ACTIVE'
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
