"""nexo.ci (section 12, D122, D123): `pipeline` blocks, `nexo ci run`,
which runs their steps here, and `nexo ci export`, which writes the file of
a CI service that runs each step with `nexo ci run --step`."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Native, NexoFailure, failure, show
from .cfg import atom_text, show_entry

STATE_DIR = Path(".nexo") / "ci"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class Pipeline:
    def __init__(self, name, span):
        self.name, self.span = name, span
        self.triggers: list = []  # ("push", branch | None) | ("pull_request", None)
        self.secrets: list[str] = []
        self.version = None  # "auto" | "1.2.3" | None
        self.steps: list = []  # (name, fn, entry)
        self.after: dict = {}  # "success" | "failure" -> fn


PIPELINES: list[Pipeline] = []


class Run(Native):
    """The run of a pipeline: what the steps keep, the version, the step."""

    type_name = "run"
    __slots__ = ("pipeline", "vars", "yes", "failed")

    def __init__(self, pipeline, vars_, yes):
        self.pipeline, self.vars, self.yes, self.failed = pipeline, vars_, yes, None

    def repr(self) -> str:
        return f"run({self.pipeline.name})"


CURRENT: list = []  # the run in progress
FILE = [""]  # the program `nexo ci` runs


def reset() -> None:
    PIPELINES.clear()
    CURRENT.clear()


def m(key: str, **args) -> str:
    return errors.msg(key, **args)


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- blocks

def b_pipeline(call, name, entries):
    if any(p.name == name for p in PIPELINES):
        raise NexoError("duplicate_name", call.span, name=name)
    p = Pipeline(name, call.span)
    for e in entries:
        w = e.words()
        key = e.key()
        is_code = e.body is not None and not isinstance(e.body, list)
        if w[:1] == ["trigger"] and e.values is None and e.body is None and len(e.head) == 1 and not e.options:
            if w[1:] == ["pull_request"]:
                p.triggers.append(("pull_request", None))
            elif w[1:] == ["push"]:
                p.triggers.append(("push", None))
            elif len(w) == 4 and w[1:3] == ["push", "to"] and isinstance(w[3], str):
                p.triggers.append(("push", w[3]))
            else:
                raise e.error("ci_trigger", entry=show_entry(e))
        elif key == "secrets" and e.body is None and not e.options:
            v = e.values
            if len(v) == 1 and len(v[0]) == 3 and v[0][1:] == ["from", "vault"]:
                raise NexoError("not_supported", e.span, what='"secrets = [...] from vault"')
            names = v[0][0] if len(v) == 1 and len(v[0]) == 1 else None
            if not isinstance(names, list) or not all(isinstance(n, str) and re.match(r"^[A-Z_][A-Z0-9_]*$", n)
                                                     for n in names):
                raise e.error("ci_secrets")
            p.secrets = list(names)
        elif key == "version" and e.body is None and not e.options:
            v = e.value()
            if v != "auto" and not (isinstance(v, str) and VERSION.match(v)):
                raise e.error("ci_version", value=atom_text(v))
            p.version = v
        elif len(w) == 2 and w[0] == "step" and is_code:
            if any(s[0] == w[1] for s in p.steps):
                raise NexoError("duplicate_name", e.span, name=w[1])
            p.steps.append((w[1], e.body, e))
        elif len(w) == 2 and w[0] == "after" and w[1] in ("success", "failure") and is_code:
            if w[1] in p.after:
                raise NexoError("duplicate_name", e.span, name=f"after {w[1]}")
            p.after[w[1]] = e.body
        else:
            raise e.error("ci_entry", entry=show_entry(e))
    if not p.steps:
        raise NexoError("ci_no_steps", call.span, name=name)
    PIPELINES.append(p)
    return None


def b_ci_get(call, run, name):
    if name in run.vars:
        return run.vars[name]
    raise NexoError("ci_missing", call.span, name=name)


def b_ci_keep(call, run, name, value):
    json_value(call, name, value)
    run.vars[name] = value
    return None


def json_value(call, name: str, value) -> None:
    """What a step keeps travels between the jobs of a CI as JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for v in value:
            json_value(call, name, v)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for v in value.values():
            json_value(call, name, v)
        return
    from ..types import type_name
    raise NexoError("ci_value", call.span, name=name, vtype=type_name(value))


# ------------------------------------------------------------ functions

def current(call, fn: str) -> Run:
    if not CURRENT:
        raise NexoError("ci_outside", call.span, fn=fn)
    return CURRENT[-1]


def shell(call, command: str) -> tuple[int, str]:
    sys.stdout.flush()
    p = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    if out:
        call.interp.write(out if out.endswith("\n") else out + "\n")
    return p.returncode, out


