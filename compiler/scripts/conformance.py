#!/usr/bin/env python3
"""Run the conformance suite (tests/conformance) and the examples against the
compiled `nexo` binary. Usage: conformance.py [--nexo=path] [-v] [filter]"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
args = [a for a in sys.argv[1:] if not a.startswith("-")]
verbose = "-v" in sys.argv
nexo = ROOT / "compiler" / "target" / "debug" / "nexo"
for a in sys.argv[1:]:
    if a.startswith("--nexo="):
        nexo = Path(a.split("=", 1)[1]).resolve()
flt = args[0] if args else ""


def run(cmd, cwd, stdin=b""):
    # compiled programs report the values they did not free (D76)
    env = dict(os.environ, NEXO_LEAKCHECK="1")
    p = subprocess.run(cmd, cwd=cwd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, env=env)
    return p.returncode, p.stdout.decode("utf-8", "replace")


failures = []
count = 0
conf = ROOT / "tests" / "conformance"
for src in sorted(conf.rglob("*.nexo")):
    cid = str(src.relative_to(conf).with_suffix(""))
    # a .nexo without .expected is a module the cases use (D124)
    if flt not in cid or not src.with_suffix(".expected").exists():
        continue
    count += 1
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(src.parent, work)
        extra = src.with_suffix(".args").read_text().split() if src.with_suffix(".args").exists() else []
        code, out = run([str(nexo), *extra, src.name], work)
    expected = src.with_suffix(".expected").read_text(encoding="utf-8")
    is_error = any(l.startswith("Error in ") for l in expected.splitlines())
    if out != expected or code != (1 if is_error else 0):
        failures.append((cid, expected, out, code))

ex = ROOT / "examples"
for src in sorted(ex.glob("*.nexo")):
    cid = "examples/" + src.stem
    if flt not in cid:
        continue
    count += 1
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "examples"
        shutil.copytree(ex, work)
        stdin = src.with_suffix(".stdin").read_bytes() if src.with_suffix(".stdin").exists() else b""
        code, out = run([str(nexo), src.name], work, stdin)
        expected = src.with_suffix(".expected").read_text(encoding="utf-8")
        if out != expected or code != 0:
            failures.append((cid, expected, out, code))
        if 'test "' in src.read_text():
            count += 1
            code, out = run([str(nexo), "test", src.name], work)
            if code != 0 or "✗" in out:
                failures.append((cid + " [test]", "(all tests pass)", out, code))

for cid, expected, out, code in failures:
    print(f"FAIL {cid} (exit {code})")
    if verbose:
        print("--- expected\n" + expected + "--- got\n" + out)
print(f"{count - len(failures)}/{count} passed")
sys.exit(1 if failures else 0)
