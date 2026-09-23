"""Exact quotas and smooth weighted allocation, independent of RDS encoding."""
from collections.abc import Mapping
from fractions import Fraction
from math import lcm


def quotas(*, rt_enabled: bool, ecc_enabled: bool,
           additional: Mapping[str, float | Fraction] | None = None) -> dict[str, Fraction]:
    """Additional entries are enabled types; omitted/zero quotas emit nothing.

    Only surplus is available to extensions. Never normalize infeasible minima.
    Decimal text conversion avoids binary-float rounding at the feasibility edge.
    """
    result = {'ps': Fraction(3, 5), 'rt': Fraction(3, 10) if rt_enabled else Fraction(0),
              'ecc': Fraction(1, 50) if ecc_enabled else Fraction(0)}
    for name, value in (additional or {}).items():
        if not isinstance(name, str) or not name or name in result:
            raise ValueError(f'Invalid or reserved RDS group name: {name!r}')
        if isinstance(value, bool):
            raise ValueError(f'Invalid quota for {name}: {value!r}')
        share = Fraction(str(value))
        if not 0 <= share <= 1:
            raise ValueError(f'Quota for {name} must be between zero and one')
        result[name] = share
    surplus = 1 - sum(result.values())
    if surplus < 0:
        raise ValueError('Infeasible RDS allocation: PS/RT minima, ECC and additional quotas exceed 1')
    result['rt' if rt_enabled else 'ps'] += surplus
    return result


class GroupAllocator:
    """Smooth weighted round robin with integer credits and stable tie order.

    Each slot adds each type's weight, emits the highest-credit type, then
    subtracts the total weight from its credit. State continues across batches;
    no fixed carousel or floating-point drift. Cost is O(enabled group types).
    """
    def __init__(self, shares: Mapping[str, Fraction]):
        if not shares or any(v < 0 for v in shares.values()) or sum(shares.values()) != 1:
            raise ValueError('Group shares must be nonnegative and sum to one')
        self.total = lcm(*(v.denominator for v in shares.values()))
        self.weights = {name: int(share * self.total) for name, share in shares.items() if share}
        self.credits = dict.fromkeys(self.weights, 0)

    def next(self) -> str:
        for name, weight in self.weights.items():
            self.credits[name] += weight
        selected = max(self.credits, key=self.credits.get)
        self.credits[selected] -= self.total
        return selected
