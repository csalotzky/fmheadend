import logging
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.diagnostics import RecentLogs, error_scope
from fmheadendd.rp2354.protocol import Command, HardwareUnavailable, QnError, Status


def test_recent_logs_bounded_order_copy_and_message_limit():
    logs = RecentLogs()
    assert logs.snapshot() == []
    for i in range(105):
        logs.handle(logging.LogRecord('fmheadendd.test', logging.WARNING, '', 0, 'entry %s', (i,), None))
    rows = logs.snapshot()
    assert len(rows) == 100
    assert [r['message'] for r in rows] == [f'entry {i}' for i in range(104, 4, -1)]
    assert rows[0]['severity'] == 'WARNING' and rows[0]['logger'] == 'fmheadendd.test'
    assert rows[0]['timestamp'].endswith('+00:00')
    rows[0]['message'] = 'mutated'
    assert logs.snapshot()[0]['message'] == 'entry 104'
    logs.handle(logging.LogRecord('fmheadendd.test', logging.ERROR, '', 0, 'x'*5000, (), None))
    assert len(logs.snapshot()[0]['message']) == 4096


def test_log_endpoint_uses_existing_logging_and_detaches(tmp_path):
    parent = logging.getLogger('fmheadendd')
    handlers, level = list(parent.handlers), parent.level
    app = create_app(Settings(tmp_path / 'logs.db'))
    with TestClient(app) as api:
        for i in range(105): logging.getLogger('fmheadendd.test').warning('incident %s', i)
        response = api.get('/api/v1/system/logs')
        assert response.status_code == 200
        assert len(response.json()) == 100
        assert response.json()[0]['message'] == 'incident 104'
        assert response.json()[-1]['message'] == 'incident 5'
        assert parent.level == level
    assert parent.handlers == handlers


@pytest.mark.parametrize('command,stage,expected', [
    (Command.GET_INFO, 'payload', 'GLOBAL'), (Command.GET_STATUS, 'payload', 'GLOBAL'),
    (Command.SET_TX_ENABLED, 'backoff', 'GLOBAL'), (Command.RDS_CLEAR, 'open', 'GLOBAL'),
    (Command.SET_TX_CONFIG, 'payload', 'TX'), (Command.RDS_PUSH, 'payload', 'TX'),
    (Command.QN_READ_REGISTER, 'payload', 'TX'),
])
def test_error_scope_uses_metadata_not_message(command, stage, expected):
    error = HardwareUnavailable('identical diagnostic text')
    error.command, error.stage = command, stage
    assert error_scope(error) == expected
    assert error_scope(QnError(Command.SET_TX_CONFIG, Status.QN_ERROR)) == 'TX'
    assert error_scope(ValueError('source configuration invalid')) == 'TX'
