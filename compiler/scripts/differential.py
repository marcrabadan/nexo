#!/usr/bin/env python3
"""Differential test: every program of the phase 0 unit tests (and of
compiler/tests/programs/*.nexo) runs with the phase 0 interpreter and with
the compiled binary; their output and exit code must be the same, except
for the differences listed in compiler/tests/differences.txt (programs
whose error the compiler reports before running, see docs/decisions.md).

Usage: differential.py [--nexo=path] [-v] [filter]
"""
import importlib
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

args = [a for a in sys.argv[1:] if not a.startswith("-")]
verbose = "-v" in sys.argv
nexo = ROOT / "compiler" / "target" / "debug" / "nexo"
for a in sys.argv[1:]:
    if a.startswith("--nexo="):
        nexo = Path(a.split("=", 1)[1]).resolve()
flt = args[0] if args else ""

# ------------------------------------------------------------ harvesting

import helpers  # noqa: E402

programs = []  # (name, source, files, stdin)
_real_run, _real_error_of = helpers.run, helpers.error_of
current = {"name": "", "stdin": ""}


def snapshot():
    files = {}
    for p in Path(".").iterdir():
        if p.is_file() and p.suffix != ".nexo":
            files[p.name] = p.read_bytes()
    return files


def rec_run(src):
    programs.append((current["name"], src, snapshot(), current["stdin"]))
    return _real_run(src)


def rec_error_of(src):
    programs.append((current["name"], src, snapshot(), current["stdin"]))
    return _real_error_of(src)


helpers.run = rec_run
helpers.error_of = rec_error_of

for mod_name in ["test_interpreter", "test_numbers", "test_checker", "test_stdlib", "test_errors"]:
    mod = importlib.import_module(mod_name)
    mod.run = rec_run
    mod.error_of = rec_error_of
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_"):
            continue
        current["name"] = f"{mod_name}.{name}"
        current["stdin"] = ""
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                kwargs = {}
                if "in_tmp" in params:
                    kwargs["in_tmp"] = Path(tmp)
                if "monkeypatch" in params:
                    import io

                    class MP:
                        def setattr(self, obj, attr, value):
                            if obj is sys and attr == "stdin":
                                current["stdin"] = value.getvalue()
                            setattr(obj, attr, value)

                    saved = sys.stdin
                    kwargs["monkeypatch"] = MP()
                try:
                    fn(**kwargs)
                except Exception:
                    pass
                finally:
                    if "monkeypatch" in params:
                        sys.stdin = saved
            finally:
                os.chdir(old)

for p in sorted((ROOT / "compiler" / "tests" / "programs").glob("*.nexo")):
    stdin_file = p.with_suffix(".stdin")
    programs.append((f"programs/{p.stem}", p.read_text(encoding="utf-8"), {}, stdin_file.read_text() if stdin_file.exists() else ""))

# ---------------------------------------------------------------- running

known = set()
diff_file = ROOT / "compiler" / "tests" / "differences.txt"
if diff_file.exists():
    for line in diff_file.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            known.add(line)


def run(cmd, cwd, stdin):
    # compiled programs report the values they did not free (D76)
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", NEXO_LEAKCHECK="1")
    try:
        p = subprocess.run(cmd, cwd=cwd, input=stdin.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=60, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


seen = set()
failures = []
count = 0
for name, src, files, stdin in programs:
    key = src
    if key in seen:
        continue
    seen.add(key)
    pid = f"{name}#{len([1 for k in seen if True])}"
    if flt and flt not in name:
        continue
    count += 1
    outs = []
    for cmd in ([sys.executable, "-m", "nexo.cli", "t.nexo"], [str(nexo), "t.nexo"]):
        with tempfile.TemporaryDirectory() as tmp:
            for fname, data in files.items():
                (Path(tmp) / fname).write_bytes(data)
            (Path(tmp) / "t.nexo").write_text(src, encoding="utf-8")
            outs.append(run(cmd, tmp, stdin))
    (pc, po), (cc, co) = outs
    if (pc, po) != (cc, co):
        tag = f"{name}: {src.splitlines()[0][:60] if src.strip() else ''}"
        failures.append((name, src, pc, po, cc, co))

shown = 0
for name, src, pc, po, cc, co in failures:
    marker = " (known)" if any(k in src for k in known) else ""
    if marker and not verbose:
        continue
    shown += 1
    print(f"DIFF {name}{marker}")
    if verbose or True:
        print("  program: " + src.replace("\n", "\n           ").rstrip())
        print(f"  phase 0 (exit {pc}): " + po.replace("\n", "\n    ").rstrip())
        print(f"  compiled (exit {cc}): " + co.replace("\n", "\n    ").rstrip())
unknown = [f for f in failures if not any(k in f[1] for k in known)]
print(f"{count - len(failures)}/{count} identical, {len(failures) - len(unknown)} known differences, {len(unknown)} unexpected")
sys.exit(1 if unknown else 0)
