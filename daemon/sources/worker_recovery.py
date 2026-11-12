"""Control-owned HTTP worker recovery; service() never waits for child exit."""
from collections import deque
from dataclasses import dataclass

from ..errors import DependencyUnavailable, SourcePending
from ..models.runtime_status import InputSourceStatus


@dataclass(frozen=True)
class RecoveryPolicy:
    status_timeout: float = 5.0
    pcm_timeout: float = 15.0
    buffering_timeout: float = 90.0
    restart_limit: int = 3
    restart_window: float = 300.0
    backoff: float = 1.0


class WorkerRecovery:
    def __init__(self, manager, policy=None):
        self.manager = manager
        self.policy = policy or RecoveryPolicy()
        self.pending = {}  # source -> (owned old adapter, desired source or None, due, automatic)
        self.attempts = {}

    def request(self, sid, desired=None, *, automatic=False):
        m = self.manager
        old = m.pipelines[sid]
        due = m.clock()
        if desired is not None and not automatic:
            self.attempts.pop(sid, None)
        if automatic:
            history = self.attempts.setdefault(sid, deque())
            while history and due - history[0] >= self.policy.restart_window:
                history.popleft()
            if len(history) >= self.policy.restart_limit:
                old.recovery_exhausted = True
                old._fail('Audio worker restart budget exhausted')
                try:
                    old.stop()
                except DependencyUnavailable:
                    pass
                m.restart_errors[sid] = 'Audio worker restart budget exhausted'
                m.statuses[sid] = InputSourceStatus(source_id=sid, state='ERROR',
                    last_error=m.restart_errors[sid], error_scope='INTERNAL')
                return
            due += self.policy.backoff * 2 ** len(history)
            history.append(m.clock())
        self.pending[sid] = (old, desired, due, automatic)
        try:
            old.stop()
        except DependencyUnavailable:
            pass

    def service(self):
        m, p = self.manager, self.policy
        now = m.clock()
        for sid, old in list(m.pipelines.items()):
            if not hasattr(old, 'recovery_observation'):
                continue
            if sid in self.pending:
                owned, desired, due, automatic = self.pending[sid]
                if owned is not old:
                    continue
                if not old.recovery_observation()[-1] or now < due:
                    continue
                # stop() confirms completion; never overlap a live generation.
                try:
                    old.stop()
                    if not m.consumers.get(sid):
                        with m._registry_lock:
                            m.pipelines.pop(sid)
                            m.consumers.pop(sid, None)
                        m.statuses[sid] = InputSourceStatus(source_id=sid)
                        self.attempts.pop(sid, None)
                    else:
                        replacement = old.fresh() if automatic else m._new_pipeline(desired, output=old.output)
                        if hasattr(replacement, 'recovery_observation'):
                            replacement.managed_recovery = True
                        m._publish_pipeline(sid, replacement)
                        try:
                            replacement.start()
                        except SourcePending:
                            pass
                    m.restart_errors.pop(sid, None)
                except Exception as exc:
                    m.restart_errors[sid] = str(exc)
                    m.statuses[sid] = InputSourceStatus(source_id=sid, state='ERROR',
                        last_error=str(exc), error_scope='INTERNAL')
                    old.recovery_exhausted = True
                    current = m.pipelines.get(sid)
                    if hasattr(current, 'recovery_exhausted'):
                        current.recovery_exhausted = True
                        try:
                            current.stop()
                        except DependencyUnavailable:
                            pass
                finally:
                    self.pending.pop(sid, None)
                continue
            if old.recovery_exhausted or not m.consumers.get(sid):
                continue
            status_at, pcm_at, state, fatal, done = old.recovery_observation()
            limit = p.buffering_timeout if state in ('BUFFERING', 'CONNECTING') else p.pcm_timeout
            reason = ('Worker exited' if done else fatal or
                ('Worker status timeout' if now - status_at > p.status_timeout else
                 'Worker PCM progress timeout' if now - pcm_at > limit and
                    not getattr(old, 'expected_media_wait', False) else None))
            if reason:
                old._fail(reason)
                self.request(sid, automatic=True)
