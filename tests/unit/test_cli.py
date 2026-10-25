"""The nexo command: running files, the REPL and nexo test."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ENV = {"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}


def nexo(*args, cwd=None, stdin=""):
    proc = subprocess.run([sys.executable, "-m", "nexo", *args], cwd=cwd, input=stdin.encode(),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, timeout=60)
    return proc.returncode, proc.stdout.decode("utf-8")


def test_run_file(tmp_path):
    (tmp_path / "a.nexo").write_text('print("hola")\n')
    assert nexo("a.nexo", cwd=tmp_path) == (0, "hola\n")


def test_missing_file(tmp_path):
    code, out = nexo("nope.nexo", cwd=tmp_path)
    assert code == 1 and "nope.nexo" in out


def test_unhandled_error_exits_with_1(tmp_path):
    (tmp_path / "a.nexo").write_text('print("a")\nfail("boom")\nprint("b")\n')
    code, out = nexo("a.nexo", cwd=tmp_path)
    assert code == 1
    assert out == 'a\nError in a.nexo, line 2:\n  fail("boom")\n  ^^^^^^^^^^^^\nUnhandled error: boom. Handle it with try and else e.\n'


def test_usage_errors(tmp_path):
    assert nexo("--lang", "en", "a.nexo", cwd=tmp_path)[0] == 2
    assert nexo("a.nexo", "b.nexo", cwd=tmp_path)[0] == 2


def test_repl_evaluates_and_remembers():
    code, out = nexo(stdin='x = 20\nx + 1\nfn sq(n):\n    return n * n\n\nsq(3)\nprint(y)\n"txt"\n')
    assert code == 0
    lines = out.split(">>> ")
    assert "21\n" in lines and "9\n" in lines
    assert '"y" does not exist.' in out
    assert '"txt"' in out


def test_repl_in_english():
    code, out = nexo(stdin="print(nope)\n")
    assert out.startswith("Nexo") and "phase 0" in out
    assert '"nope" does not exist.' in out


def test_nexo_test_on_a_directory(tmp_path):
    (tmp_path / "ok.nexo").write_text('test "one":\n    assert 1 + 1 == 2\n')
    (tmp_path / "plain.nexo").write_text('x = 1\n')
    code, out = nexo("test", cwd=tmp_path)
    assert code == 0
    assert "✓ one" in out and out.strip().endswith("1 test: 1 passed, 0 failed")


def test_nexo_test_reports_failures(tmp_path):
    (tmp_path / "t.nexo").write_text('test "bad":\n    assert 1 == 2\n')
    code, out = nexo("test", "t.nexo", cwd=tmp_path)
    assert code == 1
    assert "✗ bad" in out and "The assertion does not hold: 1 == 2 (1 == 2)." in out
    assert out.strip().endswith("1 test: 0 passed, 1 failed")


def test_program_does_not_run_test_blocks(tmp_path):
    (tmp_path / "t.nexo").write_text('print("main")\ntest "x":\n    print("in test")\n')
    assert nexo("t.nexo", cwd=tmp_path) == (0, "main\n")


def test_nexo_check_reports_compile_errors_without_running(tmp_path):
    (tmp_path / "ok.nexo").write_text('print("never printed")\n')
    assert nexo("check", "ok.nexo", cwd=tmp_path) == (0, "No errors in ok.nexo.\n")
    (tmp_path / "bad.nexo").write_text('fn f(p):\n    return read(p)\nprint("no")\n')
    code, out = nexo("check", "bad.nexo", cwd=tmp_path)
    assert code == 1
    assert out.startswith("Error in bad.nexo, line 2:") and "no\n" not in out
