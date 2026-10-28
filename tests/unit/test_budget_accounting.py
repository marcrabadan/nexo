import concurrent.futures

import pytest

from nexo.errors import NexoError
from nexo.security_budget import Budget


def test_multi_resource_charge_is_atomic_and_cumulative():
    budget = Budget()
    budget.charge([("channels", 255), ("channel_slots", 65535)])
    with pytest.raises(NexoError):
        budget.charge([("channels", 1), ("channel_slots", 2)])
    assert budget.used["channels"] == 255
    budget.charge([("channels", 1), ("channel_slots", 1)])
    with pytest.raises(NexoError):
        budget.charge([("tasks", 10**100)])
    assert budget.used["tasks"] == 0


def test_parallel_admission_has_one_shared_atomic_counter():
    budget = Budget()
    def attempt(_):
        try:
            budget.charge([("registrations", 1)])
            return 1
        except NexoError:
            return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt, range(2048))) == 1024
    assert budget.used["registrations"] == 1024


def test_thread_permits_release_on_completion_and_reject_the_33rd():
    budget = Budget()
    for _ in range(2):
        permits = [budget.acquire_thread() for _ in range(32)]
        with pytest.raises(NexoError):
            budget.acquire_thread()
        for permit in permits:
            permit.release()
            permit.release()  # failure cleanup must not double-decrement
        assert budget.threads == 0


def test_runtime_registry_resets_do_not_reset_process_budget(monkeypatch):
    from nexo import security_budget
    from nexo.interpreter import Interpreter
    from nexo.stdlib import stream, sync
    budget = Budget()
    monkeypatch.setattr(security_budget, "PROCESS", budget)
    budget.charge([("registrations", 1024)])
    Interpreter()
    stream.reset()
    sync.reset()
    with pytest.raises(NexoError):
        budget.charge([("registrations", 1)])
