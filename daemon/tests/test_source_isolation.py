"""External input availability is independent of control-plane health."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.errors import SourceUnavailable
from fmheadendd.models.configuration import InputSourceConfig, ModulatorConfig
from fmheadendd.models.runtime_status import SourceState
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.sources.pipeline import MockPipeline

P = '/api/v1'


def seed(path, kind='LOCAL'):
    db = Database(path)
    inputs = {'LOCAL': {'mode': 'folder', 'path': '/missing/nas'},
              'HTTP': {'uri': 'https://example.org/offline'},
              'ALSA': {'device': 'hw:Missing,0'}}
    ids = []
    for tx in range(8):
        source = db.save('sources', InputSourceConfig(name=f'Source {tx}', type=kind, config=inputs[kind]))
        ids.append(source.id)
        db.save('modulators', ModulatorConfig(input_source_id=source.id, frequency_khz=98100,
            enabled=True, rds_enabled=True, rds_pi=0x1234, rds_rt='Static fallback'), tx)
    db.close()
    return ids


def run(app, operation):
    return app.state.control.executor.submit(operation).result()


@pytest.mark.parametrize('failed', [{5}, set(range(8))])
def test_startup_source_failure_isolated_and_retries_only_failed_audio(tmp_path, failed):
    path = tmp_path/'isolation.db'
    ids = seed(path)
    unavailable = {ids[i] for i in failed}
    class Pipeline(MockPipeline):
        def start(self):
            if self.source.id in unavailable:
                raise SourceUnavailable('FOLDER_UNREADABLE')
            super().start()
    now = [0.0]
    rp = MockRp2354Client(clock=lambda: now[0])
    rp.set_tx_config = Mock(wraps=rp.set_tx_config)
    rp.set_tx_enabled = Mock(wraps=rp.set_tx_enabled)
    rp.get_status = Mock(wraps=rp.get_status)
    app = create_app(Settings(path), pipeline_factory=Pipeline, rp=rp, clock=lambda: now[0])
    with TestClient(app) as api:
        s = app.state.services
        run(app, s.rds.tick)
        run(app, s.status.refresh)
        result = api.get(P+'/status').json()
        assert result['system']['system_state'] == 'RUNNING'
        assert result['system']['controller_communication_state'] == 'AVAILABLE'
        assert not result['system']['controller_observation_stale']
        assert s.recovery.ready and not s.rds.suspended
        assert s.recovery.error is None and rp.get_status.call_count
        for tx, row in enumerate(result['modulators']):
            assert row['hardware_tx_state'] == ('OFF' if tx in failed else 'ON')
            if tx in failed:
                assert row['source_state'] == 'ERROR'
                assert row['source_last_error'] == 'FOLDER_UNREADABLE'
                assert row['last_error'] is None
            else:
                assert row['rds_groups_accepted'] > 0
        config_calls = list(rp.set_tx_config.call_args_list)
        enable_calls = list(rp.set_tx_enabled.call_args_list)
        now[0] = 6
        run(app, s.recovery.service)
        assert rp.set_tx_enabled.call_args_list == enable_calls
        assert rp.set_tx_config.call_args_list == config_calls
        # Inputs return: recover without replaying OFF/config/clear on healthy TXs.
        unavailable.clear()
        now[0] = 12
        for _ in range(8):
            run(app, s.recovery.service)
        run(app, s.status.refresh)
        run(app, s.rds.tick)
        assert all(rp.enabled)
        assert not s.recovery.audio_pending
        assert rp.set_tx_config.call_args_list == config_calls
        assert all(c.args[0] in failed and c.args[1] is True
                   for c in rp.set_tx_enabled.call_args_list[len(enable_calls):])
        assert run(app, s.status.snapshot).system.system_state == 'RUNNING'


@pytest.mark.parametrize('kind', ['HTTP', 'LOCAL', 'ALSA'])
@pytest.mark.parametrize('all_sources', [False, True])
def test_runtime_input_loss_keeps_rf_static_rds_and_controller(tmp_path, kind, all_sources):
    path = tmp_path/'runtime.db'
    ids = seed(path, kind)
    now = [0.0]
    app = create_app(Settings(path), clock=lambda: now[0])
    with TestClient(app):
        s = app.state.services
        s.rp.set_tx_enabled = Mock(wraps=s.rp.set_tx_enabled)
        def fail_inputs():
            for tx in (range(8) if all_sources else [5]):
                row = s.sources.statuses[ids[tx]]
                row.state, row.last_error = SourceState.ERROR, f'{kind} unavailable'
            # Static RDS must not require an active source registration either.
            s.modulators.active_sources.clear()
            s.status.refresh()
            s.rds.tick()
            return s.status.snapshot()
        result = run(app, fail_inputs)
        assert result.system.system_state == 'RUNNING'
        assert result.system.controller_state == 'OK'
        assert result.modulators[5].source_last_error == f'{kind} unavailable'
        assert all(row.hardware_tx_state == 'ON' for row in result.modulators)
        assert all(row.rds_groups_accepted > 0 for row in result.modulators)
        assert run(app, lambda: s.rt.effective(5).text) == 'Static fallback'
        s.rp.set_tx_enabled.assert_not_called()


def test_internal_audio_bug_is_degraded_but_not_controller_unavailable(tmp_path, caplog):
    path = tmp_path/'internal.db'
    ids = seed(path)
    class Broken(MockPipeline):
        def start(self):
            if self.source.id == ids[5]:
                raise RuntimeError('unexpected pipeline bug')
            super().start()
    now = [0.0]
    app = create_app(Settings(path), pipeline_factory=Broken, clock=lambda: now[0])
    with TestClient(app) as api:
        result = api.get(P+'/status').json()
        assert result['system']['system_state'] == 'DEGRADED'
        assert result['system']['controller_communication_state'] == 'AVAILABLE'
        assert result['modulators'][5]['source_error_scope'] == 'INTERNAL'
        assert result['modulators'][6]['hardware_tx_state'] == 'ON'
        assert not app.state.services.rds.suspended
        assert 'internal audio startup failure' in caplog.text
        now[0] = 6
        run(app, app.state.services.recovery.service)
        run(app, app.state.services.status.refresh)
        assert not app.state.services.rp.enabled[5]
        assert api.get(P+'/status').json()['system']['system_state'] == 'DEGRADED'


@pytest.mark.parametrize('component', ['rp', 'mux'])
def test_real_dependency_failure_still_degrades(tmp_path, component):
    app = create_app(Settings(tmp_path/'dependency.db'))
    with TestClient(app) as api:
        s = app.state.services
        getattr(s, component).available = False
        run(app, s.status.refresh)
        assert api.get(P+'/status').json()['system']['system_state'] == 'DEGRADED'
        getattr(s, component).available = True


@pytest.mark.parametrize('edit', [False, True])
def test_failed_explicit_start_retries_with_current_configuration_and_stop_cancels(tmp_path, edit):
    unavailable = [True]
    class Pipeline(MockPipeline):
        def start(self):
            if unavailable[0]:
                raise SourceUnavailable('USB input missing')
            super().start()
    now = [0.0]
    app = create_app(Settings(tmp_path/'explicit.db'), pipeline_factory=Pipeline, clock=lambda: now[0])
    with TestClient(app) as api:
        sid = api.post(P+'/sources', json={'name': 'USB', 'type': 'ALSA',
            'config': {'device': 'hw:Missing,0'}}).json()['id']
        assert api.patch(P+'/modulators/5', json={'input_source_id': sid, 'frequency_khz': 98100}).status_code == 200
        assert api.post(P+'/modulators/5/start').status_code == 503
        s = app.state.services
        run(app, s.status.refresh)
        assert api.get(P+'/status').json()['system']['system_state'] == 'RUNNING'
        assert not s.rp.enabled[5]
        if edit:
            assert api.patch(P+'/modulators/5', json={'frequency_khz': 98700}).status_code == 200
        s.rp.set_tx_config = Mock(wraps=s.rp.set_tx_config)
        unavailable[0] = False
        now[0] = 6
        run(app, s.recovery.service)
        assert s.rp.enabled[5]
        assert s.rp.set_tx_config.call_args.args[1].frequency_khz == (98700 if edit else 98100)
        assert api.post(P+'/modulators/5/stop').status_code == 200
        unavailable[0] = True
        assert api.post(P+'/modulators/5/start').status_code == 503
        assert api.post(P+'/modulators/5/stop').status_code == 200
        unavailable[0] = False
        now[0] = 12
        run(app, s.recovery.service)
        assert not s.rp.enabled[5] and 5 not in s.recovery.audio_pending
