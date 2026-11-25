from datetime import datetime
import threading
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from fmheadendd.app import create_app
from fmheadendd.errors import DomainError
from fmheadendd.models.runtime_status import ModulatorStatus
from fmheadendd.models.schedule import ScheduleRule
from fmheadendd.scheduler import Scheduler, is_due
from fmheadendd.tests.profile_support import profile_settings
from fmheadendd.tests.test_audio_worker import until


SCHEDULES = '/api/v1/schedules'
RULE = dict(recurrence='daily', time='09:00', tx_id=0, action='start')
MONDAY = datetime(2026, 11, 23, 9)


@pytest.mark.parametrize('recurrence,weekdays,expected', [
    ('daily', [], set(range(7))),
    ('weekdays', [], set(range(5))),
    ('weekends', [], {5, 6}),
    ('selected_weekdays', [0, 6], {0, 6}),
])
def test_recurrence(recurrence, weekdays, expected):
    rule = ScheduleRule(id=1, **(RULE | dict(recurrence=recurrence, weekdays=weekdays)))
    assert {day for day in range(7) if is_due(rule, MONDAY.replace(day=23 + day))} == expected
    assert not is_due(rule, MONDAY.replace(minute=1))
    assert not is_due(rule.model_copy(update={'enabled': False}), MONDAY)


def test_no_catch_up_or_duplicate_and_failures_do_not_block_other_rules(caplog):
    now = [MONDAY]
    rules = [
        ScheduleRule(id=1, **RULE),
        ScheduleRule(id=2, **(RULE | dict(time='09:02'))),
        ScheduleRule(id=3, **(RULE | dict(time='09:02', action='stop'))),
        ScheduleRule(id=4, **(RULE | dict(time='09:01'))),
        ScheduleRule(id=5, **(RULE | dict(time='09:02', enabled=False))),
        ScheduleRule(id=6, **(RULE | dict(time='09:02', action='switch_source', source_id=1))),
    ]
    modulators = Mock()
    modulators.start.side_effect = DomainError(409, 'TEST', 'start failed')
    modulators.stop.return_value = ModulatorStatus(id=0)
    modulators.save_and_resume.return_value = dict(saved=True, failed_stage='start', message='restart failed')
    scheduler = Scheduler(Mock(list_schedules=lambda: rules), modulators, Mock(), clock=lambda: now[0])
    scheduler.tick()
    assert not modulators.mock_calls
    for minute, second in [(2, 1), (2, 35), (3, 0), (2, 0)]:
        now[0] = MONDAY.replace(minute=minute, second=second)
        scheduler.tick()
    for day, minute in [(24, 3), (23, 2)]:
        now[0] = MONDAY.replace(day=day, minute=minute)
        scheduler.tick()
    modulators.start.assert_called_once_with(0)
    modulators.stop.assert_called_once_with(0)
    modulators.save_and_resume.assert_called_once()
    assert 'Schedule 2 TX0 start failed' in caplog.text
    assert 'Schedule 6 TX0 switch_source failed: restart failed' in caplog.text


@pytest.fixture
def api(tmp_path):
    settings = profile_settings(tmp_path / 'schedules.db')
    with TestClient(create_app(settings)) as client:
        yield client, settings


def add_source(client, name):
    response = client.post('/api/v1/sources', json={
        'name': name, 'type': 'HTTP', 'config': {'uri': 'http://example.org/live'}})
    assert response.status_code == 201
    return response.json()['id']


def test_crud_and_persistence(api):
    client, settings = api
    assert client.get(SCHEDULES).json() == []
    source = add_source(client, 'Scheduled Source')
    response = client.post(SCHEDULES, json=RULE | dict(action='switch_source', source_id=source))
    assert response.status_code == 201
    rule = response.json()
    path = f"{SCHEDULES}/{rule['id']}"
    assert client.delete(f'/api/v1/sources/{source}').status_code == 409
    changes = dict(enabled=False, time='14:25', recurrence='selected_weekdays', weekdays=[0, 6])
    response = client.patch(path, json=changes)
    assert response.status_code == 200
    assert response.json() == rule | changes
    assert client.get(SCHEDULES).json() == [rule | changes]
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(SCHEDULES).json() == [rule | changes]
    response = client.patch(path, json=dict(enabled=True, action='stop', source_id=None))
    assert response.status_code == 200 and response.json()['enabled'] is True
    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 404
    assert client.patch(path, json=dict(enabled=True)).status_code == 404
    assert client.delete(f'{SCHEDULES}/{2**63}').status_code == 422
    assert client.get(SCHEDULES).json() == []
    assert client.delete(f'/api/v1/sources/{source}').status_code == 204


@pytest.mark.parametrize('changes,status', [
    ({'time': '9:00'}, 422), ({'time': '24:00'}, 422), ({'time': '09:60'}, 422),
    ({'tx_id': 8}, 422), ({'tx_id': True}, 422), ({'action': 'stop_all'}, 422), ({'enabled': 1}, 422),
    ({'recurrence': 'weekly'}, 422), ({'weekdays': [0]}, 422),
    ({'recurrence': 'selected_weekdays'}, 422),
    ({'recurrence': 'selected_weekdays', 'weekdays': [1, 1]}, 422),
    ({'recurrence': 'selected_weekdays', 'weekdays': [7]}, 422),
    ({'action': 'switch_source'}, 422), ({'source_id': 1}, 422),
    ({'action': 'switch_source', 'source_id': 999}, 404),
])
def test_validation(api, changes, status):
    client, _ = api
    assert client.post(SCHEDULES, json=RULE | changes).status_code == status
    assert client.get(SCHEDULES).json() == []


def test_app_dispatch_reuses_manual_operations_on_control_thread(tmp_path, monkeypatch):
    now = [MONDAY.replace(hour=8, minute=59)]
    app = create_app(profile_settings(tmp_path / 'execution.db'), scheduler_clock=lambda: now[0])
    with TestClient(app) as client:
        first = add_source(client, 'First')
        second = add_source(client, 'Second')
        tx = '/api/v1/modulators/0'
        assert client.patch(tx, json=dict(frequency_khz=98100, input_source_id=first)).status_code == 200
        manager = app.state.services.modulators
        owner = app.state.control.executor.submit(threading.get_ident).result(timeout=2)
        calls = []

        def traced(name, operation):
            def invoke(*args):
                assert threading.get_ident() == owner
                result = operation(*args)
                calls.append((name, args))
                return result
            return invoke

        for name in ('start', 'save_and_resume', 'stop'):
            monkeypatch.setattr(manager, name, traced(name, getattr(manager, name)))
        for changes in ({}, dict(time='09:01', action='switch_source', source_id=second),
                        dict(time='09:02', action='stop')):
            assert client.post(SCHEDULES, json=RULE | changes).status_code == 201

        now[0] = MONDAY
        until(lambda: len(calls) == 1)
        assert calls[0] == ('start', (0,))
        assert client.get(tx).json()['enabled']
        now[0] = MONDAY.replace(minute=1)
        until(lambda: len(calls) == 2)
        name, (channel, patch) = calls[1]
        assert (name, channel, patch.model_dump(exclude_unset=True)) == (
            'save_and_resume', 0, {'input_source_id': second})
        config = client.get(tx).json()
        assert config['enabled'] and config['input_source_id'] == second
        now[0] = MONDAY.replace(minute=2)
        until(lambda: len(calls) == 3)
        assert calls[2] == ('stop', (0,))
        assert not client.get(tx).json()['enabled']
