"""The program policy (section 9, D142): what a program may read, write,
reach on the network, read from the environment, run as a shell command,
and load from C or Python. The `policy:` block is read by the parser into
`__policy([[rule, kind, target...], ...])`, the first statement of the
program, and every access of the standard library goes through check().

A `deny` rule always stops the program. Without a rule for an access, a
release build stops it, and development mode allows it and warns once,
except for shell commands. Without a policy, development mode allows
everything except shell commands. With NEXO_POLICY_RECORD set (`nexo
policy suggest`), development accesses are allowed and appended to that file.
Production and Shield executions never honor this recording override."""
from __future__ import annotations

import os
import json
import sys
import urllib.parse

from .. import errors
from ..errors import NexoError
from ..types import Builtin

KINDS = ("read", "write", "net", "env", "shell", "c", "python")

STATE: dict = {"rules": None, "release": False, "reported": set(), "violations": 0}


def reset() -> None:
    STATE["rules"] = None
    STATE["release"] = False
    STATE["reported"] = set()
    STATE["violations"] = 0


def b_policy(call, entries, release=False):
    if STATE["rules"] is not None:
        raise NexoError("policy_internal", call.span)
    from .. import security_context
    security_context.initialize(call.span.file)
    STATE["rules"] = [(e[0], e[1], list(e[2:])) for e in entries]
    STATE["release"] = bool(release) or enforcing()
    return None


# ------------------------------------------------------------- matching

def parts(path: str) -> list[str]:
    """The components of a path with `.` and `..` taken away."""
    out: list[str] = []
    for p in path.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if out and out[-1] != "..":
                out.pop()
            elif not path.startswith("/"):
                out.append(p)
            continue
        out.append(p)
    return out


def shown(path: str) -> str:
    """A path as the policy writes it: `data/../x.csv` is `x.csv`."""
    p = "/".join(parts(path))
    if path.startswith("/"):
        return "/" + p
    return p or "."


def resolve(path: str) -> str:
    """The absolute path the system opens: each component in turn, a
    symbolic link replaced by its target before the `..` after it, so
    neither a link nor `link/..` can lead out of an allowed folder."""
    full = path if path.startswith("/") else os.getcwd() + "/" + path
    todo = [c for c in reversed(full.split("/")) if c not in ("", ".")]
    out: list[str] = []
    links = 0
    while todo:
        c = todo.pop()
        if c == "..":
            if out:
                out.pop()
            continue
        here = "/" + "/".join(out + [c])
        if links < 40 and os.path.islink(here):
            links += 1
            target = os.readlink(here)
            if target.startswith("/"):
                out = []
            todo.extend(t for t in reversed(target.split("/")) if t not in ("", "."))
            continue
        out.append(c)
    return "/" + "/".join(out)


def path_matches(path: str, target: str) -> bool:
    p, t = resolve(path), resolve(target)
    return p == t or t == "/" or p.startswith(t + "/")


def host_matches(host: str, target: str) -> bool:
    t = target.lower()
    if t.startswith("*."):
        return host.endswith(t[1:])
    return host == t


def matches(kind: str, value: str, target: str) -> bool:
    if kind in ("read", "write"):
        return path_matches(value, target)
    if kind == "net":
        return host_matches(value, target)
    return value == target


def url_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


# --------------------------------------------------------------- checks

def rule_text(rule: str, kind: str, target: str | None) -> str:
    return f'{rule} {kind}' if target is None else f'{rule} {kind} "{target}"'


def enforcing() -> bool:
    return os.environ.get("NEXO_SECURITY_ENFORCE") == "1"


