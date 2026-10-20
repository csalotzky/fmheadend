"""Startup options are CLI-owned, never persisted system configuration."""
from unittest.mock import Mock

import pytest

from fmheadendd import app
from fmheadendd.config import Settings


def test_startup_defaults():
    args = app.argument_parser().parse_args([])
    assert (args.i2c_device, args.rp_i2c_address) == ('/dev/i2c-1', None)
    assert (args.host, args.port) == ('0.0.0.0', 8080)
    assert args.controller_status_poll_interval_ms == 1000
    assert args.log_level == 'INFO'
    assert args.i2c_timeout_ms == 200
    assert args.mux_runtime_dir == '/run/fmheadend'


@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'invalid'])
def test_poll_interval_rejects_invalid_cli_values(value):
    with pytest.raises(SystemExit):
        app.argument_parser().parse_args(['--controller-status-poll-interval-ms', value])


def test_runtime_settings_reject_invalid_poll_interval():
    with pytest.raises(ValueError, match='poll interval'):
        Settings(controller_status_poll_interval_ms=0)


def test_main_uses_cli_without_reading_persisted_startup_settings(monkeypatch):
    import uvicorn

    monkeypatch.setattr('sys.argv', [
        'fmheadendd', '--i2c-device', '/dev/i2c-3', '--rp-i2c-address', '0x62',
        '--host', '127.0.0.1', '--port', '8090',
        '--controller-status-poll-interval-ms', '2500', '--log-level', 'DEBUG',
    ])
    database = Mock(side_effect=AssertionError('startup must not read SystemConfig'))
    rp, create, run, logging = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(app, 'Database', database)
    monkeypatch.setattr(app, 'Rp2354Client', rp)
    monkeypatch.setattr(app, 'create_app', create)
    monkeypatch.setattr(app.logging, 'basicConfig', logging)
    monkeypatch.setattr(uvicorn, 'run', run)
    app.main()
    database.assert_not_called()
    rp.assert_called_once_with('/dev/i2c-3', 98, timeout_ms=200)
    assert create.call_args.args[0].controller_status_poll_interval_ms == 2500
    assert logging.call_args.kwargs['level'] == 'DEBUG'
    run.assert_called_once_with(create.return_value, host='127.0.0.1', port=8090, log_level='debug')
