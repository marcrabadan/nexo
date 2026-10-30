"""Operator-owned, sealed agent/tool grants, independent of model messages."""
from dataclasses import dataclass
from pathlib import Path
import json
import os

from . import security_context
from .errors import NexoError, SOURCES

STATE = None
FILES = {}


class Configuration(tuple):
    def __new__(cls, agents, tools, state, fingerprint, path):
        self = super().__new__(cls, (agents, tools))
        self.state, self.fingerprint, self.path = state, fingerprint, path
        return self


def load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate authorization key")
            result[key] = value
        return result

    with open(path, "rb") as file:
        text = file.read(1048577)
    if len(text) > 1048576:
        raise ValueError("authorization exceeds 1 MiB")
    from .json_limits import nesting_within
    if not nesting_within(text):
        raise ValueError("JSON nesting exceeds the supported bound")
    doc = json.loads(text, object_pairs_hook=unique)
    def exact(value, keys):
        if not isinstance(value, dict) or set(value) != set(keys):
            raise ValueError("invalid authorization fields")
    def identity(name):
        if not isinstance(name, str) or "::" not in name:
            raise ValueError("invalid authorization identity")
        file, symbol = name.rsplit("::", 1)
        if not symbol or not all(c.isalnum() or c == "_" for c in symbol):
            raise ValueError("invalid authorization symbol")
        file = (Path(path).parent / file).resolve(strict=True)
        if not file.is_file():
            raise ValueError("authorization identity is not a file")
        return file, symbol
    def permissions(entries):
        from .stdlib.policy import KINDS
        if not isinstance(entries, list):
            raise ValueError("invalid permissions")
        result = []
        for e in entries:
            if not isinstance(e, list) or len(e) < 2 or not all(isinstance(x, str) for x in e) or e[0] not in ("allow", "deny") or e[1] not in KINDS:
                raise ValueError("invalid permission rule")
            if (e[1] == "shell" and len(e) != 2) or (e[1] != "shell" and len(e) < 3):
                raise ValueError("invalid permission targets")
            result.append((e[0], e[1], tuple(e[2:])))
        return tuple(result)
    version = doc.get("version") if isinstance(doc,dict) else None
    exact(doc, ("version", "agents", "tools", "state") if version == 2 else ("version", "agents", "tools"))
    if type(version) is not int or version not in (1,2):
        raise ValueError("unknown authorization version")
    if not isinstance(doc["agents"], dict) or not isinstance(doc["tools"], dict):
        raise ValueError("invalid grants")
    agents, tools = {}, {}
    for name, grant in doc["tools"].items():
        exact(grant, ("permissions", "confirm"))
        key = identity(name)
        if key in tools or type(grant["confirm"]) is not bool:
            raise ValueError("duplicate tool identity or invalid confirmation")
        tools[key] = permissions(grant["permissions"]), grant["confirm"]
    for name, grant in doc["agents"].items():
        exact(grant, ("permissions", "tools"))
        if not isinstance(grant["tools"], list):
            raise ValueError("invalid agent tool allowlist")
        allowed = [identity(n) for n in grant["tools"]]
        key = identity(name)
        if key in agents or len(set(allowed)) != len(allowed) or any(n not in tools for n in allowed):
            raise ValueError("duplicate identity or unknown tool")
        agents[key] = permissions(grant["permissions"]), frozenset(allowed)
    from . import agent_state
    state = {}
    if version == 2:
        if not isinstance(doc['state'],dict): raise ValueError('invalid state grants')
        roots = {}
        for name, value in doc['state'].items():
            ident = identity(name)
            if ident not in agents or ident in state: raise ValueError('invalid state identity')
            grant = agent_state.parse(value, Path(path).parent)
            if grant.directory in roots and roots[grant.directory] != grant: raise ValueError('conflicting state account')
            if any(grant.directory != root and (grant.directory in root.parents or root in grant.directory.parents) for root in roots): raise ValueError('overlapping state grants')
            roots[grant.directory] = grant
            state[ident] = grant
    return Configuration(agents,tools,state,agent_state.digest(text.decode()),Path(path).resolve(strict=True))