def b_run(call, command):
    if not isinstance(command, str):
        from .core import expects
        raise expects(call, "run", "str", command)
    code, out = shell(call, command)
    if code != 0:
        raise failure("Error", call.span, key="ci_run", command=command, code=code)
    return out[:-1] if out.endswith("\n") else out


def b_test(call):
    current(call, "test")
    command = [sys.executable, "-m", "nexo.cli"]
    if errors.LANG != "es":
        command += ["--lang", errors.LANG]
    sys.stdout.flush()
    p = subprocess.run(command + ["test"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2])))
    out = p.stdout.decode("utf-8", "replace")
    call.interp.write(out)
    if p.returncode != 0:
        raise failure("Error", call.span, key="ci_tests")
    return None


def b_build_image(call, name):
    run = current(call, "build_image")
    if not isinstance(name, str) or not re.match(r"^[a-z0-9][a-z0-9._/-]*$", name):
        from .core import expects
        raise expects(call, "build_image", "str", name)
    tag = f"{name}:{run.vars['version']}"
    tool = os.environ.get("NEXO_CONTAINER_TOOL") or "docker"
    command = f"{tool} build -t {tag} ."
    code, _ = shell(call, command)
    if code != 0:
        raise failure("Error", call.span, key="ci_run", command=command, code=code)
    return tag


def infra_command(call, fn: str, command: str, stage, image):
    current(call, fn)
    from . import infra, infra_cloud
    for v, what in ((stage, "stage"), (image, "image")):
        if v is not None and not isinstance(v, str):
            from .core import expects
            raise expects(call, fn, "str", v)
    if not infra.INFRAS:
        raise NexoError("infra_none", call.span, file=FILE[0])
    sys.stdout.flush()
    infra.IMAGE[0] = image
    try:
        return infra_cloud.command(call.interp, command, [], stage, True)
    finally:
        infra.IMAGE[0] = None


def b_apply(call, stage=None, image=None):
    if infra_command(call, "apply", "apply", stage, image) != 0:
        raise failure("Error", call.span, key="ci_apply")
    return None


def b_plan(call, stage=None, image=None):
    from . import infra_cloud
    lines: list = []
    infra_cloud.CAPTURE.append(lines)
    try:
        infra_command(call, "plan", "plan", stage, image)
    finally:
        infra_cloud.CAPTURE.pop()
    return "\n".join(lines)


def b_approve(call, by):
    run = current(call, "approve")
    if not isinstance(by, list) or not by or not all(isinstance(x, str) for x in by):
        from .core import expects
        raise expects(call, "approve", "list[str]", by)
    names = ", ".join(by)
    step = run.vars["step"]
    if run.yes:
        call.interp.write(m("info.ci_approved", step=step, names=names) + "\n")
        return None
    call.interp.write(m("info.ci_approve", step=step, names=names))
    sys.stdout.flush()
    line = sys.stdin.readline()
    if line.strip().lower() not in ("sí", "si", "s", "yes", "y"):
        if not line.endswith("\n"):
            call.interp.write("\n")
        raise failure("Error", call.span, key="ci_approve", step=step)
    return None


def b_comment(call, text):
    current(call, "comment")
    text = show(call.interp.bind_value(text))
    call.interp.write(m("info.ci_comment") + "\n" + text + "\n")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "comment.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    return None


def unavailable(name):
    def b(call, *args, **kwargs):
        raise NexoError("not_supported", call.span, what=f'"{name}()"')
    return b


# --------------------------------------------------------------- version

