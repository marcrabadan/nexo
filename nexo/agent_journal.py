"""D151 checkpoint recovery for the bounded agent protocol; uncertain work never replays."""
from contextlib import contextmanager
from pathlib import Path
import json
from . import agent_state, authorization
from .errors import NexoError, SOURCES
from .types import RecordType, BuiltinType

def safe_type(call, typ, depth=0):
    from . import ast as A
    if depth > 16:
        return False
    if isinstance(typ, BuiltinType):
        return typ.name in ('str', 'int', 'float', 'decimal', 'bool', 'list', 'map', 'any')
    if not isinstance(typ, RecordType):
        return False
    for field in typ.fields:
        if field.default is not None:
            return False

        def safe(expr):
            if any(not safe(param) for param in getattr(expr, 'params', [])):
                return False
            if not isinstance(expr, A.TypeName):
                return isinstance(expr, (A.RangeType, A.LiteralUnion))
            if expr.name in ('str', 'int', 'float', 'decimal', 'bool', 'list', 'map', 'any', 'date', 'time'):
                return True
            inner = typ.env.lookup(expr.name) if typ.env is not None else None
            return isinstance(inner, RecordType) and safe_type(call, inner, depth + 1)
        if not safe(field.type):
            return False
    return True

class Journal:

    def __init__(self, call, store, name, identity, max_steps):
        self.call, self.store, self.name, self.identity, self.max_steps = (call, store, name, identity, max_steps)
        self.target = ''
        self.started = False
        self.frame = store.read(name)
        if self.frame is not None:
            f = self.frame
            fields = {'version', 'identity', 'phase', 'step', 'messages', 'calls',
                      'results', 'text', 'tool_turn', 'target', 'tokens'}
            if not isinstance(f, dict) or set(f) != fields:
                raise ValueError('invalid checkpoint fields')
            if (type(f['tool_turn']) is not bool or type(f['version']) is not int
                    or f['version'] != 1 or f['identity'] != identity
                    or f['phase'] not in ('ready', 'response', 'approval', 'complete',
                                          'provider_pending', 'tool_pending', 'failed')):
                raise ValueError('invalid checkpoint identity')
            if type(f['step']) is not int or not 0 <= f['step'] < max_steps or (not isinstance(f['text'], str)):
                raise ValueError('invalid checkpoint step')
            for key in ('messages', 'calls', 'results'):
                if not isinstance(f[key], list):
                    raise ValueError('invalid checkpoint queue')
            if any((not isinstance(v, str) for k in ('messages', 'results') for v in f[k])):
                raise ValueError('invalid checkpoint messages')
            from .stdlib.ai import _agent_json
            for v in f['messages'] + f['results']:
                _agent_json(v)
            for c in f['calls']:
                if not isinstance(c, list) or len(c) != 3 or (not all((isinstance(v, str) for v in c))) or (not isinstance(_agent_json(c[2]), dict)):
                    raise ValueError('invalid checkpoint call')
            if len({c[0] for c in f['calls']}) != len(f['calls']):
                raise ValueError('duplicate saved call')
            if not f['tool_turn'] and (f['calls'] or f['results']):
                raise ValueError('invalid final response queue')
            if f['phase'] in ('complete','ready') and (f['tool_turn'] or f['calls'] or f['results']):
                raise ValueError('invalid completed checkpoint')
            if f['phase']=='ready' and f['text']:
                raise ValueError('invalid ready checkpoint')
            if f['phase']=='approval' and (not f['tool_turn'] or not f['calls']):
                raise ValueError('invalid approval checkpoint')
            if f['phase'] in ('provider_pending', 'tool_pending', 'failed'):
                raise ValueError('uncertain or terminated checkpoint')

    def bind(self, target):
        identity = agent_state.digest(target)
        if self.frame is not None and self.frame['target'] != identity:
            raise ValueError('changed agent target')
        from . import agent_tokens
        if self.frame is not None: agent_tokens.restore(self.frame["tokens"],self.call.span)
        agent_tokens.attach(self.call,self.store,self.name,self.identity+"\n"+identity,self.frame is None)
        self.target = identity

    def save(self, phase, step, messages, calls, results, text, tool_turn=False):
        enc = lambda v: json.dumps(v, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        from . import agent_tokens
        self.frame = {'tokens': agent_tokens.states(), 'version': 1, 'identity': self.identity, 'phase': phase, 'step': step, 'messages': [enc(m) for m in messages], 'calls': [[c, n, enc(a)] for c, n, a in calls], 'results': [enc(v) for v in results], 'text': text, 'tool_turn': tool_turn, 'target': self.target}
        self.store.write(self.name, self.frame)
        self.started = True

    def phase(self, phase):
        self.frame['phase'] = phase
        self.store.write(self.name, self.frame)
        self.started = True

    def fail(self):
        if self.started and self.frame is not None and (self.frame['phase'] != 'complete'):
            self.phase('failed')

@contextmanager
def open_run(call, agent, run, input, typ):
    if run is None:
        if authorization.STATE is not None and agent.seal.identity in authorization.STATE.state:
            raise NexoError('agent_state', call.span)
        yield None
        return
    if not isinstance(run, str) or not 1 <= len(run.encode()) <= 256 or (input is not None and (not isinstance(input, str))) or (typ is not None and (not safe_type(call, typ))) or agent.servers:
        raise NexoError('agent_state', call.span)
    store = None
    try:
        grant = authorization.state_grant(agent.seal, call.span)
        store = agent_state.Store(grant)
        source = sorted((str(Path(file).resolve()),agent_state.digest(text)) for file,text in SOURCES.items())
        agent_identity = str(agent.seal.identity[0]) + '::' + agent.seal.identity[1]
        binding = ['python',authorization.context_identity(agent.seal),authorization.STATE.fingerprint,
                   source,agent_identity,run,input,getattr(typ,'name','str')]
        ident = agent_state.digest(json.dumps(binding,ensure_ascii=False,separators=(',',':')))
        name = agent_state.digest(json.dumps([agent_identity, run], ensure_ascii=False, separators=(',', ':')))
        with store.lock(name):
            journal = Journal(call, store, name, ident, agent.max_steps)
            try:
                yield journal
            except BaseException:
                journal.fail()
                raise
    except NexoError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise NexoError('agent_state', call.span)
    except Exception as exc:
        from cryptography.exceptions import InvalidTag
        if isinstance(exc, InvalidTag):
            raise NexoError('agent_state', call.span)
        raise
