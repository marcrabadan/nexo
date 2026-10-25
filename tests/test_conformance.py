"""Conformance tests: every tests/conformance/**/NAME.nexo is run with the
`nexo` CLI from a scratch copy of its directory, and stdout + stderr must equal
NAME.expected exactly (a .nexo without .expected is a module that other
cases use). NAME.args, if present, holds extra CLI arguments placed
before the file name (for example `--lang en` or `test`).

A case listed in tests/conformance/PENDING is expected to fail (xfail); the
list must be empty once phase 0 is complete.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFORMANCE = ROOT / "tests" / "conformance"


def pending():
    path = CONFORMANCE / "PENDING"
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def cases():
    # a .nexo without .expected is a module the cases use (D124)
    return sorted(p for p in CONFORMANCE.rglob("*.nexo") if p.with_suffix(".expected").exists())


def case_id(path):
    return str(path.relative_to(CONFORMANCE).with_suffix(""))


@pytest.mark.parametrize("source", cases(), ids=case_id)
def test_conformance(source, tmp_path):
    if case_id(source) in pending():
        pytest.xfail("pending")
    work = tmp_path / "work"
    shutil.copytree(source.parent, work)
    args_file = source.with_suffix(".args")
    args = args_file.read_text().split() if args_file.exists() else []
    proc = subprocess.run(
        [sys.executable, "-m", "nexo.cli", *args, source.name],
        cwd=work,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
        timeout=60,
    )
    expected = source.with_suffix(".expected").read_text(encoding="utf-8")
    output = proc.stdout.decode("utf-8")
    assert output == expected
    is_error = any(line.startswith(("Error en ", "Error in ")) for line in expected.splitlines())
    assert proc.returncode == (1 if is_error else 0)
