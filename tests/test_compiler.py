"""Phase 1: the conformance suite and the examples, compiled to native
code by the Rust compiler (compiler/). Skipped when cargo or clang are not
installed."""
import os
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
    # a program that does not free every value says so on stderr (D76)
    env = dict(os.environ, NEXO_LEAKCHECK="1")
    return subprocess.run(cmd, cwd=cwd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, env=env)


@pytest.mark.parametrize("source", sorted(p for p in CONFORMANCE.rglob("*.nexo") if p.with_suffix(".expected").exists()),
                         ids=lambda p: str(p.relative_to(CONFORMANCE)))
def test_conformance_compiled(source, tmp_path, nexo):
    work = tmp_path / "work"
    shutil.copytree(source.parent, work)
    args_file = source.with_suffix(".args")
    args = args_file.read_text().split() if args_file.exists() else []
    proc = run([str(nexo), *args, source.name], work)
    expected = source.with_suffix(".expected").read_text(encoding="utf-8")
    assert proc.stdout.decode("utf-8") == expected
    is_error = any(line.startswith("Error in ") for line in expected.splitlines())
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


CONTRACTS = """policy:
    deny shell

type Account:
    balance: int
fn transfer(source: Account, target: Account, amount: int):
    expect amount > 0
    ensure source.balance + target.balance == old(source.balance + target.balance)
    source.balance -= amount
    target.balance += amount
fn sloppy(a: Account):
    ensure a.balance == old(a.balance)
    a.balance -= 1
a = Account(10)
b = Account(0)
transfer(a, b, 3)
print(a.balance, b.balance)
try:
    sloppy(a)
    print("not checked")
else e:
    print(e.kind)
try:
    transfer(a, b, 0)
else e:
    print(e.kind)
"""


def test_trust_ensure_leaves_out_unproven_ensures(tmp_path, nexo):
    src = tmp_path / "c.nexo"
    src.write_text(CONTRACTS)
    proc = run([str(nexo), "build", "--release", src.name, "-o", "checked"], tmp_path)
    assert proc.returncode == 0
    assert run([str(tmp_path / "checked")], tmp_path).stdout.decode() == "7 3\nContractBroken\nContractBroken\n"
    proc = run([str(nexo), "build", "--release", "--trust-ensure", src.name, "-o", "trusted"], tmp_path)
    assert proc.returncode == 0
    # every expect stays; the ensure the compiler could not prove is gone
    assert run([str(tmp_path / "trusted")], tmp_path).stdout.decode() == "7 3\nnot checked\nContractBroken\n"
    assert run([str(nexo), "build", "--trust-ensure", src.name], tmp_path).returncode == 2


DEPTH = """
fn down(n: int) -> int:
    if n == 0:
        return 0
    return down(n - 1) + 1

fn mixed(n) -> int:
    return deeper(n)

fn deeper(n: int) -> int:
    if n == 0:
        return 0
    return mixed(n - 1) + 1

print(down({a}) + mixed({b}))
"""


def test_call_depth_limit_counts_int_only_functions(tmp_path, nexo):
    # a function on ints only keeps its depth in a register (D84); the
    # limit of D73 is the same as for any other function
    for a, b, ok in [(9999, 4999, True), (10000, 1, False), (1, 5000, False)]:
        src = tmp_path / "d.nexo"
        src.write_text(DEPTH.format(a=a, b=b))
        proc = run([str(nexo), src.name], tmp_path)
        assert (proc.returncode == 0) == ok, (a, b, proc.stdout)
        if ok:
            assert proc.stdout.decode() == f"{a + b}\n"


def test_contracts_that_read_shared_variables_hold_the_lock(tmp_path, nexo):
    # the old() snapshot and the ensure take the lock, like statements
    src = tmp_path / "s.nexo"
    src.write_text(
        "use nexo.sync\nshared xs = [1]\n"
        "fn f(n: int):\n    ensure len(xs) >= old(len(xs))\n    pass\n"
        "f(1)\nprint(\"ok\")\n"
    )
    env = dict(os.environ, NEXO_KEEP_IR="1")
    proc = subprocess.run([str(nexo), "build", src.name, "-o", "s"], cwd=tmp_path, env=env, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    ir = (tmp_path / "s.ll").read_text()
    assert ir.count("call void @nx_shared_lock()") == 2
    assert run([str(tmp_path / "s")], tmp_path).stdout.decode() == "ok\n"
