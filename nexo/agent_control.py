"""Inherited, authority-reducing cancellation/deadline admission scopes."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from .types import Native, Builtin, failure
_CURRENT = ContextVar('nexo_agent_controls', default=())

class Control(Native):
    type_name = 'agent_control'

    def __init__(self):
        self.closed = Event()

    def repr(self):
        return 'agent_control'

    def member(self, call, name):
        if name == 'cancel':
            return Builtin('cancel', self.cancel)
        if name == 'cancelled':
            return self.closed.is_set()
        return self.missing(call, name, ['cancel', 'cancelled'])

    def cancel(self, call):
        self.closed.set()

    def check(self, call):
        if self.closed.is_set():
            raise failure('Cancelled', call.span)

def capture():
    return _CURRENT.get()

@contextmanager
def scope(controls, replace=False):
    token = _CURRENT.set(tuple(controls) if replace else _CURRENT.get() + tuple(controls))
    try:
        yield
    finally:
        _CURRENT.reset(token)

def check(call):
    for control in _CURRENT.get():
        control.check(call)
