"""The REPL (D57, D88): the compiler's REPL shows what phase 0 shows."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"

SESSION = '''x = 41
x + 1
fn double(n):
    return n * 2

double(x)
var count = 0
count += 5
count
print("hi", end: "!\\n")
name = input("Name? ")
Ana
print("Hello {name}")
y = nope
[1, 2, 3] |> map it * 10 |> where it > 10
type P:
    a: int

P(a: 1)
fail("boom")
write("t.out.txt", "saved")
read("t.out.txt")
{"k": [1.5, none, "s"]}
x = 2
print(x, count)
'''


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, input=SESSION.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=600, env=dict(os.environ, NEXO_LEAKCHECK=""))
    return proc.returncode, proc.stdout.decode("utf-8")


def test_phase_0_session(tmp_path):
    code, out = run([sys.executable, "-m", "nexo.cli", "--lang", "en"], tmp_path)
    assert code == 0
    assert ">>> 42\n" in out and ">>> 82\n" in out and ">>> 5\n" in out
    assert "Name? >>> Hello Ana\n" in out
    assert '"nope" does not exist.' in out
    assert ">>> [20, 30]\n" in out
    assert ">>> P(a: 1)\n" in out
    assert '>>> "saved"\n' in out
    assert (tmp_path / "t.out.txt").read_text() == "saved"


@pytest.mark.skipif(shutil.which("cargo") is None or shutil.which("clang") is None, reason="cargo and clang are needed")
def test_the_compiled_repl_shows_what_phase_0_shows(tmp_path):
    subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
    (tmp_path / "py").mkdir()
    (tmp_path / "rs").mkdir()
    _, expected = run([sys.executable, "-m", "nexo.cli", "--lang", "en"], tmp_path / "py")
    code, out = run([str(COMPILER / "target" / "debug" / "nexo"), "--lang", "en"], tmp_path / "rs")
    assert code == 0
    assert out.splitlines()[0] == "Nexo 0.1.0 (compiler). Type code and press Enter; Ctrl-D to exit."
    assert out.splitlines()[1:] == expected.splitlines()[1:]
    # the earlier inputs replay without writing the file again
    assert (tmp_path / "rs" / "t.out.txt").read_text() == "saved"


@pytest.mark.skipif(shutil.which("cargo") is None or shutil.which("clang") is None, reason="cargo and clang are needed")
def test_a_failed_input_is_left_out(tmp_path):
    subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
    session = 'print("once")\nif true:\n    z = 5\n    fail("e")\n\nprint("after")\n'
    proc = subprocess.run([str(COMPILER / "target" / "debug" / "nexo"), "--lang", "en"], cwd=tmp_path,
                          input=session.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    out = proc.stdout.decode()
    assert out.count("once") == 1  # not printed again by the replays
    assert "Unhandled error: e." in out and out.rstrip().endswith("after\n>>>".rstrip())
