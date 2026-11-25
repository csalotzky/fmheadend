"""Run due rules on the existing control thread."""
from datetime import datetime
import logging
import sqlite3

from .models.configuration import ModulatorPatch


logger = logging.getLogger(__name__)


def is_due(rule, minute):
    if not rule.enabled or rule.time != minute.strftime('%H:%M'):
        return False
    day = minute.weekday()
    return (rule.recurrence == 'daily'
            or rule.recurrence == 'weekdays' and day < 5
            or rule.recurrence == 'weekends' and day >= 5
            or rule.recurrence == 'selected_weekdays' and day in rule.weekdays)


class Scheduler:
    def __init__(self, database, modulators, publish_status, *, clock=datetime.now):
        self.database = database
        self.modulators = modulators
        self.publish_status = publish_status
        self.clock = clock
        # The minute already in progress at startup has been missed.
        self.last_minute = clock().replace(second=0, microsecond=0, tzinfo=None)
        self.handled_minutes = {self.last_minute}

    def tick(self):
        minute = self.clock().replace(second=0, microsecond=0, tzinfo=None)
        if minute == self.last_minute or minute in self.handled_minutes:
            return
        self.last_minute = minute
        try:
            due = [rule for rule in self.database.list_schedules() if is_due(rule, minute)]
        except (sqlite3.Error, ValueError):
            logger.exception('Could not read schedules for %s', minute.isoformat(timespec='minutes'))
            return
        if due:
            # Remember only minutes with attempted actions, including failures.
            # A wall-clock rollback can repeat the same minute after midnight.
            self.handled_minutes.add(minute)
        for rule in due:
            try:
                self.execute(rule)
            except Exception:
                logger.exception('Schedule %s TX%s %s failed', rule.id, rule.tx_id, rule.action)
            finally:
                self.publish_status()

    def execute(self, rule):
        if rule.action == 'switch_source':
            result = self.modulators.save_and_resume(rule.tx_id, ModulatorPatch(input_source_id=rule.source_id))
            error = result['message'] if result['failed_stage'] or not result['saved'] else None
            pending = result.get('restart_pending', False)
        else:
            operation = self.modulators.start if rule.action == 'start' else self.modulators.stop
            result = operation(rule.tx_id)
            error = result.last_error or result.start_error
            pending = result.start_pending
        if error:
            logger.warning('Schedule %s TX%s %s failed: %s', rule.id, rule.tx_id, rule.action, error)
        else:
            logger.info('Schedule %s TX%s %s %s', rule.id, rule.tx_id, rule.action,
                        'pending' if pending else 'completed')
