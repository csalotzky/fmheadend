"""Loopback PCM and real direct workers with mock RF control."""
import sqlite3
import struct
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.sources.direct_pcm import DirectHttpFactory, DirectMux
from fmheadendd.tests.profile_support import profile_settings
from fmheadendd.tests.test_audio_worker import until
from fmheadendd.tests.test_direct_multitx_native import Sinks
from fmheadendd.tests.test_gstreamer import file_profile, free_udp_port, gst
from fmheadendd.tests.test_network_direct_native import paced


@pytest.fixture
def running(gst, tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Requires native audiod')
    app = create_app(profile_settings(tmp_path / 'switch.db'),
                     pipeline_factory=DirectHttpFactory(tmp_path, network=True),
                     mux_factory=lambda sources: DirectMux(sources, tmp_path))
    sinks = Sinks(tmp_path)
    senders = []
    try:
        with TestClient(app) as api:
            s = app.state.services
            def call(function):
                return app.state.control.executor.submit(function).result(timeout=4)

            profile = call(lambda: s.profile_files.create(file_profile('Switch').model_dump(mode='json')))
            ids = []
            for index in range(2):
                source, sender = paced(gst, 'UDP', tmp_path, free_udp_port(), seconds=30,
                                       left=6000 + 1000*index)
                senders.append(sender)
                body = source.model_dump(mode='json') | dict(name=f'Source {index}', use_default_audio_profile=False,
                    audio_profile_id=profile.path, rds_profile=dict(rds_pi=101+index, rds_ecc=0xE1+index,
                    rds_rt=f'Profile {index}', rds_metadata_to_rt=True))
                response = api.post('/api/v1/sources', json=body)
                assert response.status_code == 201, response.text
                ids.append(response.json()['id'])

            def assign(tx, sid):
                base = f'/api/v1/modulators/{tx}'
                resp = api.patch(base, json=dict(input_source_id=sid, frequency_khz=98100, rds_follow_source=True))
                assert resp.status_code == 200, resp.text
                assert api.post(base + '/start').status_code == 200

            def row(tx=0):
                return api.get('/api/v1/status').json()['modulators'][tx]

            def saved(tx=0):
                return api.get(f'/api/v1/modulators/{tx}').json()

            def send(index):
                assert senders[index].set_state(gst.State.PLAYING) != gst.StateChangeReturn.FAILURE

            send(0)
            for tx in (0, 1):
                assign(tx, ids[0])
            until(lambda: row()['hardware_tx_state'] == row(1)['hardware_tx_state'] == 'ON'
                  and min(sinks.counts[:2]) > 40000, timeout=15)
            yield api, s, ids, sinks, call, assign, row, saved, send
    finally:
        for sender in senders:
            sender.set_state(gst.State.NULL)
        sinks.close()


def spy_rf(services, monkeypatch):
    configure = Mock(wraps=services.rp.set_tx_config)
    enable = Mock(wraps=services.rp.set_tx_enabled)
    monkeypatch.setattr(services.rp, 'set_tx_config', configure)
    monkeypatch.setattr(services.rp, 'set_tx_enabled', enable)
    return configure, enable


@pytest.mark.parametrize('case', ['active', 'inactive', 'unavailable', 'stop', 'edit', 'rollback'])
def test_source_only_save_on_direct_mux(running, monkeypatch, case):
    api, s, (old, new), sinks, call, assign, row, saved, send = running
    endpoint = '/api/v1/modulators/0/save-and-resume'
    previous = s.sources.pipelines[old]
    old_lease = s.mux.channels[0].revision
    if case == 'active':
        send(1)
        assign(2, new)
        until(lambda: sinks.counts[2] > 40000 and s.sources.pipelines[new]._has_pcm, timeout=10)
    if case == 'rollback':
        def inject_save_failure():
            original = s.database.save

            def save(table, model, record=None):
                if table == 'modulators' and record == 0 and model.input_source_id == new:
                    raise sqlite3.OperationalError('injected save failure')
                return original(table, model, record)

            monkeypatch.setattr(s.database, 'save', save)

        call(inject_save_failure)
    configure, enable = spy_rf(s, monkeypatch)
    result = api.post(endpoint, json={'input_source_id': new})
    assert result.status_code == 200, result.text
    result = result.json()
    assert not result['restarted'] and not result['stop_confirmed']
    shared_before = sinks.counts[1]
    if case == 'rollback':
        assert not result['saved'] and result['failed_stage'] == 'save'
        assert saved()['input_source_id'] == old and s.mux.channels[0].owner is previous
        assert s.mux.channels[0].revision == old_lease
        until(lambda: new not in s.sources.pipelines, timeout=10)
    else:
        assert result['saved'] and result['failed_stage'] is None
        assert result['configuration']['input_source_id'] == saved()['input_source_id'] == new
        candidate = s.sources.pipelines[new]
        assert s.modulators.active_sources[0] == s.mux.channels[0].desired == new
        assert s.rp.enabled[0]
        if case == 'stop':
            assert api.post('/api/v1/modulators/0/stop').status_code == 200
            enable.assert_called_once_with(0, False)
            assert saved()['input_source_id'] == new and not saved()['enabled']
            until(lambda: s.mux.channels[0].owner is None and new not in s.sources.pipelines, timeout=8)
        elif case == 'edit':
            send(1)
            result = api.post(endpoint, json={'frequency_khz': 98300}).json()
            assert result['saved'] and (result['restarted'] or result['restart_pending'])
            until(lambda: s.rp.enabled[0], timeout=15)
            assert [c.args for c in enable.call_args_list] == [(0, False), (0, True)]
            configure.assert_called_once()
            assert saved()['input_source_id'] == new and saved()['frequency_khz'] == 98300
        else:
            until(lambda: s.mux.channels[0].owner is candidate and candidate.direct_applied[1] is not None, timeout=8)
            assert previous.direct_applied == (old_lease+1, None)
            assert candidate.direct_applied[1] == old_lease+2
            if case == 'unavailable':
                before = sinks.counts[0]
                until(lambda: sinks.counts[1] > shared_before + 96000)
                assert sinks.counts[0] == before and not candidate._has_pcm
            else:
                if case == 'inactive':
                    send(1)
                sinks.samples[0].clear()
                def new_pcm():
                    data = bytes(sinks.samples[0])
                    if len(data) >= 4 and abs(struct.unpack_from('<h', data)[0] - 7000) <= 3:
                        return candidate._has_pcm
                    if len(data) >= 4:
                        sinks.samples[0].clear()
                    return False

                until(new_pcm, timeout=10)
            assert row()['hardware_tx_state'] == 'ON' and s.rp.enabled[0]
            assert saved()['rds_follow_source'] and saved()['rds_pi'] == 102 and saved()['rds_ecc'] == 0xE2
            until(lambda: row()['rds_rt_effective'] == 'Profile 1')
            assert row()['rds_rt_effective'] == 'Profile 1'
            assert saved(1)['rds_pi'] == 101 and row(1)['rds_rt_effective'] == 'Profile 0'
            until(lambda: s.rds.channels[0].scheduler.config.rds_pi == 102)
            assert s.modulators.active_sources[0] == new and 0 in s.sources.consumers[new]
            def metadata():
                for pipeline, title in ((candidate, 'New artist'), (previous, 'Old artist')):
                    with pipeline._lock:
                        pipeline._status['stream_title'] = title
                return s.rt.effective(0).text, s.rt.effective(1).text

            assert call(metadata) == ('New artist', 'Old artist')
    if case not in ('stop', 'edit'):
        enable.assert_not_called()
    if case != 'edit':
        configure.assert_not_called()
    until(lambda: sinks.counts[1] > shared_before + 30000)
    assert s.sources.pipelines[old] is previous and s.mux.channels[1].owner is previous
    assert s.rp.enabled[1]


def test_rds_only_save_preserves_direct_worker_and_lease(running, monkeypatch):
    api, s, (old, _), sinks, _, _, _, saved, _ = running
    worker = s.sources.pipelines[old]
    lease = s.mux.channels[0].revision
    before = tuple(sinks.counts[:2])
    configure, enable = spy_rf(s, monkeypatch)
    stop = Mock(wraps=worker.stop)
    monkeypatch.setattr(worker, 'stop', stop)
    assign_lease = Mock(wraps=worker.set_output_lease)
    monkeypatch.setattr(worker, 'set_output_lease', assign_lease)
    base = '/api/v1/modulators/0'
    patch = {'rds_follow_source': False, 'rds_override_profile': saved()['rds_override_profile'] | {
        'rds_pi': 312, 'rds_ecc': 0xE4, 'rds_rt': 'New text', 'rds_pty': 7, 'rds_tp': True,
        'rds_ps': [{'text': 'NEW', 'duration_s': 5}]}}
    for method, url, body in [('POST', base+'/save-and-resume', patch),
                              ('PATCH', base, {'rds_rt': 'Later text'})]:
        response = api.request(method, url, json=body)
        assert response.status_code == 200, response.text
    assert saved()['rds_ps'] == patch['rds_override_profile']['rds_ps']
    for field in ('rds_pi', 'rds_ecc', 'rds_pty', 'rds_tp'):
        assert saved()[field] == patch['rds_override_profile'][field]
    assert saved()['rds_rt'] == 'Later text'
    configure.assert_not_called()
    enable.assert_not_called()
    stop.assert_not_called()
    assign_lease.assert_not_called()
    assert s.sources.pipelines[old] is worker and s.sources.consumers[old] == {0, 1}
    assert s.mux.channels[0].owner is worker and s.mux.channels[0].revision == lease
    until(lambda: all(sinks.counts[ch] > before[ch]+15000 for ch in (0, 1)))
