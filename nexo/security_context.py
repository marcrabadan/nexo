"""Synchronous dependency ceilings; root policy remains the outer authority."""
from contextlib import contextmanager
from pathlib import Path
import json
import os

from .errors import NexoError, SOURCES

RULES = None
STACK = []
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
    STACK.clear()
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
    depth = len(STACK)
    STACK.append(RULES.get(file, []))
    try:
        yield
    finally:
        del STACK[depth:]


def allows(kind, value, file):
    if RULES is None:
        return True
    from .stdlib.policy import matches
    def permits(rules):
        allowed = False
        for effect, k, targets in rules:
            if k == kind and (not targets or any(matches(kind, value, t) for t in targets)):
                if effect == "deny":
                    return False
                allowed = True
        return allowed
    return (file == ROOT or permits(RULES.get(file, []))) and all(permits(r) for r in STACK)


def check_result(value, span):
    if RULES is None or not STACK:
        return
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
