"""Startup UI plans, independent of hardware tuning and RDS AF rules."""
from dataclasses import dataclass, asdict

@dataclass(frozen=True)
class FrequencyPlan:
    region: int
    region_name: str
    fm_min_khz: int | None
    fm_max_khz: int | None
    fm_step_khz: int | None

    def as_dict(self):
        return asdict(self)

    def frequencies(self):
        if self.fm_min_khz is None:
            return range(0)
        return range(self.fm_min_khz, self.fm_max_khz + 1, self.fm_step_khz)

PLANS = {
    0: FrequencyPlan(0, "Expert / full hardware range", None, None, None),
    1: FrequencyPlan(1, "Europe / standard", 87500, 108000, 100),
    2: FrequencyPlan(2, "United States", 88100, 107900, 200),
    3: FrequencyPlan(3, "Japan", 76100, 98900, 100),
}


def effective_plan(region, info):
    plan = PLANS[region].as_dict()
    if region == 0 and info is not None:
        plan.update(fm_min_khz=info['frequency_min_khz'],
                    fm_max_khz=info['frequency_max_khz'], fm_step_khz=info['frequency_step_khz'])
    return plan
