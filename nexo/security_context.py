"""Immutable dependency ceilings and context-local captured permissions."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import json
import os

from .errors import NexoError, SOURCES

RULES = None
STACK = ContextVar("nexo_security_stack", default=())
ROOT = None


def load(path, root):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate permission key")
            result[k] = v
        return result
    with open(path, encoding="utf-8") as f:
        text = f.read(1048577)
    if len(text.encode("utf-8")) > 1048576:
        raise ValueError("permission file too large")
    from .json_limits import nesting_within
    if not nesting_within(text):
        raise ValueError("JSON nesting exceeds the supported bound")
    doc = json.loads(text, object_pairs_hook=unique)
    if not isinstance(doc, dict) or set(doc) != {"version", "modules"} or type(doc["version"]) is not int or doc["version"] != 1 or not isinstance(doc["modules"], dict):
        raise ValueError("invalid permission schema")
    from .stdlib.policy import KINDS
    result = {}
    for name, entries in doc["modules"].items():
        file = (Path(path).parent / name).resolve(strict=True)
        if file == Path(root).resolve(strict=True) or file in result or not isinstance(entries, list):
            raise ValueError("invalid dependency identity")
        for e in entries:
            if not isinstance(e, list) or len(e) < 2 or not all(isinstance(w, str) for w in e) or e[0] not in ("allow", "deny") or e[1] not in KINDS:
                raise ValueError("invalid dependency rule")
            if (e[1] == "shell" and len(e) != 2) or (e[1] != "shell" and len(e) < 3):
                raise ValueError("invalid dependency target")
        result[file] = [(e[0], e[1], e[2:]) for e in entries]
    return result


def initialize(root):
    global RULES, ROOT
    STACK.set(())
    RULES = None
    path = os.environ.get("NEXO_SECURITY_DEPENDENCIES")
    if not path:
        return
    try:
        profiles = load(path, root)
        ROOT = str(root)
        # Freeze source identities before any user statements execute.
        RULES = {name: profiles.get(Path(name).resolve(strict=True), []) for name in SOURCES if Path(name).exists()}
    except (OSError, ValueError, TypeError):
        raise NexoError("security_context_config")


@contextmanager
def scope(file):
    if RULES is None or file == ROOT:
        yield
        return
    token = STACK.set(STACK.get() + (RULES.get(file, []),))
    try:
        yield
    finally:
        STACK.reset(token)


def permits(rules, kind, value):
    """Pure policy predicate shared by runtime ceilings and static reports."""
    from .stdlib.policy import matches
    allowed = False
    for effect, k, targets in rules:
        if k == kind and (not targets or any(matches(kind, value, t) for t in targets)):
            if effect == "deny":
                return False
            allowed = True
    return allowed


def allows(kind, value, file):
    return (RULES is None or file == ROOT or permits(RULES.get(file, []), kind, value)) and all(
        permits(r, kind, value) for r in STACK.get())


def check_result(value, span):
    if RULES is None or not STACK.get():
        return
    _check_data(value, span)


def check_transfer(value, span=None):
    if not (os.environ.get("NEXO_SECURITY_DEPENDENCIES") or os.environ.get("NEXO_SECURITY_AUTHORIZATION") or os.environ.get("NEXO_SECURITY_STREAMS") or os.environ.get("NEXO_SECURITY_WEB")):
        return
    _check_data(value, span)


def _check_data(value, span):
    from decimal import Decimal
    from .types import Money, Quantity, Currency, LitFloat, NexoSet
    budget = [100000]
    def data(v, depth=0):
        if depth > 128 or budget[0] == 0:
            return False
        budget[0] -= 1
        if v is None or isinstance(v, (bool, int, float, str, Decimal, Money, Quantity, Currency, LitFloat)):
            return True
        if isinstance(v, (list, tuple)):
            return all(data(x,depth+1) for x in v)
        if isinstance(v, dict):
            return all(data(k,depth+1) and data(x,depth+1) for k,x in v.items())
        if isinstance(v, NexoSet):
            return all(data(x,depth+1) for x in v.items)
        return False
    if not data(value):
        raise NexoError("security_context_escape",span)


class Snapshot(tuple):
    def __new__(cls, rules, controls, finance=(), tokens=()):
        value = super().__new__(cls, rules)
        value.controls = controls
        value.finance = finance
        value.tokens = tokens
        return value


def capture():
    from . import agent_control
    from . import agent_finance, agent_tokens
    return Snapshot(STACK.get(), agent_control.capture(),agent_finance.capture(),agent_tokens.capture())


@contextmanager
def inherit(snapshot):
    from . import agent_control, agent_finance, agent_tokens
    token = STACK.set(snapshot)
    try:
        with agent_control.scope(getattr(snapshot, "controls", ()), replace=True), agent_finance.scope(getattr(snapshot,"finance",()),replace=True), agent_tokens.scope(getattr(snapshot,"tokens",()),replace=True):
            yield
    finally:
        STACK.reset(token)


@contextmanager
def intersect(snapshot):
    from . import agent_control, agent_finance, agent_tokens
    token = STACK.set(STACK.get() + snapshot)
    try:
        with agent_control.scope(getattr(snapshot, "controls", ()), replace=False), agent_finance.scope(getattr(snapshot,"finance",())), agent_tokens.scope(getattr(snapshot,"tokens",())):
            yield
    finally:
        STACK.reset(token)


@contextmanager
def attenuate(snapshot, rules):
    from . import agent_control, agent_finance, agent_tokens
    token = STACK.set(STACK.get() + snapshot + (rules,))
    try:
        with agent_control.scope(getattr(snapshot, "controls", ()), replace=False), agent_finance.scope(getattr(snapshot,"finance",())), agent_tokens.scope(getattr(snapshot,"tokens",())):
            yield
    finally:
        STACK.reset(token)


def check_connector(span):
    from .stdlib.policy import enforcing
    if enforcing():
        raise NexoError("security_connector", span)
