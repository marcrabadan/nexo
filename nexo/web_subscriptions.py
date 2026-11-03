"""Bounded public SSE snapshot channel, explicitly mounted by a trusted WSGI host."""
from collections import deque
import json
import re
import threading
import uuid
from .web_state import checked, packed, MAX_BYTES


def schema_type(schema, depth=0):
    if depth > 32 or not isinstance(schema, dict):
        raise ValueError('Invalid event schema')
    kind = schema.get('kind')
    if kind in ('int', 'str', 'bool'):
        return kind
    if kind == 'list':
        return ('list', schema_type(schema['item'], depth + 1))
    if kind != 'record' or not isinstance(schema.get('fields'), dict):
        raise ValueError('Invalid event schema')
    return ('record', 'Event', tuple((name, schema_type(field, depth + 1)) for name, field in schema['fields'].items()))


class EventChannel:
    def __init__(self, schema, *, capacity=32, heartbeat=1):
        if type(capacity) is not int or not 1 <= capacity <= 128 or not 0.05 <= heartbeat <= 30:
            raise ValueError('Invalid channel bounds')
        self._type = schema_type(schema)
        self._frames = deque(maxlen=capacity)
        self._condition = threading.Condition()
        self._epoch = uuid.uuid4().hex
        self._sequence = 0
        self._closed = False
        self._heartbeat = heartbeat
        self._clients = 0

    @property
    def clients(self):
        with self._condition:
            return self._clients

    def publish(self, value):
        body = json.dumps(packed(self._type, checked(self._type, value)), ensure_ascii=True, separators=(',', ':'))
        if len(body.encode()) > 65536:
            raise ValueError('Event exceeds 64 KiB')
        with self._condition:
            if self._closed or self._sequence >= 2**63 - 1:
                raise RuntimeError('Event channel closed')
            self._sequence += 1
            identity = f'{self._epoch}:{self._sequence}'
            frame = f'id: {identity}\ndata: {body}\n\n'.encode()
            self._frames.append((self._sequence, frame))
            self._condition.notify_all()
            return identity

    def events(self, last_event_id=''):
        match = re.fullmatch(r'([0-9a-f]{32}):([1-9][0-9]{0,18})', last_event_id)
        last = int(match[2]) if match and match[1] == self._epoch else None
        with self._condition:
            self._clients += 1
        try:
            yield b': connected\n\n'
            while True:
                with self._condition:
                    if self._closed:
                        return
                    if self._frames and (last is None or last < self._frames[0][0] - 1 or last > self._sequence):
                        batch = [self._frames[-1]]
                    else:
                        batch = [(seq, frame) for seq, frame in self._frames if seq > (last or 0)]
                    if not batch:
                        self._condition.wait(self._heartbeat)
                if not batch:
                    yield b': keepalive\n\n'
                else:
                    for seq, frame in batch:
                        yield frame
                        last = seq
        finally:
            with self._condition:
                self._clients -= 1

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
