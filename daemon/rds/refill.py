"""Bounded FIFO refill on the existing serialized control worker, never appsink."""
from dataclasses import dataclass, field
import time
from ..diagnostics import error_scope
from ..errors import DependencyUnavailable
from ..rp2354.protocol import Busy, Command, Outcome, RpResponseError, execution_outcome
from .scheduler import Scheduler, LOOKAHEAD_SECONDS

POLL_SECONDS = 0.1
RP_ERROR_RETRY_SECONDS = 1.0  # Valid device error replies, never transport failures.
MAX_BATCHES_PER_TICK = 2
CONTENT_FIELDS = {'rds_pi', 'rds_ecc', 'rds_pty', 'rds_tp', 'rds_ps', 'rds_af_khz', 'stereo',
                  'rds_rt_enabled', 'rds_rt_termination'}


@dataclass
class Channel:
    scheduler: Scheduler = field(default_factory=Scheduler)
    active: bool = False
    needs_clear: bool = False
    pending: tuple | None = None
    signature: dict | None = None
    retry_at: float = 0


class RdsRefill:
    def __init__(self, database, modulators, radiotext, rp, clock=time.monotonic, *, horizon=LOOKAHEAD_SECONDS):
        self.database, self.modulators, self.radiotext, self.rp = database, modulators, radiotext, rp
        self.clock, self.horizon = clock, horizon
        self.channels = [Channel() for _ in range(8)]
        self.next_poll = 0.0
        self.suspended = False
        self.on_failure = None
        self.can_communicate = lambda: True
        self.channel_ready = lambda channel: True
        self.transport_retry_at = 0.0

    def suspend(self):
        self.suspended = True
        for channel in range(8):
            self.reset_channel(channel)
            row = self.modulators.statuses[channel]
            row.rds_state = "UNKNOWN"

    def reset_channel(self, channel):
        self.channels[channel] = Channel()
        row = self.modulators.statuses[channel]
        row.rds_state, row.rds_last_error = "DISABLED", None

    def profile_changed(self, channel):
        """Request the existing target-local content boundary, without RF commands."""
        state = self.channels[channel]
        state.signature = None
        if state.active:
            self.fence(channel)
        self.next_poll = 0

    def fence(self, channel):
        state = self.channels[channel]
        state.pending = None
        state.needs_clear = True
        state.retry_at = 0
        self.next_poll = 0

    def _transport_ready(self):
        return (self.clock() >= self.transport_retry_at and self.can_communicate()
                and getattr(self.rp, 'retry_delay', 0.0) == 0)

    def _transport_failure(self, error):
        if not isinstance(error, RpResponseError):
            self.transport_retry_at = self.clock() + max(.25, getattr(self.rp, 'retry_delay', 0.0))
            if self.on_failure:
                self.on_failure(error)

    def _error(self, channel, error):
        self.modulators.statuses[channel].rds_last_error = str(error)
        self.modulators.statuses[channel].rds_last_error_scope = error_scope(error)
        self.modulators.statuses[channel].rds_state = 'ERROR'
        if isinstance(error, RpResponseError):
            self.channels[channel].retry_at = self.clock() + RP_ERROR_RETRY_SECONDS
        else:
            self._transport_failure(error)

    def stop(self, channel):
        state = self.channels[channel]
        if state.active or state.needs_clear:
            state.active = False
            state.pending = None
            state.needs_clear = True
            self._clear(channel)

    def _clear(self, channel):
        state = self.channels[channel]
        try:
            self.rp.clear_rds(channel)
        except DependencyUnavailable as exc:
            self._error(channel, exc)
            return False
        state.needs_clear = False
        state.pending = None
        state.retry_at = 0
        row = self.modulators.statuses[channel]
        row.rds_last_error = None
        row.rds_state = 'READY' if state.active else 'DISABLED'
        return True

    def tick(self):
        if self.suspended or not self._transport_ready():
            return
        now = self.clock()
        if now < self.next_poll and not any(c.needs_clear for c in self.channels):
            return
        self.next_poll = now + POLL_SECONDS
        ready = []
        for config in self.database.list('modulators'):
            if self.suspended or not self._transport_ready():
                return
            channel = config.id
            if not self.channel_ready(channel):
                continue
            state = self.channels[channel]
            active = config.rds_enabled and (channel in self.modulators.active_sources or
                (config.audio_mode == "ANALOG" and self.modulators.statuses[channel].tx_state == "ON"))
            if not active:
                if state.active:
                    state.active = False
                    state.pending = None
                    state.needs_clear = True
                if state.needs_clear and now >= state.retry_at:
                    self._clear(channel)
                continue
            text = self.radiotext.effective(channel).text
            signature = config.model_dump(include=CONTENT_FIELDS)
            if not state.active or signature != state.signature:
                state.scheduler.reset(config, text, now)
                state.signature = signature
                state.active, state.needs_clear, state.pending = True, True, None
            elif config.rds_rt_enabled and state.scheduler.update_rt(text, now):
                state.pending = None
                state.needs_clear = True
            if now < state.retry_at:
                continue
            if state.needs_clear:
                if not self._clear(channel):
                    if self.suspended:
                        return
                    continue
                # Clear drops queued fragments, not independent content timers.
                # RT updates must not restart the PS carousel or quota credits.
                scheduler = state.scheduler
                scheduler.cursor = now
                scheduler.ps_segment = scheduler.rt_segment = scheduler.af_pair = 0
            ready.append(channel)
        if not ready:
            return
        try:
            free = self.rp.get_rds_fifo_free()
            if len(free) != 8 or any(type(n) is not int or not 0 <= n <= 65535 for n in free):
                raise DependencyUnavailable('Invalid RDS FIFO capacity response')
        except DependencyUnavailable as exc:
            self._transport_failure(exc)
            for channel in ready:
                # Capacity is unknown, but scheduler/pending batches are intact.
                row = self.modulators.statuses[channel]
                row.rds_last_error, row.rds_state = str(exc), 'UNKNOWN'
            return
        for channel in ready:
            if self.suspended or not self._transport_ready():
                return
            state = self.channels[channel]
            row = self.modulators.statuses[channel]
            row.rds_fifo_free = free[channel]
            for _ in range(MAX_BATCHES_PER_TICK):
                if state.pending is None:
                    groups, candidate = state.scheduler.prepare(free[channel], now, self.horizon)
                    if not groups:
                        break
                    state.pending = (groups, candidate)
                groups, candidate = state.pending
                if len(groups) > free[channel]:
                    break
                try:
                    self.rp.push_rds(channel, groups)
                except Busy as exc:
                    # Atomic rejection: keep identical bytes and candidate state.
                    row.rds_last_error, row.rds_state = str(exc), 'BUSY'
                    break
                except DependencyUnavailable as exc:
                    if execution_outcome(exc, Command.RDS_PUSH) != Outcome.NOT_SENT:
                        # Ambiguous/failed submission: no replay across the boundary.
                        self.fence(channel)
                    self._error(channel, exc)
                    break
                state.scheduler = candidate
                state.pending = None
                free[channel] -= len(groups)
                row.rds_fifo_free = free[channel]
                row.rds_groups_accepted += len(groups)
                row.rds_last_error, row.rds_state = None, 'FEEDING'
                row.rds_text_ab = state.scheduler.text_ab
            if free[channel] == 0 and row.rds_state not in ('ERROR', 'BUSY'):
                row.rds_state = 'FIFO_FULL'

    def close(self):
        for channel in range(8):
            self.stop(channel)