def security_event(kind: str, decision: str, reason: str) -> None:
    """Structured events contain no program-provided strings or secret values."""
    if os.environ.get("NEXO_SECURITY_TRACE") != "1":
        return
    denied = decision == "deny"
    STATE["violations"] += int(denied)
    operations = dict(read="file.read", write="file.write", net="network.connect",
                      env="environment.read", shell="process.execute", c="native.load", python="python.load")
    event = dict(event="security.policy.denied" if denied else "security.policy.decision",
                 severity="high" if denied else "info", operation=operations[kind], target="[redacted]",
                 decision=decision, reason=reason, violations=STATE["violations"], repeated=STATE["violations"] >= 3)
    sys.stderr.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def check(call, kind: str, value: str) -> None:
    """Stop the program, warn or record before an access (D142)."""
    target = shown(value) if kind in ("read", "write") else value
    record = os.environ.get("NEXO_POLICY_RECORD")
    if record and not STATE["release"] and not enforcing():
        with open(record, "a", encoding="utf-8") as f:
            f.write(f"{kind}\t{'' if kind == 'shell' else target}\n")
        return
    what = errors.msg(f"detail.policy_{kind}", target=target)
    rules = STATE["rules"]
    allow_line = rule_text("allow", kind, None if kind == "shell" else target)
    if rules is None:
        if enforcing():
            raise NexoError("policy_missing", call.span)
        if kind == "shell":
            raise NexoError("policy_shell", call.span, what=what)
        return
    allowed = False
    for rule, k, targets in rules:
        if k != kind:
            continue
        hit = not targets or any(matches(kind, value, t) for t in targets)
        if hit and rule == "deny":
            denied = next((t for t in targets if matches(kind, value, t)), None)
            security_event(kind, "deny", "explicit_deny")
            raise NexoError("policy_deny", call.span, what=what, rule=rule_text("deny", kind, denied))
        if hit:
            allowed = True
    if enforcing() and kind in ("c", "python", "shell"):
        security_event(kind, "deny", "isolation_unavailable")
        raise NexoError("security_isolation", call.span)
    if allowed:
        from .. import security_context
        if not security_context.allows(kind, value, call.span.file):
            security_event(kind, "deny", "capability_missing")
            raise NexoError("security_dependency", call.span)
        security_event(kind, "allow", "capability_granted")
        return
    if STATE["release"] or enforcing() or kind == "shell":
        security_event(kind, "deny", "capability_missing")
        raise NexoError("policy_denied", call.span, what=what, line=allow_line)
    security_event(kind, "warn", "development_undeclared")
    key = (kind, target)
    if key in STATE["reported"]:
        return
    STATE["reported"].add(key)
    sys.stdout.flush()
    e = NexoError("policy_undeclared", call.span, what=what, line=allow_line)
    sys.stderr.write(errors.render(call.span, e.text(), "header_warning"))
    sys.stderr.flush()


def check_url(call, url: str) -> None:
    # Avoid parser differentials with the native HTTP transport (WHATWG).
    if any(c == "\\" or ord(c) < 32 or ord(c) == 127 for c in url):
        security_event("net", "deny", "invalid_target")
        raise NexoError("security_url", call.span)
    check(call, "net", url_host(url))


# ------------------------------------------------- nexo policy show|suggest

def declared(program):
    """(line, rules) of the policy of a program, or None."""
    from ..parser import is_policy
    for s in program.body:
        if is_policy(s):
            rules = []
            for e in s.expr.args[0].value.items:
                words = ["".join(p for p in i.parts) for i in e.items]
                rules.append((words[0], words[1], words[2:]))
            return s.span.line, rules
    return None


def show_text(file: str, program) -> str:
    found = declared(program)
    if found is None:
        return errors.msg("info.policy_none", file=file) + "\n"
    line, rules = found
    out = [errors.msg("info.policy_header", file=file, line=line)]
    for kind in KINDS:
        allow = [t for r, k, ts in rules if k == kind and r == "allow" for t in ts]
        deny = [t for r, k, ts in rules if k == kind and r == "deny" for t in ts]
        if kind == "shell":
            ok = any(r == "allow" for r, k, _ in rules if k == kind) and not any(
                r == "deny" for r, k, _ in rules if k == kind)
            value = errors.msg("info.policy_yes" if ok else "info.policy_no")
        else:
            value = ", ".join(allow) or errors.msg("info.policy_nothing")
            if deny:
                value += " " + errors.msg("info.policy_except", targets=", ".join(deny))
        out.append(f"  {kind:<8}{value}")
    out.append(errors.msg("info.policy_footer", file=file))
    return "\n".join(out) + "\n"


def suggest_text(lines: list[str]) -> str:
    """The minimal policy for the accesses a test run recorded."""
    seen: dict[str, set] = {k: set() for k in KINDS}
    for line in lines:
        kind, _, target = line.partition("\t")
        if kind in seen:
            seen[kind].add(target)
    out = ["policy:"]
    for kind in KINDS:
        if not seen[kind]:
            continue
        if kind == "shell":
            out.append("    allow shell")
        else:
            out.append(f"    allow {kind} " + ", ".join(f'"{t}"' for t in sorted(seen[kind])))
    if len(out) == 1:
        out.append("    deny shell")
    return "\n".join(out) + "\n"


def exports() -> dict:
    return {"__policy": Builtin("__policy", b_policy)}
