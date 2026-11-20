"""Control-owned worker recovery; service() never waits for child exit."""
from dataclasses import dataclass
import logging
from .diagnostic_events import redact
from ..errors import DependencyUnavailable, SourcePending
from ..models.runtime_status import InputSourceStatus

log = logging.getLogger(__name__)
COOLDOWNS = (1, 2, 5, 10, 30, 60, 120)

@dataclass(frozen=True)
class RecoveryPolicy:
    status_timeout: float = 5.0
    pcm_timeout: float = 15.0
    buffering_timeout: float = 90.0
    network_idle_timeout: float = 90.0

class WorkerRecovery:
    def __init__(self, manager, policy=None):
        self.manager = manager
        self.policy = policy or RecoveryPolicy()
        # Layout shared with SourceManager: due=None means retiring, not reaped yet.
        self.pending = {}  # source -> (owned adapter, latest desired or None, due, automatic)
        self.attempts = {}  # consecutive failures, never an exhausted restart budget
        self.stability = {}
        self._reap_warning = {}

    def request(self, sid, desired=None, *, automatic=False):
        m = self.manager
        old = m.pipelines[sid]
        self.stability.pop(sid, None)
        if automatic:
            self.attempts[sid] = min(self.attempts.get(sid, 0)+1, 2**63-1)
        self.pending[sid] = (old, desired, None if automatic else m.clock(), automatic)
        try:
            self._stop(old, desired, automatic)
        except DependencyUnavailable:
            pass

    @staticmethod
    def _retain(old, desired, automatic):
        return (getattr(old, '_podcast_service', None) is not None
            and getattr(old, '_service_cleanup', None) is None) and (automatic or
            (desired is not None and desired.type == 'PODCAST' and desired.config == old.source.config))

    def _stop(self, old, desired, automatic):
        if self._retain(old, desired, automatic):
            old.stop(retain_service=True)
        else:
            old.stop()

    def _stable(self, sid, old, observation, now):
        """Conservative sampled stability; never count an unobserved interval."""
        if not self.attempts.get(sid):
            return
        status_at, pcm_at, state, fatal, done = observation
        healthy = (not fatal and not done and state == 'RUNNING'
                   and getattr(old, '_has_pcm', False)
                   and not getattr(old, 'expected_media_wait', False)
                   and not getattr(old, '_pcm_error', None)
                   and 0 <= now-status_at <= self.policy.status_timeout
                   and 0 <= now-pcm_at <= 2)
        if getattr(old, 'direct_runtime', None) is not None:
            active = [ch for ch, (_, lease) in old._direct_intents.items() if lease is not None]
            progress = old.direct_progress
            if active:
                healthy = healthy and (0 <= now-progress.get('writer_at', -1e30) <= 2
                    and all(progress.get('connections', {}).get(str(ch)) is True for ch in active))
        previous = self.stability.get(sid)
        if not healthy:
            self.stability.pop(sid, None)
            return
        if (previous is None or previous[0] is not old or not 0 <= now-previous[2] <= 2
                or pcm_at < previous[3]):
            self.stability[sid] = (old, now, now, pcm_at)
            return
        _, since, _, last_pcm = previous
        self.stability[sid] = (old, since, now, pcm_at)
        if pcm_at > last_pcm and pcm_at-since >= 300:
            count = self.attempts.pop(sid)
            self.stability.pop(sid, None)
            log.info('source=%s generation=%s category=worker_failure_counter_reset consecutive_failures=%s healthy_pcm_s=%.3f',
                     sid, getattr(old, 'generation', 'unknown'), count, pcm_at-since)

    def service(self):
        m, p = self.manager, self.policy
        now = m.clock()
        for sid, old in list(m.pipelines.items()):
            old._http_retry_window = p.buffering_timeout
            if not hasattr(old, 'recovery_observation'):
                continue
            # Qualification only: never feed shadow decisions into the policy below.
            if hasattr(old, 'native_health') and m.consumers.get(sid):
                from .source_liveness import evaluate
                evaluate(old)
            if sid in self.pending:
                owned, desired, due, automatic = self.pending[sid]
                if owned is not old:
                    continue
                if not old.recovery_observation()[-1]:
                    continue
                try:
                    self._stop(old, desired, automatic)  # Confirms actual reap/service cleanup.
                except DependencyUnavailable as exc:
                    if now >= self._reap_warning.get(sid, 0):
                        self._reap_warning[sid] = now+300
                        log.error('source=%s generation=%s category=worker_reap_pending reason=%s replacement_blocked=true',
                                  sid, getattr(old, 'generation', 'unknown'), redact(exc))
                    continue
                self._reap_warning.pop(sid, None)
                if not m.consumers.get(sid):
                    with m._registry_lock:
                        m.pipelines.pop(sid)
                        m.consumers.pop(sid, None)
                    m.statuses[sid] = InputSourceStatus(source_id=sid)
                    m.restart_errors.pop(sid, None)
                    self.pending.pop(sid, None)
                    self.attempts.pop(sid, None)
                    self.stability.pop(sid, None)
                    continue
                if automatic and due is None:
                    delay = COOLDOWNS[min(self.attempts[sid]-1, len(COOLDOWNS)-1)]
                    due = m.clock()+delay
                    self.pending[sid] = (old, desired, due, automatic)
                    reason = m.restart_errors.get(sid) or getattr(old, 'fatal_error', None) or 'Worker failure'
                    message = f'Audio worker cooldown {delay}s; automatic retry at monotonic {due:.3f}: {reason}'
                    m.statuses[sid] = InputSourceStatus(source_id=sid, state='ERROR', last_error=message, error_scope='INTERNAL')
                    m.restart_errors[sid] = str(reason)
                    log.warning('source=%s generation=%s category=worker_cooldown consecutive_failures=%s delay_s=%s next_retry_monotonic=%.3f reason=%s',
                                sid, getattr(old, 'generation', 'unknown'), self.attempts[sid], delay, due, redact(reason))
                if now < due:
                    continue
                # Remove only the completed request; failure installs a NEW owned request below.
                self.pending.pop(sid)
                try:
                    options = dict(output=old.output)
                    if self._retain(old, desired, automatic):
                        options['podcast_service'] = old._podcast_service
                    replacement = old.fresh() if desired is None else m._new_pipeline(desired, **options)
                    if hasattr(replacement, 'recovery_observation'):
                        replacement.managed_recovery = True
                    replacement._diagnostic_outage_since = getattr(old, '_diagnostic_outage_since', None)
                    m._publish_pipeline(sid, replacement)
                    log.info('source=%s generation=%s category=worker_replacement old_generation=%s automatic=%s',
                             sid, getattr(replacement, 'generation', 'unknown'), getattr(old, 'generation', 'unknown'), automatic)
                    try:
                        replacement.start()
                    except SourcePending:
                        pass
                    m.restart_errors.pop(sid, None)
                except Exception as exc:
                    log.error('source=%s generation=%s category=replacement_failed reason=%s', sid,
                              getattr(m.pipelines[sid], 'generation', 'unknown'), redact(exc))
                    m.restart_errors[sid] = str(exc)
                    m.statuses[sid] = InputSourceStatus(source_id=sid, state='ERROR', last_error=str(exc), error_scope='INTERNAL')
                    self.request(sid, desired, automatic=True)
                continue
            if not m.consumers.get(sid):
                continue
            observation = old.recovery_observation()
            status_at, pcm_at, state, fatal, done = observation
            self._stable(sid, old, observation, now)
            limit = p.buffering_timeout if state in ('BUFFERING', 'CONNECTING') else p.pcm_timeout
            progress_at = pcm_at
            source_type = getattr(getattr(old, 'source', None), 'type', None)
            if source_type in ('UDP', 'RTP'):
                limit = p.network_idle_timeout
                # A native receive timeout proves a bounded interval of input
                # silence. Use its original timestamp, never report receipt.
                idle_at = getattr(old, 'network_idle_at', None)
                # The IPC reader may publish after the cycle's initial clock
                # sample. Check against a clock read after taking that sample.
                if type(idle_at) in (float, int) and 0 < idle_at <= m.clock():
                    progress_at = max(pcm_at, idle_at)
            # Real process adapters distinguish cold initialization from an
            # established reporter. Class lookup avoids inventing this optional
            # contract on legacy adapters or dynamic partial mocks.
            status_check = getattr(type(old), 'status_timeout_reason', None)
            status_reason = None
            if not done and not fatal:
                status_reason = (status_check(old, m.clock, p.status_timeout) if status_check else
                    'Worker status timeout' if now-status_at > p.status_timeout else None)
            reason = 'Worker exited' if done else fatal or status_reason
            if reason is None and now-progress_at > limit and not getattr(old, 'expected_media_wait', False):
                reason = 'Worker PCM progress timeout'
            if reason == 'Worker PCM progress timeout':
                if source_type == 'ALSA':
                    alsa_wait = getattr(type(old), 'alsa_retry_allows_wait', None)
                    if alsa_wait and alsa_wait(old, m.clock, p.buffering_timeout):
                        reason = None
                else:
                    from .source_liveness import healthy_http_retry_exemption
                    if healthy_http_retry_exemption(old, now, p.status_timeout, clock=m.clock):
                        reason = None
            if reason:
                log.error('source=%s generation=%s category=worker_recovery reason=%s state=%s status_age_s=%.3f pcm_age_s=%.3f pcm_limit_s=%s',
                          sid, getattr(old, 'generation', 'unknown'), redact(reason), state, now-status_at, now-pcm_at, limit)
                old._fail(reason)
                self.request(sid, automatic=True)
