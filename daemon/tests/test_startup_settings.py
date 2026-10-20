"""Startup options are CLI-owned, never persisted system configuration."""
from unittest.mock import Mock

import pytest

from fmheadendd import app
from fmheadendd.config import Settings


def test_startup_defaults():
    args = app.argument_parser().parse_args([])
    assert (args.i2c_device, args.controller_i2c_address) == ('/dev/i2c-1', None)
    assert (args.host, args.port) == ('0.0.0.0', 8080)
    assert args.controller_status_poll_interval_ms == 1000
    assert args.log_level == 'INFO'
    assert args.i2c_timeout_ms == 200
    assert args.mux_runtime_dir == '/run/fmheadend'


@pytest.mark.parametrize('option', ['--rp-i2c-address', '--mock-rp'])
def test_retired_cli_names_are_rejected(option):
    with pytest.raises(SystemExit):
        app.argument_parser().parse_args([option])


def test_help_groups_and_displays_actual_defaults():
    parser = app.argument_parser()
    group = next(g for g in parser._action_groups if g.title == 'Development / Testing')
    assert [a.option_strings for a in group._group_actions] == [
        ['--mock'], ['--mock-controller'], ['--mock-mux']]
    text = ' '.join(parser.format_help().split())
    for action in parser._actions:
        if action.default is not None and action.dest != 'help':
            assert f'(default: {action.default})' in text
    assert '--rp-i2c-address' not in text and '--mock-rp' not in text


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
        'fmheadendd', '--i2c-device', '/dev/i2c-3', '--controller-i2c-address', '0x62',
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
