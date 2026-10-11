"""ALSA inventory never opens PCM; configuration remains independent of hardware."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fmheadendd.alsa import AlsaDiscovery
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import AlsaConfig


def card(root, index=2, identifier='CODEC', direction='c', number=0):
    folder = root / f'card{index}'
    folder.mkdir(exist_ok=True)
    (folder / 'id').write_text(identifier)
    pcm = folder / f'pcm{number}{direction}'
    pcm.mkdir(exist_ok=True)
    (pcm / 'info').write_text(f'stream: {"CAPTURE" if direction == "c" else "PLAYBACK"}\nname: USB Audio\n')
    return pcm


def test_capture_only_stable_distinguishable_and_fresh(tmp_path):
    card(tmp_path, direction='p')
    card(tmp_path)
    card(tmp_path, 3, 'CODEC_1')
    (tmp_path / 'cards').write_text(' 2 [CODEC ]: USB-Audio - USB Audio CODEC\n')
    discovery = AlsaDiscovery(tmp_path)
    devices = discovery.capture_devices().devices
    assert len(devices) == 2
    assert {d.pcm for d in devices} == {'plughw:CARD=CODEC,DEV=0', 'plughw:CARD=CODEC_1,DEV=0'}
    assert len({d.label for d in devices}) == 2
    assert any('USB Audio CODEC' in d.label for d in devices)
    (tmp_path / 'card2').rename(tmp_path / 'card7')
    assert {d.pcm for d in discovery.capture_devices().devices} == {d.pcm for d in devices}
    (tmp_path / 'card7/pcm0c/info').unlink()
    assert len(discovery.capture_devices().devices) == 1
    card(tmp_path, 4, 'New')
    assert len(discovery.capture_devices().devices) == 2


def test_no_devices_or_subsystem(tmp_path):
    assert AlsaDiscovery(tmp_path).capture_devices().devices == []
    assert AlsaDiscovery(tmp_path / 'absent').capture_devices().devices == []


def test_errors_are_not_reported_as_empty_inventory(tmp_path, monkeypatch):
    def denied(self):
        raise PermissionError('permission denied')
    monkeypatch.setattr(type(tmp_path), 'iterdir', denied)
    with pytest.raises(DomainError) as result:
        AlsaDiscovery(tmp_path).capture_devices()
    assert result.value.status == 503
    assert result.value.error == 'ALSA_DISCOVERY_ERROR'


@pytest.mark.parametrize('device', ['default', 'plughw:CARD=CODEC,DEV=0', 'hw:2,0', 'my_dsnoop', 'route:SLAVE="custom pcm"'])
@pytest.mark.parametrize('mode', ['stereo', 'mono', 'left', 'right'])
def test_config_custom_names_and_modes(device, mode):
    value = AlsaConfig(device=device, channel_selection=mode)
    assert value.model_dump() == dict(device=device, channel_selection=mode)


@pytest.mark.parametrize('device', ['', '  ', 'bad\0name', 'bad\nname', 12, None])
def test_invalid_device(device):
    with pytest.raises(ValidationError):
        AlsaConfig(device=device)


def test_canonical_config_has_no_user_rate_or_channel_count():
    assert AlsaConfig(device='default').channel_selection == 'stereo'
    for invalid in ({'channel_selection': 'rear'}, {'sample_rate': 44100}, {'channels': 2}):
        with pytest.raises(ValidationError):
            AlsaConfig(device='default', **invalid)


def test_endpoint_refresh_errors_persistence_and_probe(tmp_path):
    with TestClient(create_app(Settings(tmp_path / 'alsa.db'))) as client:
        client.app.state.services.alsa = AlsaDiscovery(tmp_path)
        url = '/api/v1/alsa/capture-devices'
        assert client.get(url).json() == {'devices': []}
        card(tmp_path)
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['devices'][0]['pcm'] == 'plughw:CARD=CODEC,DEV=0'
        body = dict(name='Capture', type='ALSA', config={'device': 'custom_pcm', 'channel_selection': 'right'})
        saved = client.post('/api/v1/sources', json=body)
        assert saved.status_code == 201
        sid = saved.json()['id']
        assert client.get(f'/api/v1/sources/{sid}').json()['config'] == body['config']
        body['config']['channel_selection'] = 'mono'
        assert client.patch(f'/api/v1/sources/{sid}', json=body).json()['config'] == body['config']
        probe = client.post('/api/v1/sources/test', json=body)
        assert probe.status_code == 200 and probe.json()['mocked'] is True
        assert client.get(f'/api/v1/sources/{sid}').json()['config'] == body['config']
        (tmp_path / 'card2/id').write_text('invalid card id')
        failure = client.get(url)
        assert failure.status_code == 503 and failure.json()['error'] == 'ALSA_DISCOVERY_ERROR'
    with TestClient(create_app(Settings(tmp_path / 'alsa.db'))) as client:
        assert client.get(f'/api/v1/sources/{sid}').json()['config'] == body['config']
