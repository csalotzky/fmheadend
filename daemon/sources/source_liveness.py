"""Bounded HTTP retry exemption; native recovery decisions remain shadow-only."""
import time
from .diagnostic_events import log
from .native_health import LIMITS
import math


def accept_http_retry(parent, value, now, valid):
    """Called under the existing report lock; fixed-size evidence, no native work."""
    old=getattr(parent, '_http_retry_evidence', None)
    parent._http_retry_valid_at=None
    if not valid or value.get('version')!=3 or any(value['overflow']) or value['progress'][1]=='finished':return
    marker=value.get('http_retry')
    if not isinstance(marker,(list,tuple)) or len(marker)!=3:return
    a,b,d=marker
    if (type(a) is not int or not 1<=a<=value['attempt']
        or any(type(x) not in (int,float) or not math.isfinite(x) for x in (b,d))
        or not 0<=b<=now or not any(abs(d-b-delay)<=.001 for delay in (1,2,5,10,30))):return
    marker=tuple(marker)
    if old:
        owner,previous,expiry,consumed=old
        if owner!=parent.generation or a<previous[0]:return
        if a==previous[0]:
            if marker!=previous:return
        else:
            if b<=previous[1]:return
            expiry=d+getattr(parent,'_http_retry_window',90.)
            consumed=False
    else:
        expiry=d+getattr(parent,'_http_retry_window',90.);consumed=False
    produced=(getattr(parent,'_http_pcm_produced_at',0) if getattr(parent,'direct_runtime',None) is None
              else getattr(parent,'_last_pcm',0))
    consumed=consumed or now>=expiry or (getattr(parent,'_has_pcm',False) and produced>b)
    parent._http_retry_evidence=(parent.generation,marker,expiry,consumed)
    parent._http_retry_valid_at=now


def healthy_http_retry_exemption(parent, now, freshness=5., *, clock=None):
    if getattr(getattr(parent, 'source', None), 'type', None) != 'HTTP':
        return False
    if not getattr(parent,'_http_retry_evidence',None):return False
    with parent._lock:
        if clock is not None:now=clock()  # Read after report-lock acquisition, not before a concurrent receipt.
        evidence=getattr(parent,'_http_retry_evidence',None)
        receipt=getattr(parent,'_http_retry_valid_at',None)
        reason='invalid_health'
        allowed=False
        if evidence and receipt is not None:
            owner,marker,expiry,consumed=evidence
            produced=(getattr(parent,'_http_pcm_produced_at',0) if getattr(parent,'direct_runtime',None) is None
                      else getattr(parent,'_last_pcm',0))
            consumed=consumed or now>=expiry or (getattr(parent,'_has_pcm',False) and produced>marker[1])
            parent._http_retry_evidence=(owner,marker,expiry,consumed)
            allowed=(owner==parent.generation and parent.source.type=='HTTP' and parent.reconnect
                     and not parent._stop.is_set() and not getattr(parent,'_fatal_error',None)
                     and not getattr(parent,'_pcm_error',None) and not parent._done.is_set()
                     and 0<=now-receipt<=freshness and not consumed)
            reason='completed_retry' if allowed else 'expired_consumed_or_stale'
        # Log transitions, with a fixed-size limiter; never every supervisor tick.
        key=(allowed,reason)
        if key!=getattr(parent,'_http_retry_log_key',None) and now>=getattr(parent,'_http_retry_log_due',0):
            log.info('source=%s generation=%s category=http_retry_exemption allowed=%s reason=%s expiry=%s',
                     parent.source_id,parent.generation,allowed,reason,evidence[2] if evidence else None)
            parent._http_retry_log_key=key;parent._http_retry_log_due=now+5
        return allowed


def request_progress(parent, attempt):
    """Queue one idempotent progress probe; no native work or source wakeup."""
    if type(attempt) is not int or not 0<=attempt<2**63:
        return None
    with parent._lock:
        if parent._stop.is_set() or parent._fatal_error or getattr(parent, '_source_probe_cancelled', False):
            return None
        previous = getattr(parent, '_source_probe', None)
        report = getattr(parent, '_source_probe_report', None)
        if previous and (not report or report[0] != previous['id'] or report[5] == 'pending'):
            return previous['id']
        seq = getattr(parent, '_source_probe_sequence', 0)+1
        if seq >= 2**63:
            return None
        parent._source_probe_sequence = seq
        parent._source_probe = dict(id=seq, attempt=attempt, owner=parent.generation, mode='progress')
        parent._source_probe_queued = time.monotonic()
        parent._source_probe_sent = None
        parent._source_probe_report = None
        return seq


def evaluate(parent, now=None):
    try:
        return _evaluate(parent, now)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        # Qualification data must never break the existing recovery service cycle.
        return None


