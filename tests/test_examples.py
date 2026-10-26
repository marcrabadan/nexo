"""Every program in examples/ runs and prints NAME.expected (with
NAME.stdin as its input, if present). Programs with test blocks must also
pass `nexo test`."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from phases import commands
from phases import run as run_phase

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
ENV = {"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}


def run(args, cwd, stdin=b""):
    return subprocess.run([sys.executable, "-m", "nexo.cli", *args], cwd=cwd, input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, timeout=60)


@pytest.fixture
def workdir(tmp_path):
    work = tmp_path / "examples"
    shutil.copytree(EXAMPLES, work)
    return work


def test_there_are_at_least_ten_examples():
    assert len(list(EXAMPLES.glob("*.nexo"))) >= 10


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.nexo")), ids=lambda p: p.stem)
def test_example_output(example, workdir):
    stdin = example.with_suffix(".stdin")
    proc = run([example.name], workdir, stdin.read_bytes() if stdin.exists() else b"")
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode("utf-8") == example.with_suffix(".expected").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "example",
    [p for p in sorted(EXAMPLES.glob("*.nexo")) if 'test "' in p.read_text(encoding="utf-8")],
    ids=lambda p: p.stem,
)
def test_example_tests_pass(example, workdir):
    proc = run(["test", example.name], workdir)
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, out
    assert "✗" not in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_events_example(phase, tmp_path):
    """examples/events handles a stream, so it runs under `nexo test` only
    (a program with `when` keeps waiting for messages); both phases print
    events.expected."""
    work = tmp_path / "events"
    shutil.copytree(EXAMPLES / "events", work)
    proc = run_phase(phase[1], ["test", "events.nexo"], work)
    assert proc.stdout.decode("utf-8") == (work / "events.expected").read_text(encoding="utf-8")
