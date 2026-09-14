"""Startup UI plans, independent of hardware tuning and RDS AF rules."""
from dataclasses import dataclass, asdict

HARDWARE_MIN_KHZ = 76000
HARDWARE_MAX_KHZ = 108000
HARDWARE_STEP_KHZ = 50

@dataclass(frozen=True)
class FrequencyPlan:
    region: int
    region_name: str
    fm_min_khz: int
    fm_max_khz: int
    fm_step_khz: int

    def as_dict(self):
        return asdict(self)

    def frequencies(self):
        return range(self.fm_min_khz, self.fm_max_khz + 1, self.fm_step_khz)

PLANS = {
    0: FrequencyPlan(0, "Expert / full hardware range", 76000, 108000, 50),
    1: FrequencyPlan(1, "Europe / standard", 87500, 108000, 100),
    2: FrequencyPlan(2, "United States", 88100, 107900, 200),
    3: FrequencyPlan(3, "Japan", 76100, 98900, 100),
}
