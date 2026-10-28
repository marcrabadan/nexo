"""Process-wide Shield admission budgets; no task/interpreter/test resets."""
import threading

from .errors import NexoError

LIMITS = {"tasks": 4096, "registrations": 1024, "channels": 256,
          "channel_slots": 65536, "keys": 4096, "key_bytes": 1048576, "web_servers": 16, "web_requests": 4096}


class Budget:
    def __init__(self):
        self.lock = threading.Lock()
        self.used = dict.fromkeys(LIMITS, 0)
        self.threads = 0

    def charge(self, requests, span=None):
        with self.lock:
            next_used = self.used.copy()
            for resource, amount in requests:
                next_used[resource] += amount
                if next_used[resource] > LIMITS[resource]:
                    raise NexoError("security_job_budget", span)
            self.used = next_used

    def acquire_thread(self, span=None):
        with self.lock:
            if self.threads >= 32:
                raise NexoError("security_job_budget", span)
            self.threads += 1
        return ThreadLease(self)


class ThreadLease:
    def __init__(self, budget):
        self.budget = budget

    def release(self):
        if self.budget is not None:
            with self.budget.lock:
                self.budget.threads -= 1
            self.budget = None


PROCESS = Budget()


def charge(requests, span=None):
    from .stdlib.policy import enforcing
    if enforcing():
        PROCESS.charge(requests, span)


def spawn_thread(span=None):
    from .stdlib.policy import enforcing
    return PROCESS.acquire_thread(span) if enforcing() else ThreadLease(None)
