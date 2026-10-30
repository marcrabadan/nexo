"""Cross-process financial reservations in the operator-granted encrypted store."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from decimal import Decimal
import secrets
import json
from .agent_state import digest
from .errors import NexoError
from .types import Money, failure

def policy_id(budget):
    return digest(json.dumps([budget['currency'],budget['limit'],budget['period'],sorted(budget['prices'].items())],ensure_ascii=False,separators=(',',':')))

def guarded(operation):
    def run(self, *args):
        try:
            return operation(self, *args)
        except Exception as exc:
            from cryptography.exceptions import InvalidTag
            if isinstance(exc, (OSError, ValueError, TypeError, InvalidTag)):
                plan = self.plan if hasattr(self, 'plan') else self
                raise NexoError('agent_state', plan.call.span)
            raise
    return run


class Plan:

    def __init__(self, store, call):
        self.store, self.call = (store, call)
        self.budget = store.grant.budget
        self.identity = policy_id(self.budget)

    def load(self):
        value = self.store.read('budget')
        if value is None:
            return {'version': 1, 'policy': self.identity, 'entries': []}
        if not isinstance(value, dict) or set(value) != {'version', 'policy', 'entries'}:
            raise ValueError('invalid budget state')
        if (type(value['version']) is not int or value['version'] != 1
                or value['policy'] != self.identity or not isinstance(value['entries'], list)):
            raise ValueError('invalid budget state')
        ids = set()
        for e in value['entries']:
            if not isinstance(e, dict) or set(e) != {'id', 'day', 'amount', 'pending'}:
                raise ValueError('invalid budget entry')
            if (not isinstance(e['id'], str) or len(e['id']) != 32
                    or any(c not in '0123456789abcdef' for c in e['id']) or e['id'] in ids
                    or type(e['pending']) is not bool or not isinstance(e['day'], str)
                    or date.fromisoformat(e['day']).isoformat() != e['day']):
                raise ValueError('invalid budget identity/date')
            amount = e['amount']
            if (not isinstance(amount, str) or not amount or len(amount) > 100
                    or any(c not in '0123456789.' for c in amount)
                    or amount.count('.') > 1 or not any(c.isdigit() for c in amount)):
                raise ValueError('invalid budget amount')
            ids.add(e['id'])
            n = Decimal(e['amount'])
            if not n.is_finite() or n < 0:
                raise ValueError('invalid budget amount')
        return value

    @guarded
    def reserve(self, call, target):
        from .stdlib.ai import _BudgetLease
        model = target.provider + ':' + target.name
        prices = self.budget['prices'].get(model)
        if prices is None:
            raise failure('AIError', call.span, key='AIError.budget_usage')
        prices = [Money(Decimal(p), self.budget['currency']) for p in prices]
        day = date.today().isoformat()
        with self.store.lock('budget'):
            value = self.load()
            used = sum((Decimal(e['amount']) for e in value['entries'] if (e['day'] == day if self.budget['period'] == 'day' else e['day'][:7] == day[:7])), Decimal(0))
            free = Decimal(self.budget['limit']) - used
            if free <= 0:
                raise failure('AIError', call.span, key='AIError.budget_pending', budget=self.budget['limit'] + ' ' + self.budget['currency'], period=self.budget['period'], spent=str(used))
            identity = secrets.token_hex(16)
            value['entries'].append({'id': identity, 'day': day, 'amount': format(free, 'f'), 'pending': True})
            self.store.write('budget', value)
        ticket = Ticket(self, identity, day)

        class Lease(_BudgetLease):

            def finish(self, call, tokens):
                if tokens is None:
                    raise failure('AIError', call.span, key='AIError.budget_usage')
                amount = (tokens[0] * self.price[0].amount + tokens[1] * self.price[1].amount) / 1000000
                self.ticket.settle(amount, lambda: False)
        return Lease(self, target, ticket, prices)

class Ticket:

    def __init__(self, plan, identity, day):
        self.plan, self.identity, self.day, self.finished = (plan, identity, day, False)

    @guarded
    def settle(self, amount, write):
        if self.finished:
            return
        with self.plan.store.lock('budget'):
            value = self.plan.load()
            matches = [e for e in value['entries'] if e['id'] == self.identity]
            if len(matches) != 1 or not matches[0]['pending']:
                raise ValueError('invalid reservation')
            matches[0].update(amount=format(amount, 'f'), pending=False)
            self.plan.store.write('budget', value)
        self.finished = True


_CURRENT = ContextVar('nexo_agent_finance', default=())

def capture():
    return _CURRENT.get()

@contextmanager
def scope(stores, replace=False):
    combined = [] if replace else list(_CURRENT.get())
    for store in stores:
        if all((s.grant.directory != store.grant.directory for s in combined)):
            combined.append(store)
    token = _CURRENT.set(tuple(combined))
    try:
        yield
    finally:
        _CURRENT.reset(token)

def inherited(call):
    stores = capture()
    if len(stores) > 1:
        raise NexoError('agent_state', call.span)
    return Plan(stores[0], call) if stores else None
