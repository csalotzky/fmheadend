"""Real audiod discovery through the mock-controller Dashboard and log APIs."""
import sys

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.sources.direct_pcm import DirectHttpFactory, DirectMux
from fmheadendd.tests.profile_support import profile_settings
from fmheadendd.tests.test_audio_worker import until
from fmheadendd.tests.test_direct_multitx_native import Sinks
from fmheadendd.tests.test_gstreamer import gst, require
from fmheadendd.tests.test_local_worker_native import wav


@pytest.mark.skipif(sys.platform != 'linux', reason='Requires native audiod')
@pytest.mark.parametrize('resolution', ['arrival', 'recursive', 'stop'])
def test_pending_local_discovery_visible(gst, tmp_path, caplog, resolution):
    require(gst, 'playbin3', 'wavparse')
    caplog.set_level('INFO')
    root = tmp_path / 'private-nas'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    wav(nested / 'audio.wav', 6000, 12)
    sinks = Sinks(tmp_path)
    app = create_app(profile_settings(tmp_path / 'test.db'),
                     pipeline_factory=DirectHttpFactory(tmp_path, local=True),
                     mux_factory=lambda sources: DirectMux(sources, tmp_path))
    try:
        with TestClient(app) as api:
            services = app.state.services
            def get(path):
                response = api.get('/api/v1' + path)
                assert response.status_code == 200, response.text
                return response.json()

            def rows():
                return get('/status')['modulators'][:2]

            config = dict(mode='folder', path=str(root), recursive=False)
            result = api.post('/api/v1/sources', json=dict(name='Folder', type='LOCAL', config=config))
            assert result.status_code == 201, result.text
            sid = result.json()['id']
            for tx in (0, 1):
                path = f'/api/v1/modulators/{tx}'
                assert api.patch(path, json=dict(input_source_id=sid, frequency_khz=98100)).status_code == 200
                initial = api.post(path + '/start').json()
                assert initial['start_pending'] and initial['source_state'] == 'CONNECTING'
            until(lambda: get(f'/sources/{sid}/status')['last_error'] == 'NO_PLAYABLE_TRACKS', timeout=12)
            pending = rows()
            assert all(row['start_pending'] and row['source_state'] == 'CONNECTING' and
                       row['hardware_tx_state'] == 'OFF' and row['source_last_error'] is None for row in pending)
            assert all('No playable tracks found' in row['pending_message'] for row in pending), pending
            owner = services.sources.pipelines[sid]
            assert services.sources.consumers[sid] == {0, 1}
            assert not any(services.rp.enabled)
            assert all(get(f'/modulators/{tx}')['enabled'] for tx in (0, 1))

            def warnings():
                return [entry for entry in get('/system/logs') if entry['severity'] == 'WARNING' and
                        f'source {sid} LOCAL' in entry['message']]

            assert len(warnings()) == 1 and 'NO_PLAYABLE_TRACKS' in warnings()[0]['message']
            assert str(root) not in str(warnings()) and str(root) not in str(pending)
            if resolution == 'arrival':
                # Ensure a repeated discovery produced no duplicate RecentLogs entry.
                catalog = owner._catalog
                request = catalog.last_request
                until(lambda: catalog.last_request > request and not catalog.scan_request, timeout=9)
                assert len(warnings()) == 1 and not any(services.rp.enabled)
                (root / 'audio.wav').write_bytes((nested / 'audio.wav').read_bytes())
            elif resolution == 'recursive':
                result = api.patch(f'/api/v1/sources/{sid}', json={'config': config | {'recursive': True}})
                assert result.status_code == 200 and result.json()['saved'], result.text
            if resolution != 'stop':
                until(lambda: all(row['hardware_tx_state'] == 'ON' and not row['start_pending'] and
                                  row['pending_message'] is None for row in rows()) and
                      min(sinks.counts[:2]) > 0, timeout=22)
                if resolution == 'arrival':
                    assert services.sources.pipelines[sid] is owner and not services.sources.worker_recovery.attempts
            for tx in (0, 1):
                assert api.post(f'/api/v1/modulators/{tx}/stop').status_code == 200
                if tx == 0:
                    assert services.sources.consumers[sid] == {1}
            until(lambda: sid not in services.sources.pipelines and all(not row['start_pending'] and
                  row['pending_message'] is None and row['hardware_tx_state'] == 'OFF' for row in rows()), timeout=8)
    finally:
        sinks.close()