def git(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return 1, ""
    return p.returncode, p.stdout.decode("utf-8", "replace")


def auto_version() -> str:
    """The last tag vX.Y.Z raised by the commits after it: fix -> patch,
    feat -> minor, `!` or BREAKING CHANGE -> major (D122)."""
    code, _ = git(["rev-parse", "--git-dir"])
    if code != 0:
        raise NexoError("ci_git", None)
    code, out = git(["tag", "--merged", "HEAD", "--list", "v[0-9]*"])
    best, tag = (0, 0, 0), None
    for t in out.split():
        mt = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", t)
        if mt and tuple(int(x) for x in mt.groups()) >= best:
            best, tag = tuple(int(x) for x in mt.groups()), t
    code, out = git(["log", "--format=%B%x00", f"{tag}..HEAD" if tag else "HEAD"])
    bump = 0
    for msg in out.split("\x00"):
        msg = msg.strip()
        if not msg:
            continue
        head = re.match(r"^(\w+)(\([^)]*\))?(!)?:", msg)
        if (head and head.group(3)) or re.search(r"^BREAKING[ -]CHANGE:", msg, re.M):
            bump = 3
        elif head and head.group(1) == "feat":
            bump = max(bump, 2)
        elif head and head.group(1) == "fix":
            bump = max(bump, 1)
    x, y, z = best
    if bump == 3:
        x, y, z = x + 1, 0, 0
    elif bump == 2:
        y, z = y + 1, 0
    elif bump == 1:
        z += 1
    return f"{x}.{y}.{z}"


def version_of(p: Pipeline) -> str:
    if p.version is None:
        return "dev"
    return auto_version() if p.version == "auto" else p.version


# ------------------------------------------------------------------ run

def state_path(p: Pipeline) -> Path:
    return STATE_DIR / f"{p.name}.json"


def load_state(p: Pipeline) -> dict | None:
    try:
        return json.loads(state_path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(p: Pipeline, run: Run) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"pipeline": p.name, "vars": run.vars}
    if run.failed:
        data["failed"] = run.failed
    from .data import to_json
    state_path(p).write_text(to_json(data) + "\n", encoding="utf-8")


def call_step(interp, fn, run: Run, report) -> bool:
    CURRENT.append(run)
    try:
        interp.call(fn, [(None, run)], None)
        return True
    except (NexoFailure, NexoError) as exc:
        report(exc)
        return False
    finally:
        CURRENT.pop()
        sys.stdout.flush()


def pick(name: str | None, path: str) -> Pipeline:
    if not PIPELINES:
        raise NexoError("ci_none", None, file=path)
    if name is None:
        if len(PIPELINES) > 1:
            raise NexoError("ci_which", None, names=", ".join(p.name for p in PIPELINES))
        return PIPELINES[0]
    for p in PIPELINES:
        if p.name == name:
            return p
    s = errors.suggest(name, [p.name for p in PIPELINES])
    if s:
        raise NexoError("ci_unknown", None, fix="suggest", what="pipeline", name=name, suggestion=s)
    raise NexoError("ci_unknown", None, what="pipeline", name=name)


def run_pipeline(interp, p: Pipeline, step: str | None, after: str | None, yes: bool, report) -> int:
    missing = [s for s in p.secrets if os.environ.get(s) is None]
    if missing:
        raise NexoError("ci_secret", None, names=", ".join(missing), pipeline=p.name)
    if step is not None and all(s[0] != step for s in p.steps):
        names = [s[0] for s in p.steps]
        s = errors.suggest(step, names)
        if s:
            raise NexoError("ci_unknown", None, fix="suggest", what="step", name=step, suggestion=s)
        raise NexoError("ci_unknown", None, what="step", name=step)
    if step is None and after is None:
        run = Run(p, {"version": version_of(p), "step": ""}, yes)
    else:
        saved = load_state(p)
        if saved is None:
            run = Run(p, {"version": version_of(p), "step": ""}, yes)
        else:
            from .data import from_json
            run = Run(p, from_json(saved.get("vars", {}), None), yes)
            run.failed = saved.get("failed")
    if after is not None:
        fn = p.after.get(after)
        if fn is not None:
            if after == "failure":
                run.vars["step"] = run.failed or ""
            say(m("info.ci_after", what=after))
            if not call_step(interp, fn, run, report):
                return 1
        return 0
    steps = [s for s in p.steps if step is None or s[0] == step]
    for name, fn, _ in steps:
        run.vars["step"] = name
        say(m("info.ci_step", step=name))
        ok = call_step(interp, fn, run, report)
        if not ok:
            run.failed = name
            save_state(p, run)
            say(m("info.ci_failed", pipeline=p.name, step=name))
            if step is None and "failure" in p.after:
                say(m("info.ci_after", what="failure"))
                call_step(interp, p.after["failure"], run, report)
            return 1
        save_state(p, run)
    if step is None:
        if "success" in p.after:
            run.vars["step"] = ""
            say(m("info.ci_after", what="success"))
            if not call_step(interp, p.after["success"], run, report):
                return 1
        say(m("info.ci_done", pipeline=p.name, n=len(steps), version=run.vars["version"]))
    return 0


def exports() -> dict:
    return {
        "__pipeline": Builtin("__pipeline", b_pipeline),
        "__ci_get": Builtin("__ci_get", b_ci_get),
        "__ci_keep": Builtin("__ci_keep", b_ci_keep),
        "run": Builtin("run", b_run),
        "test": Builtin("test", b_test),
        "build_image": Builtin("build_image", b_build_image),
        "apply": Builtin("apply", b_apply),
        "plan": Builtin("plan", b_plan),
        "approve": Builtin("approve", b_approve),
        "comment": Builtin("comment", b_comment),
        "audit": Builtin("audit", unavailable("audit")),
        "scan": Builtin("scan", unavailable("scan")),
        "slack": Builtin("slack", unavailable("slack")),
    }
