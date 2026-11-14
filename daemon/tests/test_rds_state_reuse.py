"""Ownership at the speculative scheduler ACK boundary."""
from copy import deepcopy

from fmheadendd.rds.scheduler import Scheduler
from fmheadendd.tests.test_rds import config


def test_candidate_owns_all_mutable_speculative_state():
    scheduler = Scheduler()
    original = config()
    scheduler.reset(original, 'Artist - Track', 0)
    original.rds_ps[0].text = 'External'
    before = deepcopy(scheduler)
    groups, candidate = scheduler.prepare(31, 0)
    assert groups
    assert vars(scheduler.allocator) == vars(before.allocator)
    assert {k: v for k, v in vars(scheduler).items() if k != 'allocator'} == {
        k: v for k, v in vars(before).items() if k != 'allocator'}
    assert candidate.config is scheduler.config
    assert candidate.rt is scheduler.rt
    assert candidate.allocator is not scheduler.allocator
    assert candidate.allocator.weights is not scheduler.allocator.weights
    assert candidate.allocator.credits is not scheduler.allocator.credits
    assert scheduler.config.rds_ps[0].text == 'RADIO FM'
    assert scheduler.prepare(31, 0)[0] == groups  # Identical retry bytes.
    scheduler.update_rt('New title', 1)
    assert candidate.rt != scheduler.rt
    scheduler.reset(config(), 'Reset', 2)
    assert candidate.config is not scheduler.config
