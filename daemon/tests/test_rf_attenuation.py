"""Relative RF output contract, no hardware required."""
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.rp2354 import protocol as p


def test_relative_rf_api_round_trip(tmp_path):
    with TestClient(create_app(Settings(tmp_path/'api.db'))) as api:
        url='/api/v1/modulators/0'
        assert api.get(url).json()['rf_attenuation_db'] is None
        assert api.patch(url,json={'rf_output_dbuv':96}).status_code==422
        for i in range(16):
            response=api.patch(url,json={'rf_attenuation_db':i*1.5})
            assert response.status_code==200
            assert response.json()['rf_attenuation_db']==i*1.5
        for bad in [-1,1,23,24]:assert api.patch(url,json={'rf_attenuation_db':bad}).status_code==422


@pytest.mark.parametrize('value',[1,14,226,65535])
def test_invalid_relative_wire_targets(value):
    body=p.TX_CONFIG.pack(98100,value,1,50,0,0,0,0,0,0)
    with pytest.raises(ValueError):p.request(p.Command.SET_TX_CONFIG,0,body)


@pytest.mark.parametrize('value',[-1,1,226])
def test_invalid_relative_status(value):
    row=p.CHANNEL_STATUS.pack(0,value,0,0,0,0,255)
    with pytest.raises(p.ProtocolError):p.parse_status(p.GLOBAL_STATUS.pack(0,0,0,0)+row*8)
