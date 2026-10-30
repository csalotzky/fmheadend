"""Relative RF output contract, no hardware required."""
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.models.configuration import ModulatorConfig
from fmheadendd.rp2354.client import TxConfig
from fmheadendd.rp2354 import protocol as p


def test_relative_rf_api_round_trip(tmp_path):
    settings = Settings(tmp_path / 'api.db')
    url = '/api/v1/modulators/0'
    with TestClient(create_app(settings)) as api:
        assert api.get(url).json()['rf_attenuation_db'] is None
        assert api.patch(url, json={'rf_output_dbuv': 96}).status_code == 422
        for wire in range(406):
            level = wire / 10
            response = api.patch(url, json={'rf_attenuation_db': level})
            assert response.status_code == 200
            assert response.json()['rf_attenuation_db'] == level
            assert api.get(url).json()['rf_attenuation_db'] == level
        for bad in [-1, -0.1, 40.6, 41, 6553.6, 0.01, 40.49, 1.5000000000000002,
                    True, '6', 'NaN', 'Infinity']:
            assert api.patch(url, json={'rf_attenuation_db': bad}).status_code == 422
        response = api.post('/api/v1/modulators/1/save-and-resume',
                            json={'rf_attenuation_db': 40.4})
        assert response.status_code == 200 and response.json()['saved']
        assert response.json()['configuration']['rf_attenuation_db'] == 40.4

    with TestClient(create_app(settings)) as api:
        assert api.get(url).json()['rf_attenuation_db'] == 40.5
        assert api.get('/api/v1/modulators/1').json()['rf_attenuation_db'] == 40.4
        assert api.patch(url, json={'rf_attenuation_db': None}).json()['rf_attenuation_db'] is None


@pytest.mark.parametrize('wire', range(406))
def test_relative_models_and_wire_encoding(wire):
    level = wire / 10
    modulator = ModulatorConfig(frequency_khz=98100, rf_attenuation_db=level)
    config = TxConfig(**modulator.model_dump(include=set(TxConfig.model_fields)))
    assert config.rf_attenuation_db == level
    payload = p.tx_config(config)
    assert payload[4:6] == wire.to_bytes(2, 'little')
    packet = p.request(p.Command.SET_TX_CONFIG, 0, payload)
    assert packet[:3] == bytes((2, 0, 15))
    assert packet[3:-2] == payload


@pytest.mark.parametrize('value', [406, 65535])
def test_invalid_relative_wire_targets(value):
    body = p.TX_CONFIG.pack(98100, value, 1, 50, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        p.request(p.Command.SET_TX_CONFIG, 0, body)


@pytest.mark.parametrize('value', [-32767, -1, 406, 32767])
def test_invalid_relative_status(value):
    row = p.CHANNEL_STATUS.pack(0, value, 0, 0, 0, 0, 255)
    with pytest.raises(p.ProtocolError):
        p.parse_status(p.GLOBAL_STATUS.pack(0, 0, 0, 0) + row * 8)


@pytest.mark.parametrize('value', [-32768, *range(406)])
def test_relative_status_range_and_unavailable_sentinel(value):
    row = p.CHANNEL_STATUS.pack(1, value, 0, 0, 0, 0, 255)
    status = p.parse_status(p.GLOBAL_STATUS.pack(0, 0, 0, 0) + row * 8)
    expected = None if value == -32768 else value / 10
    assert [channel['rf_attenuation_db'] for channel in status['modulators']] == [expected] * 8
