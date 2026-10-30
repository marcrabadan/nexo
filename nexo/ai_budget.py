"""Process-local AI request reservations; uncertain charges are never refunded."""
from decimal import Decimal
from itertools import count
from threading import RLock

LOCK = RLock()
_HOLDS = {}
_IDS = count(1)


class Unavailable(Exception):
    def __init__(self, spent, reserved):
        self.spent, self.reserved = spent, reserved


def _reserved(scope, currency, day, period):
    return sum((amount for path, coin, booked, amount in _HOLDS.values()
                if path == scope and coin == currency
                and (booked == day if period == 'day' else booked[:7] == day[:7])), Decimal(0))


def inspect(scope, currency, day, period, history):
    with LOCK:
        return history(), _reserved(scope, currency, day, period)


def reserve(scope, currency, day, period, limit, history):
    with LOCK:
        spent = history()
        reserved = _reserved(scope, currency, day, period)
        available = limit - spent - reserved
        if available <= 0:
            raise Unavailable(spent, reserved)
        identity = next(_IDS)
        # Holding all remaining headroom avoids guessing a tokenizer/invoice.
        _HOLDS[identity] = (scope, currency, day, available)
        return Ticket(identity, day)


class Ticket:
    def __init__(self, identity, day):
        self.identity, self.day = identity, day
        self.finished = False

    def settle(self, actual, write):
        with LOCK:
            if self.finished:
                return
            if write() or actual == 0:
                _HOLDS.pop(self.identity)
            else:
                path, coin, day, _ = _HOLDS[self.identity]
                _HOLDS[self.identity] = (path, coin, day, actual)
            self.finished = True

    # No destructor refunds a dispatched, failed or abandoned request.
