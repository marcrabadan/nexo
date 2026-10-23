"""Phase 1: the conformance suite and the examples, compiled to native
code by the Rust compiler (compiler/). Skipped when cargo or clang are not
installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"
CONFORMANCE = ROOT / "tests" / "conformance"
EXAMPLES = ROOT / "examples"

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None or shutil.which("clang") is None, reason="cargo and clang are needed"
)


@pytest.fixture(scope="session")
def nexo():
    subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
    return COMPILER / "target" / "debug" / "nexo"


def run(cmd, cwd, stdin=b""):
    return subprocess.run(cmd, cwd=cwd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)


@pytest.mark.parametrize("source", sorted(CONFORMANCE.rglob("*.nexo")), ids=lambda p: str(p.relative_to(CONFORMANCE)))
def test_conformance_compiled(source, tmp_path, nexo):
    work = tmp_path / "work"
    shutil.copytree(source.parent, work)
    args_file = source.with_suffix(".args")
    args = args_file.read_text().split() if args_file.exists() else []
    proc = run([str(nexo), *args, source.name], work)
    expected = source.with_suffix(".expected").read_text(encoding="utf-8")
    assert proc.stdout.decode("utf-8") == expected
    is_error = any(line.startswith(("Error en ", "Error in ")) for line in expected.splitlines())
    assert proc.returncode == (1 if is_error else 0)


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.nexo")), ids=lambda p: p.stem)
def test_example_compiled(example, tmp_path, nexo):
    work = tmp_path / "examples"
    shutil.copytree(EXAMPLES, work)
    stdin = example.with_suffix(".stdin")
    proc = run([str(nexo), example.name], work, stdin.read_bytes() if stdin.exists() else b"")
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode("utf-8") == example.with_suffix(".expected").read_text(encoding="utf-8")
    if 'test "' in example.read_text(encoding="utf-8"):
        proc = run([str(nexo), "test", example.name], work)
        out = proc.stdout.decode("utf-8")
        assert proc.returncode == 0, out
        assert "✗" not in out