def _evaluate(parent, now=None):
    """Hypothetical decisions only. Missing PCM by itself NEVER means replace."""
    now = time.monotonic() if now is None else now
    if now < getattr(parent, '_liveness_next', 0):
        return getattr(parent, 'shadow_decision', None)
    parent._liveness_next = now+1
    status_at, pcm_at, state, fatal, done = parent.recovery_observation()
    health = getattr(parent, 'native_health', None)
    kind = getattr(getattr(parent, 'source', None), 'type', 'UNKNOWN')
    decision = 'observe_unqualified'; reason = 'no validated native health'
    probe_needed = False
    evidence = dict(state=state, status_age_s=max(0, now-status_at), pcm_age_s=max(0, now-pcm_at))
    progress = getattr(parent, 'direct_progress', {})
    active = any(lease is not None for _,lease in getattr(parent, '_direct_intents', {}).values())
    writer_age = now-max(progress.get('writer_at', now), getattr(parent, '_direct_writer_required_since', 0))
    if parent._stop.is_set() and not fatal:
        decision, reason = 'stopped_or_retiring', 'manual lifecycle intent; no restart proposal'
    elif done:
        decision, reason = 'replace_candidate_worker_exit', str(fatal or 'worker exited')
    elif active and writer_age > 5:
        decision, reason = 'replace_candidate_writer', 'writer loop progress missing'
        evidence['writer_age_s'] = writer_age
    elif now-status_at > 5:
        decision, reason = 'replace_candidate_ipc', 'independent IPC heartbeat missing'
    elif fatal:
        decision, reason = 'replace_candidate_worker_fault', 'worker reported a fatal fault'
    elif health:
        at, phase, due = health['progress']
        overdue = [(lane, op) for lane,stack in enumerate(health['slots']) for op in stack
                   if now-op[3] > LIMITS[op[2]]]
        if overdue:
            lane, op = max(overdue, key=lambda item:now-item[1][3]-LIMITS[item[1][2]])
            age = now-op[3]
            evidence.update(operation_lane=lane, operation=op[0], attempt=op[1], operation_kind=op[2], start=op[3], elapsed_s=age)
            probe_needed = True
            decision = 'await_operation_progress' if age <= LIMITS[op[2]]+5 else 'replace_candidate_native_unqualified'
            reason = 'operation overdue; deadline is NOT qualified for enforcement'
        elif phase == 'backoff' and now <= due+5:
            decision, reason = 'continue_retry', 'source-owned retry backoff; do not interrupt'
            evidence['retry_due'] = due
        elif now-at > 5 and phase != 'finished':
            probe_needed = True
            episode = (health['attempt'], at)
            waiting = getattr(parent, '_liveness_response_wait', None)
            if waiting is None or waiting[:2] != episode:
                waiting = (*episode, now)
                parent._liveness_response_wait = waiting
            requested = getattr(parent, '_source_probe', None)
            report = getattr(parent, '_source_probe_report', None)
            current = (requested and requested['attempt'] == health['attempt']
                       and (not report or (report[0] == requested['id']
                            and report[1] == requested['attempt'] and report[5] == 'pending')))
            sent = getattr(parent, '_source_probe_sent', None) if current else None
            # A stale attempt's ACK or a probe received during legitimate backoff
            # cannot consume this episode's source-thread response allowance.
            if sent is not None and now-max(sent, waiting[2]) >= 5:
                decision, reason = 'replace_candidate_source_unqualified', 'source probe not completed; control ACK is insufficient'
            else:
                decision, reason = 'request_source_progress', 'source checkpoint missing; await source-thread response'
        elif active and any(value is False for value in progress.get('connections', {}).values()):
            decision, reason = 'continue_delivery_retry', 'mux disconnected; writer loop still progressing'
        elif state == 'RUNNING' and getattr(parent, '_has_pcm', False) and now-pcm_at <= 2:
            decision, reason = 'continue_playback', 'recent actual PCM and source progress'
        else:
            decision, reason = 'continue_media_wait', 'source progressing without available media'
        evidence.update(source_type=str(kind), phase=phase, checkpoint=at)
    if probe_needed and health and not parent._stop.is_set():
        # One probe per identified stall episode, not one per supervisor tick.
        episode = ((health['attempt'], evidence['operation'], evidence['start']) if 'operation' in evidence
                   else (health['attempt'], 'loop', health['progress'][0]))
        if episode != getattr(parent, '_liveness_probe_episode', None):
            if request_progress(parent, health['attempt']) is not None:
                parent._liveness_probe_episode = episode
    requested = getattr(parent, '_source_probe', None)
    reported = getattr(parent, '_source_probe_report', None)
    evidence.update(configuration_owner=parent.generation,
                    request_id=requested['id'] if requested else None,
                    request_queued=getattr(parent, '_source_probe_queued', None),
                    request_sent=getattr(parent, '_source_probe_sent', None),
                    receipt=reported[3] if reported else None,
                    completion=reported[4] if reported else None,
                    response=reported[5] if reported else None)
    value = dict(decision=decision, reason=reason, evidence=evidence, observed_at=now)
    parent.shadow_decision = value
    # One fixed-size transition key. Ages and routine checkpoints advance on
    # every tick; they do not make an unchanged condition worth logging again.
    # Healthy playback resets the episode silently, even at DEBUG level.
    key = None if decision == 'continue_playback' else (
        decision, reason, state, evidence.get('phase'),
        evidence.get('operation_lane'), evidence.get('operation'),
        evidence.get('attempt'), evidence.get('operation_kind'), evidence.get('start'),
        evidence['request_id'], evidence['response'])
    if key is not None and key != getattr(parent, '_liveness_log_key', None):
        # Unqualified native deadlines and source probes remain observations.
        # Only independently observed faults warrant WARNING, never an action claim.
        emit = log.warning if decision in (
            'replace_candidate_worker_exit', 'replace_candidate_worker_fault',
            'replace_candidate_writer', 'replace_candidate_ipc') else log.info
        emit('source=%s generation=%s category=shadow_recovery_decision decision=%s reason=%s state=%s request_id=%s response=%s operation=%s operation_kind=%s attempt=%s elapsed_s=%s observation_only=true',
             parent.source_id, parent.generation, decision, reason, state, evidence['request_id'],
             evidence['response'], evidence.get('operation'), evidence.get('operation_kind'),
             evidence.get('attempt'), evidence.get('elapsed_s'))
    parent._liveness_log_key = key
    return value
