"""Opt-in OTLP/JSON spans. Payloads and application identifiers never enter here."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import secrets
import sys
import threading
import time
from .errors import NexoError
_CURRENT = ContextVar('agent_trace_parent', default=None)
_WRITE = threading.Lock()

@contextmanager
def span(call, name):
    option = os.environ.get('NEXO_AGENT_TRACE', '')
    if not option:
        yield
        return
    if option != 'stderr':
        raise NexoError('agent_trace', call.span)
    parent = _CURRENT.get()

    def identity_bytes(size):
        try:
            value = secrets.token_hex(size)
            return value if any((c != '0' for c in value)) else value[:-1] + '1'
        except OSError:
            raise NexoError('agent_trace', call.span)
    trace = parent[0] if parent else identity_bytes(16)
    identity = identity_bytes(8)
    token = _CURRENT.set((trace, identity))
    start = time.time_ns()
    success = False
    try:
        yield
        success = True
    finally:
        _CURRENT.reset(token)
        data = {'traceId': trace, 'spanId': identity, 'name': name, 'kind': 1, 'startTimeUnixNano': str(start), 'endTimeUnixNano': str(max(start, time.time_ns())), 'status': {'code': 1 if success else 2}}
        if parent:
            data['parentSpanId'] = parent[1]
        envelope = {'resourceSpans': [{'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'nexo'}}]}, 'scopeSpans': [{'scope': {'name': 'nexo.agent', 'version': '1'}, 'spans': [data]}]}]}
        with _WRITE:
            try:
                sys.stderr.write(json.dumps(envelope, separators=(',', ':')) + '\n')
                sys.stderr.flush()
            except OSError:
                raise NexoError('agent_trace', call.span)
