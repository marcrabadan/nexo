"""`nexo lint` and `nexo fix` (D86): the pairs in tests/lint/ in both
phases, fixed programs print what the originals printed, and both phases
give the same warnings on every program of the repository."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexo.errors import NexoError
from nexo.lint import fix_source, lint_source

ROOT = Path(__file__).resolve().parent.parent
PAIRS = ROOT / "tests" / "lint"
COMPILER = ROOT / "compiler"
HAS_CARGO = shutil.which("cargo") is not None


@pytest.fixture(scope="session")
def compiled():
    if not HAS_CARGO:
        pytest.skip("cargo is needed")
    subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
    return COMPILER / "target" / "debug" / "nexo"


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    return proc.returncode, proc.stdout.decode("utf-8")


def interpreter():
    return [sys.executable, "-m", "nexo.cli"]


SOURCES = sorted(PAIRS.glob("*.nexo"))


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.stem)
def test_lint_pairs_phase_0(source):
    code, out = run(interpreter() + ["lint", source.name], PAIRS)
    assert out == source.with_suffix(".expected").read_text(encoding="utf-8")
    assert code == 1


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.stem)
def test_fix_pairs_phase_0(source):
    fixed, count = fix_source(source.read_text(encoding="utf-8"), source.name)
    assert fixed == source.with_suffix(".fixed").read_text(encoding="utf-8")
    assert count > 0
    assert fix_source(fixed, source.name) == (fixed, 0)


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.stem)
def test_lint_and_fix_pairs_compiled(source, compiled, tmp_path):
    code, out = run([str(compiled), "lint", source.name], PAIRS)
    assert out == source.with_suffix(".expected").read_text(encoding="utf-8")
    assert code == 1
    copy = tmp_path / source.name
    shutil.copy(source, copy)
    run([str(compiled), "fix", source.name], tmp_path)
    assert copy.read_text(encoding="utf-8") == source.with_suffix(".fixed").read_text(encoding="utf-8")


def test_the_safe_fixes_keep_what_the_program_prints(tmp_path):
    source = PAIRS / "rules.nexo"
    fixed = tmp_path / "fixed.nexo"
    fixed.write_text(source.with_suffix(".fixed").read_text(encoding="utf-8"), encoding="utf-8")
    _, before = run(interpreter() + [str(source)], tmp_path)
    _, after = run(interpreter() + [str(fixed)], tmp_path)
    assert before == after
    assert lint_source(fixed.read_text(encoding="utf-8"), "fixed.nexo")  # unsafe ones remain


def test_lint_without_warnings(tmp_path):
    (tmp_path / "ok.nexo").write_text('print("hi")\n', encoding="utf-8")
    code, out = run(interpreter() + ["lint", "ok.nexo"], tmp_path)
    assert (code, out) == (0, "No warnings in ok.nexo.\n")


def test_a_loop_first_in_a_block_does_not_hide_later_reads(compiled, tmp_path):
    # a block and its first statement start at the same place
    (tmp_path / "loop.nexo").write_text("for i in 1..3:\n    print(i)\nvar n = 0\nwhile n < 3:\n    n += 1\n"
                                        "print(n)\n", encoding="utf-8")
    for cmd in (interpreter(), [str(compiled)]):
        assert run(cmd + ["lint", "loop.nexo"], tmp_path) == (0, "No warnings in loop.nexo.\n")


def programs():
    found = []
    for path in sorted(ROOT.rglob("*.nexo")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in (".venv", ".git") or "target" in rel.parts:
            continue
        try:
            lint_source(path.read_text(encoding="utf-8"), path.name)
        except NexoError:
            continue
        found.append(path)
    return found


def test_both_phases_warn_alike(compiled, tmp_path):
    """Programs the compiler accepts get the same warnings and the same fixes."""
    checked = 0
    for n, path in enumerate(programs()):
        work = tmp_path / str(n)
        work.mkdir()
        shutil.copy(path, work / path.name)
        rs_code, rs_out = run([str(compiled), "lint", path.name], work)
        if rs_out.startswith(("Error in ", "Error:")):
            continue  # a compile error only phase 1 finds (D67, D68)
        py_code, py_out = run(interpreter() + ["lint", path.name], work)
        assert (rs_code, rs_out) == (py_code, py_out), str(path)
        run([str(compiled), "fix", path.name], work)
        expected, _ = fix_source(path.read_text(encoding="utf-8"), path.name)
        assert (work / path.name).read_text(encoding="utf-8") == expected, str(path)
        checked += 1
    assert checked > 50