def initialize(root):
    global STATE, FILES
    STATE, FILES = None, {}
    path = os.environ.get("NEXO_SECURITY_AUTHORIZATION")
    if not path:
        return
    try:
        STATE = load(path)
        dependencies=os.environ.get("NEXO_SECURITY_DEPENDENCIES")
        STATE.protected=(STATE.path,)+((Path(dependencies).resolve(strict=True),) if dependencies else ())
        FILES = {name: Path(name).resolve(strict=True) for name in (*SOURCES, root) if Path(name).is_file()}
    except (OSError, ValueError, TypeError, RecursionError):
        raise NexoError("security_authorization")


@dataclass(frozen=True)
class Seal:
    identity: object
    snapshot: tuple


def seal(file, name):
    return Seal((FILES[file], name) if file in FILES else None, security_context.capture())


def enter_agent(agent, span):
    if STATE is None:
        if os.environ.get("NEXO_SECURITY_DEPENDENCIES"):
            raise NexoError("security_dispatch", span)
        return security_context.inherit(security_context.capture())
    grant = STATE[0].get(agent.identity)
    if grant is None:
        raise NexoError("security_dispatch", span)
    return security_context.attenuate(agent.snapshot, grant[0])


def enter_tool(agent, tool, span):
    if STATE is None:
        return security_context.inherit(security_context.capture()), False
    a = STATE[0].get(agent.identity)
    t = STATE[1].get(tool.identity)
    if a is None or t is None or tool.identity not in a[1]:
        raise NexoError("security_dispatch", span)
    return security_context.attenuate(tool.snapshot, t[0]), t[1]


def check_confirmation(required, span):
    if required and os.environ.get("NEXO_SECURITY_SANDBOX"):
        raise NexoError("security_dispatch", span)


def protect(kind, value, span):
    if STATE is None: return
    from .stdlib.policy import resolve
    if kind == "write" and STATE.state:
        path=Path(resolve(value))
        if any(path==file or path in file.parents for file in STATE.protected): raise NexoError("agent_state",span)
    for grant in STATE.state.values():
        denied = kind == 'env' and value == grant.key
        if kind in ('read','write'):
            path = Path(resolve(value))
            denied |= path == grant.directory or grant.directory in path.parents or (kind == 'write' and path in grant.directory.parents)
        if denied: raise NexoError('agent_state',span)


def state_grant(seal, span):
    if STATE is None or seal.identity not in STATE.state:
        raise NexoError('agent_state',span)
    return STATE.state[seal.identity]


def context_identity(seal):
    """Pin caller ceilings too: cached data must not cross into a weaker role."""
    from .stdlib import policy
    from .agent_state import digest
    def rules(entries):
        if entries is None: return None
        rows=[]
        for effect,kind,targets in entries:
            values=[[target,policy.resolve(target)] if kind in ('read','write') else [target] for target in targets]
            rows.append(json.dumps([effect,kind,sorted(values)],ensure_ascii=False,separators=(',',':')))
        return sorted(set(rows))
    def scopes(snapshot):
        return sorted(set(json.dumps(rules(r),ensure_ascii=False,separators=(',',':')) for r in snapshot))
    modules=security_context.RULES or {}
    grants=[]
    if STATE is not None:
        grants=[(str(file)+'::'+name,rules(grant[0])) for entries in STATE for (file,name),grant in entries.items()]
    value=[str(Path.cwd().resolve()),rules(policy.STATE['rules']),policy.STATE['release'],
           scopes(security_context.capture()),scopes(seal.snapshot),
           sorted((str(Path(file).resolve()),rules(entries)) for file,entries in modules.items()),sorted(grants)]
    return digest(json.dumps(value,ensure_ascii=False,separators=(',',':')))
