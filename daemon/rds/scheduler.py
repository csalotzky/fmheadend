"""Per-channel deadline policy; speculative batches commit only after RP ACK."""
from copy import deepcopy
from dataclasses import dataclass, field
from .characters import ps_bytes, rt_bytes
from .encoder import group_0a, group_1a, group_2a, Group

PS_INTERVAL = 1 / 4.5
RT_INTERVAL = 1 / 6.7
RT_BOOST_INTERVAL = 0.125
ECC_INTERVAL = 5.0
GROUP_SECONDS = 104 / 1187.5
# M8.6B: margin for the unchanged 850 ms GET_STATUS wait on the shared
# control executor, plus refill phase/jitter. This is Linux buffering policy.
LOOKAHEAD_SECONDS = 2.0
MAX_PENDING = 31


@dataclass
class Scheduler:
    config: object = None
    rt: bytes | None = None
    text_ab: int = 0
    ps_item: int = 0
    ps_segment: int = 0
    af_pair: int = 0
    rt_segment: int = 0
    ps_since: float = 0
    cursor: float = 0
    deadlines: dict = field(default_factory=dict)
    boost_remaining: int = 0

    def reset(self, config, text: str, now: float):
        encoded = rt_bytes(text)
        if self.rt is not None and self.rt != encoded:
            self.text_ab ^= 1
        self.rt = encoded
        self.config = config.model_copy(deep=True)
        self.ps_item = self.ps_segment = self.rt_segment = self.af_pair = 0
        self.ps_since = self.cursor = now
        self.deadlines = {'ps': now, 'rt': now, 'ecc': now}
        self.boost_remaining = len(encoded) // 4

    def update_rt(self, text: str, now: float) -> bool:
        encoded = rt_bytes(text)
        if self.rt == encoded:
            return False
        self.text_ab ^= 1
        self.rt, self.rt_segment = encoded, 0
        self.boost_remaining = len(encoded) // 4
        self.deadlines['rt'] = now
        return True

    def _next(self, when: float) -> Group:
        c = self.config
        # Hard ECC deadline; PS/RT relative deadlines share remaining slots.
        if c.rds_ecc is not None and when >= self.deadlines['ecc']:
            self.deadlines['ecc'] = max(when, self.deadlines['ecc'] + ECC_INTERVAL)
            return group_1a(c.rds_pi, c.rds_pty, c.rds_tp, c.rds_ecc)
        # Virtual finish deadlines preserve relative weights without rounding each
        # interval to a physical group slot. Rebase both together after delays.
        earliest = min(self.deadlines['ps'], self.deadlines['rt'])
        for key in ('ps', 'rt'):
            self.deadlines[key] += when - earliest
        kind = min(('ps', 'rt'), key=lambda key: self.deadlines[key])
        if kind == 'ps':
            group = group_0a(c.rds_pi, c.rds_pty, c.rds_tp,
                            ps_bytes(c.rds_ps[self.ps_item].text), self.ps_segment, c.stereo, c.rds_af_khz, self.af_pair)
            self.af_pair = (self.af_pair + 1) % ((len(c.rds_af_khz) + 2) // 2 or 1)
            self.ps_segment = (self.ps_segment + 1) % 4
            self.deadlines['ps'] = self.deadlines['ps'] + PS_INTERVAL
        else:
            group = group_2a(c.rds_pi, c.rds_pty, c.rds_tp, self.rt, self.rt_segment, self.text_ab)
            self.rt_segment = (self.rt_segment + 1) % (len(self.rt) // 4)
            interval = RT_BOOST_INTERVAL if self.boost_remaining else RT_INTERVAL
            self.boost_remaining = max(0, self.boost_remaining - 1)
            self.deadlines['rt'] = self.deadlines['rt'] + interval
        return group

    def prepare(self, limit: int, now: float, horizon: float = LOOKAHEAD_SECONDS):
        candidate = deepcopy(self)
        candidate.cursor = max(now, candidate.cursor)
        c = candidate.config
        if len(c.rds_ps) > 1 and now >= candidate.ps_since + c.rds_ps[candidate.ps_item].duration_s:
            candidate.ps_item = (candidate.ps_item + 1) % len(c.rds_ps)
            candidate.ps_since, candidate.ps_segment = now, 0
        groups = []
        while len(groups) < min(limit, MAX_PENDING) and candidate.cursor < now + horizon:
            groups.append(candidate._next(candidate.cursor))
            candidate.cursor += GROUP_SECONDS
        return tuple(groups), candidate
