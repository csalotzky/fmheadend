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
RULE = dict(days_of_week=list(range(7)), time='09:00', tx_id=0, action='start')
MONDAY = datetime(2026, 11, 23, 9)


@pytest.mark.parametrize('days', [list(range(7)), [0, 1, 2, 3, 4], [5, 6], [6, 0], [0]])
def test_due_on_selected_days(days):
    rule = ScheduleRule(id=1, **(RULE | dict(days_of_week=days)))
    assert {day for day in range(7) if is_due(rule, MONDAY.replace(day=23 + day))} == set(days)
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
    conflict = {'error': 'RESOURCE_IN_USE', 'message': 'Cannot delete source: it is used by a scheduler rule.'}
    response = client.delete(f'/api/v1/sources/{source}')
    assert response.status_code == 409 and response.json() == conflict
    changes = dict(enabled=False, time='14:25', days_of_week=[0, 6])
    response = client.patch(path, json=changes)
    assert response.status_code == 200
    assert response.json() == rule | changes
    assert client.get(SCHEDULES).json() == [rule | changes]
    response = client.delete(f'/api/v1/sources/{source}')
    assert response.status_code == 409 and response.json() == conflict
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(SCHEDULES).json() == [rule | changes]
    response = client.patch(path, json=dict(enabled=True, action='stop', source_id=None))
    assert response.status_code == 200
    assert response.json() == rule | changes | dict(enabled=True, action='stop', source_id=None)
    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 404
    assert client.patch(path, json=dict(enabled=True)).status_code == 404
    assert client.delete(f'{SCHEDULES}/{2**63}').status_code == 422
    assert client.get(SCHEDULES).json() == []
    assert client.delete(f'/api/v1/sources/{source}').status_code == 204


@pytest.mark.parametrize('changes,status', [
    ({'time': '9:00'}, 422), ({'time': '24:00'}, 422), ({'time': '09:60'}, 422),
    ({'tx_id': 8}, 422), ({'tx_id': True}, 422), ({'action': 'stop_all'}, 422), ({'enabled': 1}, 422),
    ({'action': 'switch_source'}, 422), ({'action': 'stop', 'source_id': 1}, 422),
    ({'source_id': 999}, 404),
    ({'action': 'switch_source', 'source_id': 999}, 404),
])
def test_validation(api, changes, status):
    client, _ = api
    assert client.post(SCHEDULES, json=RULE | changes).status_code == status
    assert client.get(SCHEDULES).json() == []


def test_days_of_week_is_required_on_create(api):
    client, _ = api
    assert client.post(SCHEDULES, json={k: v for k, v in RULE.items() if k != 'days_of_week'}).status_code == 422


@pytest.mark.parametrize('invalid', [None, [], [1, 1], [-1], [7], [True], ['1'], [1.0], list(range(7)) + [0]])
def test_days_of_week_validation_on_create_and_patch(api, invalid):
    client, _ = api
    changes = {'days_of_week': invalid}
    assert client.post(SCHEDULES, json=RULE | changes).status_code == 422
    saved = client.post(SCHEDULES, json=RULE).json()
    assert client.patch(f"{SCHEDULES}/{saved['id']}", json=changes).status_code == 422
    assert client.get(SCHEDULES).json() == [saved]


@pytest.mark.parametrize('failure', ['unsaved', 'restart', 'exception'])
def test_start_is_skipped_when_source_assignment_fails(failure, caplog):
    now = [MONDAY.replace(hour=8, minute=59)]
    rule = ScheduleRule(id=1, **(RULE | {'source_id': 42}))
    modulators = Mock()
    if failure == 'exception':
        modulators.save_and_resume.side_effect = DomainError(409, 'TEST', 'assignment failed')
    else:
        modulators.save_and_resume.return_value = dict(
            saved=failure == 'restart', failed_stage='start' if failure == 'restart' else None,
            message='assignment failed')
    scheduler = Scheduler(Mock(list_schedules=lambda: [rule]), modulators, Mock(), clock=lambda: now[0])
    now[0] = MONDAY
    scheduler.tick()
    modulators.save_and_resume.assert_called_once()
    modulators.start.assert_not_called()
    assert 'assignment failed' in caplog.text


def test_start_source_can_be_cleared_and_modulator_reference_is_preserved(api):
    client, _ = api
    source = add_source(client, 'Station')
    response = client.post(SCHEDULES, json=RULE | {'source_id': source})
    assert response.status_code == 201 and response.json()['source_id'] == source
    path = f"{SCHEDULES}/{response.json()['id']}"
    assert client.patch(path, json={'action': 'stop'}).status_code == 422
    response = client.patch(path, json={'source_id': None})
    assert response.status_code == 200 and response.json()['source_id'] is None
    assert client.patch('/api/v1/modulators/0', json={'input_source_id': source}).status_code == 200
    response = client.delete(f'/api/v1/sources/{source}')
    assert response.status_code == 409
    assert response.json() == {
        'error': 'RESOURCE_IN_USE', 'message': 'Resource is referenced by another configuration'}


def test_app_dispatch_reuses_manual_operations_on_control_thread(tmp_path, monkeypatch):
    now = [MONDAY.replace(hour=8, minute=59)]
    app = create_app(profile_settings(tmp_path / 'execution.db'), scheduler_clock=lambda: now[0])
    with TestClient(app) as client:
        first = add_source(client, 'First')
        second = add_source(client, 'Second')
        tx = '/api/v1/modulators/0'
        assert client.patch(tx, json=dict(frequency_khz=98100)).status_code == 200
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
        for changes in (dict(source_id=first), dict(time='09:01', action='switch_source', source_id=second),
                        dict(time='09:02', action='stop')):
            assert client.post(SCHEDULES, json=RULE | changes).status_code == 201

        now[0] = MONDAY
        until(lambda: len(calls) == 2)
        name, (channel, patch) = calls[0]
        assert (name, channel, patch.model_dump(exclude_unset=True)) == (
            'save_and_resume', 0, {'input_source_id': first})
        assert calls[1] == ('start', (0,))
        config = client.get(tx).json()
        assert config['enabled'] and config['input_source_id'] == first
        now[0] = MONDAY.replace(minute=1)
        until(lambda: len(calls) == 3)
        name, (channel, patch) = calls[2]
        assert (name, channel, patch.model_dump(exclude_unset=True)) == (
            'save_and_resume', 0, {'input_source_id': second})
        config = client.get(tx).json()
        assert config['enabled'] and config['input_source_id'] == second
        now[0] = MONDAY.replace(minute=2)
        until(lambda: len(calls) == 4)
        assert calls[3] == ('stop', (0,))
        assert not client.get(tx).json()['enabled']
