"""Invocation token admission. Counts include input and output; unknown holds persist."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from .types import failure, NexoFailure
from .errors import NexoError
_CURRENT = ContextVar('nexo_agent_tokens', default=())

class Meter:

    def __init__(self, limit):
        self.limit = self.remaining = limit
        self.pending = False
        self.lock = Lock()
        self.backend = None

    def load(self):
        if self.backend is None:
            return
        store, name, identity = self.backend
        value = store.read(name)
        if not isinstance(value, dict) or set(value) != {'version', 'identity', 'limit', 'remaining', 'pending'} or type(value['version']) is not int or (value['version'] != 1) or (value['identity'] != identity) or (type(value['limit']) is not int) or (value['limit'] != self.limit) or (type(value['remaining']) is not int) or (not 0 <= value['remaining'] <= self.limit) or (type(value['pending']) is not bool):
            raise ValueError('invalid token ledger')
        self.remaining = min(self.remaining, value['remaining'])
        self.pending |= value['pending']

    def save(self):
        if self.backend is not None:
            store, name, identity = self.backend
            store.write(name, {'version': 1, 'identity': identity, 'limit': self.limit, 'remaining': self.remaining, 'pending': self.pending})

@contextmanager
def transaction(meter):
    with meter.lock:
        if meter.backend is None:
            yield
        else:
            store, name, _ = meter.backend
            with store.lock(name):
                meter.load()
                yield

def guarded(operation):

    def run(*args):
        try:
            return operation(*args)
        except Exception as exc:
            if isinstance(exc, (NexoError, NexoFailure)):
                raise
            from cryptography.exceptions import InvalidTag
            if isinstance(exc, (OSError, ValueError, TypeError, InvalidTag)):
                call = args[0] if hasattr(args[0], 'span') else args[1]
                raise NexoError('agent_state', call.span)
            raise
    return run

def attach(call, store, run, identity, allow_new):
    from .agent_state import digest
    try:
        for index, meter in enumerate(capture()):
            with meter.lock:
                if meter.backend is not None:
                    old_store, old_name, _ = meter.backend
                    with old_store.lock(old_name):
                        meter.load()
                        if meter.pending:
                            raise ValueError('uncertain token ledger')
                        meter.save()
                    continue
                if meter.pending:
                    raise ValueError('active token request')
                name = digest(run + '\ntokens\n' + str(index))
                with store.lock(name):
                    existed = store.read(name) is not None
                    if not existed and (not allow_new):
                        raise ValueError('missing token ledger')
                    meter.backend = (store, name, identity)
                    if existed:
                        meter.load()
                    if meter.pending:
                        raise ValueError('uncertain token ledger')
                    meter.save()
    except Exception as exc:
        from cryptography.exceptions import InvalidTag
        if isinstance(exc, (OSError, ValueError, TypeError, InvalidTag)):
            raise NexoError('agent_state', call.span)
        raise

def capture():
    return _CURRENT.get()

@contextmanager
def scope(meters, replace=False):
    combined = [] if replace else list(capture())
    for meter in meters:
        if not any((old is meter for old in combined)):
            combined.append(meter)
    token = _CURRENT.set(tuple(combined))
    try:
        yield
    finally:
        _CURRENT.reset(token)

def states():
    result = []
    for meter in capture():
        with meter.lock:
            result.append([meter.limit, meter.remaining, meter.pending])
    return result

def restore(saved, span):
    meters = capture()
    if not isinstance(saved, list) or len(saved) != len(meters):
        raise NexoError('agent_state', span)
    for value, meter in zip(saved, meters):
        if not isinstance(value, list) or len(value) != 3 or type(value[0]) is not int or (type(value[1]) is not int) or (value[0] != meter.limit) or (not 0 <= value[1] <= value[0]) or (type(value[2]) is not bool):
            raise NexoError('agent_state', span)
    for value, meter in zip(saved, meters):
        with meter.lock:
            meter.remaining = min(meter.remaining, value[1])

class Lease:

    def __init__(self, meters, cap):
        self.meters, self.cap, self.finished = (meters, cap, False)

    @guarded
    def abort(self, call):
        if not self.finished:
            for meter in self.meters:
                with transaction(meter):
                    meter.pending = False
                    meter.save()
            self.finished = True

    @guarded
    def finish(self, call, usage):
        if self.finished:
            return
        if usage is None:
            raise failure('AIError', call.span, key='AIError.budget_usage')
        total = sum(usage)
        exceeded = False
        for meter in self.meters:
            with transaction(meter):
                exceeded |= total > meter.remaining
                meter.remaining = max(0, meter.remaining - total)
                meter.pending = False
                meter.save()
        self.finished = True
        if exceeded:
            raise failure('AIError', call.span, key='AIError.tokens')

@guarded
def reserve(call):
    meters = capture()
    if not meters:
        return None
    acquired = []
    cap = 9223372036854775807
    try:
        for meter in meters:
            with transaction(meter):
                if meter.pending or meter.remaining <= 0:
                    raise failure('AIError', call.span, key='AIError.tokens')
                meter.pending = True
                cap = min(cap, meter.remaining)
                meter.save()
                acquired.append(meter)
    except BaseException:
        Lease(acquired, cap).abort(call)
        raise
    return Lease(meters, cap)
