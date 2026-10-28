"""Operator-owned, sealed agent/tool grants, independent of model messages."""
from dataclasses import dataclass
from pathlib import Path
import json
import os

from . import security_context
from .errors import NexoError, SOURCES

STATE = None
FILES = {}


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
    exact(doc, ("version", "agents", "tools"))
    if type(doc["version"]) is not int or doc["version"] != 1:
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
    return agents, tools


def initialize(root):
    global STATE, FILES
    STATE, FILES = None, {}
    path = os.environ.get("NEXO_SECURITY_AUTHORIZATION")
    if not path:
        return
    try:
        STATE = load(path)
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
