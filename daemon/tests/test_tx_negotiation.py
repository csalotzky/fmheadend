"""Capability and mux lifecycle only: no media, sockets or hardware."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from fmheadendd.errors import DependencyUnavailable
from fmheadendd.recovery import Recovery
from fmheadendd.mux.client import MockMuxClient, validate_stream_status
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.rp2354 import protocol as p
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.rp2354.linux_client import Rp2354Client
from fmheadendd.database import Database
from fmheadendd.modulators.manager import ModulatorManager
from fmheadendd.config import Settings


def info(count):
    return dict(rp_protocol_version=1, modulator_count=count)


def setup(count=4, mux=None, rp=None):
    mux = mux or MockMuxClient()
    rp = rp or MockRp2354Client()
    now = [0.0]
    manager = ModulatorManager(Database(':memory:'), Mock(), rp, mux, Settings())
    recovery = Recovery(manager, Mock(), lambda: now[0])
    manager.recovery = recovery
    if count is not None:
        recovery._record_info(info(count))
        recovery.error = None
    for name in ('pause', 'resume', 'set_tx_count'):
        setattr(mux, name, Mock(wraps=getattr(mux, name)))
    return recovery, mux, now


@pytest.mark.parametrize('count', range(1, 9))
def test_all_counts(count):
    assert p.parse_info(p.INFO.pack(1, b'fw', b'hw', count))['modulator_count'] == count
    recovery, mux, _ = setup(count)
    result = recovery.reconcile_mux(mux.get_status())
    assert result['stream_state'] == 'STREAMING'
    assert result['spi_frame_bytes'] == 4 + 512 * count
    mux.set_tx_count.assert_called_once_with(count)
    recovery.reconcile_mux(mux.get_status())
    mux.set_tx_count.assert_called_once()
    mux.pause.assert_not_called()
    mux.resume.assert_not_called()


@pytest.mark.parametrize('payload', [b'', p.INFO.pack(1, b'fw', b'hw', 0),
    p.INFO.pack(1, b'fw', b'hw', 9), p.INFO.pack(2, b'fw', b'hw', 8)])
def test_bad_info(payload):
    with pytest.raises(p.ProtocolError):
        p.parse_info(payload)


def test_order_and_restart():
    mux = MockMuxClient()
    mux.set_tx_count(8)
    recovery, mux, _ = setup(4, mux)
    events = Mock()
    for name in ('pause', 'set_tx_count', 'resume'):
        events.attach_mock(getattr(mux, name), name)
    for expected in ('PAUSED', 'PAUSED', 'STREAMING'):
        assert recovery.reconcile_mux(mux.get_status())['stream_state'] == expected
    assert [c[0] for c in events.mock_calls] == ['pause', 'set_tx_count', 'resume']
    events.reset_mock()
    recovery.reconcile_mux(mux.get_status())
    assert not events.mock_calls
    mux.tx_count, mux.stream_state = None, 'WAITING_FOR_CONFIG'  # New mux process.
    recovery.reconcile_mux(mux.get_status())
    mux.set_tx_count.assert_called_once_with(4)


def test_unknown_or_stale_capability_never_configures():
    recovery, mux, _ = setup(None)
    recovery.reconcile_mux(mux.get_status())
    mux.set_tx_count.assert_not_called()
    recovery._record_info(info(4))
    recovery.communication_failed(DependencyUnavailable('I2C failure'))
    recovery.reconcile_mux(mux.get_status())
    mux.set_tx_count.assert_not_called()
    assert recovery.verified_info == info(4)


def test_backend_first_and_lost_ack():
    recovery, mux, now = setup()
    mux.available = False
    with pytest.raises(DependencyUnavailable):
        mux.get_status()
    mux.available = True
    original = mux.set_tx_count
    def lost_ack(count):
        original(count)
        raise DependencyUnavailable('Timeout after application')
    mux.set_tx_count = Mock(side_effect=lost_ack)
    with pytest.raises(DependencyUnavailable):
        recovery.reconcile_mux(mux.get_status())
    now[0] = 2
    assert recovery.reconcile_mux(mux.get_status())['stream_state'] == 'STREAMING'
    mux.set_tx_count.assert_called_once()


def test_ack_without_application_and_backoff():
    recovery, mux, _ = setup()
    mux.set_tx_count = Mock()
    with pytest.raises(DependencyUnavailable):
        recovery.reconcile_mux(mux.get_status())
    with pytest.raises(DependencyUnavailable):
        recovery.reconcile_mux(mux.get_status())
    mux.set_tx_count.assert_called_once()


@pytest.mark.parametrize('status', [{}, {'stream_state': 'STREAMING', 'tx_count': True, 'spi_frame_bytes': 516},
    {'stream_state': 'PAUSED', 'tx_count': 4, 'spi_frame_bytes': 4100},
    {'stream_state': 'WAITING_FOR_CONFIG', 'tx_count': 8, 'spi_frame_bytes': 4100}])
def test_invalid_mux_status(status):
    with pytest.raises(DependencyUnavailable):
        validate_stream_status(status)


@pytest.mark.parametrize('reply', [{'ok': False}, {'ok': 1}, {}, {'error': 'INVALID_STATE'}])
def test_negative_or_malformed_ack(reply):
    mux = UnixMuxClient(lambda _: None)
    mux._request = Mock(return_value=reply)
    with pytest.raises(DependencyUnavailable):
        mux.pause()


def test_implicit_handshake_cannot_bypass_backend_guard():
    rp = Rp2354Client('/unused', 0x62)
    recovery, _, _ = setup(None, rp=rp)
    rp._exchange = Mock(return_value=info(4))
    with pytest.raises(DependencyUnavailable):
        rp.set_tx_enabled(7, True)
    assert [c.args[0] for c in rp._exchange.call_args_list] == [p.Command.GET_INFO]
    assert recovery.verified_info == info(4)
    assert not recovery.ready
    rp._exchange.reset_mock()
    with pytest.raises(DependencyUnavailable):
        rp.set_tx_enabled(7, False)
    rp._exchange.assert_not_called()


def test_small_startup_targets_only_physical_channels():
    rp = MockRp2354Client()
    rp.get_info = Mock(return_value=info(3))
    rp.set_tx_enabled = Mock()
    rp.set_tx_config = Mock()
    rp.clear_rds = Mock()
    recovery, mux, _ = setup(None, rp=rp)
    recovery.observe()
    recovery.observe()
    recovery.service()
    rp.get_info.assert_called_once()
    assert {c.args[0] for c in rp.set_tx_enabled.call_args_list} == {0, 1, 2}
    assert {c.args[0] for c in rp.clear_rds.call_args_list} == {0, 1, 2}
    assert recovery.ready
    assert recovery.reconcile_mux(mux.get_status())['tx_count'] == 3


@pytest.mark.parametrize('previous,current', [(8, 8), (8, 4), (4, 8)])
def test_reopened_session_coordinates_before_targeted_command(previous, current):
    rp = Rp2354Client('/unused', 0x62)
    recovery, mux, _ = setup(previous, rp=rp)
    mux.set_tx_count(previous)
    mux.set_tx_count.reset_mock()
    rp._exchange = Mock(return_value=info(current))
    if previous != current:
        with pytest.raises(DependencyUnavailable):
            rp.set_tx_enabled(0, True)
        assert [c.args[0] for c in rp._exchange.call_args_list] == [p.Command.GET_INFO]
    else:
        rp.set_tx_enabled(0, True)
        assert [c.args[0] for c in rp._exchange.call_args_list] == [p.Command.GET_INFO, p.Command.SET_TX_ENABLED]
    for _ in range(3):
        recovery.reconcile_mux(mux.get_status())
    assert mux.tx_count == current and mux.stream_state == 'STREAMING'
    if previous == current:
        mux.pause.assert_not_called()
        mux.resume.assert_not_called()
        mux.set_tx_count.assert_not_called()


def test_small_board_only_disables_absent_saved_configuration(tmp_path):
    from fastapi.testclient import TestClient
    from fmheadendd.app import create_app
    from fmheadendd.config import Settings
    from fmheadendd.database import Database
    path = tmp_path / 'config.db'
    db = Database(path)
    old = db.get('modulators', 7)
    db.save('modulators', old.model_copy(update={'enabled': True}), 7)
    before = db.get('modulators', 7).model_dump()
    db.close()
    rp = MockRp2354Client()
    original = rp.get_info()
    rp.get_info = Mock(return_value=original | {'modulator_count': 4})
    rp.set_tx_enabled = Mock()
    app = create_app(Settings(path), rp=rp)
    with TestClient(app):
        assert app.state.services.info['modulator_count'] == 4
        assert app.state.services.recovery.ready
        assert app.state.services.mux.tx_count == 4
    assert all(c.args[0] < 4 for c in rp.set_tx_enabled.call_args_list)
    db = Database(path)
    assert db.get('modulators', 7).model_dump() == before | {'enabled': False}
    db.close()
