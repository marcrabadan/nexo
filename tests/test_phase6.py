"""Phase 6 (D126-D133): the tools that check the guarantees of the
specification, in both phases with the same output, and the release
builds of the compiler (reproducible, with a bill of materials)."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import ROOT, commands, compiler, run

PHASES = commands()
IDS = [c[0] for c in PHASES]

C_PROGRAM = '''use c "libm" as m
## the length of a vector
fn hyp(a: float, b: float) -> float:
    return m.sqrt(a * a + b * b)
print("{m.cbrt(27.0)}")
print(hyp(3.0, 4.0))
'''


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_audit_lists_the_uses_of_c(phase, tmp_path):
    (tmp_path / "m.nexo").write_text(C_PROGRAM)
    (tmp_path / "safe.nexo").write_text("print(1)\n")
    proc = run(phase[1], ["audit"], tmp_path)
    assert proc.stdout.decode() == (
        'm.nexo:1: use c "libm" as m: C code runs here without Nexo\'s checks (implicit unsafe). '
        "Keep the library reviewed; a published package needs a signed review of it.\n"
        "m.nexo:4: m.sqrt(...) calls C (implicit unsafe). "
        "Check its arguments and its result: nothing protects the memory it touches.\n"
        "m.nexo:5: m.cbrt(...) calls C (implicit unsafe). "
        "Check its arguments and its result: nothing protects the memory it touches.\n"
        "Unsafe uses: 3. Files with unsafe code: 1.\n")
    assert proc.returncode == 0
    proc = run(phase[1], ["audit", "safe.nexo"], tmp_path)
    assert proc.stdout.decode() == "No unsafe code in safe.nexo.\n"


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_docs_for_ai(phase, tmp_path):
    (tmp_path / "m.nexo").write_text(C_PROGRAM + "type P:\n    x: int\n    fn norm(self) -> int:\n        return self.x\n")
    out = run(phase[1], ["docs", "--for-ai", "m.nexo"], tmp_path).stdout.decode()
    assert out.startswith("# Nexo 0.1: what a program can use\n")
    assert "print(values..., end: str = \"\\n\")  # Write the values separated by spaces, then end.\n" in out
    assert out.endswith("## This program (m.nexo)\nfn hyp(a: float, b: float) -> float\n"
                        "    ## the length of a vector\ntype P\n    x: int\n    fn norm(self) -> int\n")
    assert run(phase[1], ["docs", "--html"], tmp_path).returncode == 2


def test_the_api_list_is_exact():
    """api.json names every builtin a program can use, and nothing else."""
    from nexo.stdlib import core
    api = json.loads((ROOT / "nexo" / "stdlib" / "api.json").read_text())
    named = set()
    for section in api["sections"]:
        for sig, _ in section["entries"]:
            named.add(re.match(r"[\w.]+", sig).group().split(".")[0])
    for names, _ in api["values"]:
        named.update(n.strip() for n in names.split(","))
    named.update(api["types"])
    resolve = (ROOT / "compiler" / "nexoc" / "src" / "resolve.rs").read_text()
    library = re.search(r"pub const LIBRARY_NAMES: &\[&str\] = &\[(.*?)\];", resolve, re.S).group(1)
    builtins = set(re.findall(r'"(\w+)"', library))
    first = re.search(r"let mut v = vec!\[(.*?)\];", resolve, re.S).group(1)
    builtins |= set(re.findall(r'"(\w+)"', first)) | {"read", "write", "read_lines", "save", "json", "csv"}
    errors = re.search(r"pub const ERROR_KINDS: &\[&str\] = &\[(.*?)\];", resolve, re.S).group(1)
    builtins |= set(re.findall(r'"(\w+)"', errors))
    builtins |= {"connect"}
    builtins = {b for b in builtins if not b.startswith("__")}
    usable = builtins - set(api["unavailable"]) - {"set", "list", "map", "money"} | {"set"}
    # every builtin is listed, and everything listed exists in both phases
    assert usable - named == set()
    assert named - builtins - {"set", "list", "map", "money", "str", "float", "int", "decimal", "bool"} == set()
    defined = set(core.exports())
    assert {n for n in named if n in {"print", "len", "PI", "half_up", "validate"}} <= defined
    methods = dict(api["methods"])
    assert set(methods) == {"list", "set", "map", "str", "channel"}
    for t in ("list", "set", "map", "str"):
        assert set(core.METHODS[t]) == set(re.findall(r"(\w+)\(", methods[t])), t


SUGGEST = """type Level:
    low
    high

## half of a positive number
fn half(n: int) -> int:
    expect n > 0
    return n // 2

fn score(s: 1..5, mode: "fast" | "slow") -> int:
    if mode == "fast":
        return s * 2
    return s

fn rate(l: Level) -> float:
    match l:
        low:
            return 0.5
        high:
            return 1.5

fn shout(text: str) -> str:
    return text.upper() + "{{!}}"

print("the top level does not run")
"""

SUGGESTED = """# Edge cases proposed by nexo test --suggest from types, contracts and unions: check each expected value before keeping it.

test "half: suggested edge cases":
    assert half(1) == 0
    var kind = ""
    try:
        half(0)
    else e:
        kind = e.kind
    assert kind == "ContractBroken"
    kind = ""
    try:
        half(-1)
    else e:
        kind = e.kind
    assert kind == "ContractBroken"
    assert half(9223372036854775807) == 4611686018427387903

test "score: suggested edge cases":
    assert score(1, "fast") == 2
    assert score(5, "fast") == 10
    assert score(1, "slow") == 1

test "rate: suggested edge cases":
    assert rate(low) == 0.5
    assert rate(high) == 1.5

test "shout: suggested edge cases":
    assert shout("") == "{{!}}"
    assert shout("a b") == "A B{{!}}"
"""


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_suggest(phase, tmp_path):
    (tmp_path / "calc.nexo").write_text(SUGGEST)
    proc = run(phase[1], ["test", "--suggest", "calc.nexo"], tmp_path, timeout=600)
    assert proc.stdout.decode() == SUGGESTED
    assert sorted(p.name for p in tmp_path.iterdir()) == ["calc.nexo"]
    # the tests it proposes pass
    (tmp_path / "calc.nexo").write_text(SUGGEST + "\n" + SUGGESTED)
    proc = run(phase[1], ["test", "calc.nexo"], tmp_path, timeout=600)
    assert proc.stdout.decode().endswith("4 tests: 4 passed, 0 failed\n")
    (tmp_path / "none.nexo").write_text("print(1)\n")
    proc = run(phase[1], ["test", "--suggest", "none.nexo"], tmp_path)
    assert proc.stdout.decode() == "No functions with typed parameters in none.nexo.\n"


INTENT = """ai:
    provider = "ollama"
    model = "llama"
    url = "{base}"

## the double of n
fn double(n: int) -> int:
    return n * 3

## the double of n
fn twice(n: int) -> int:
    return n * 2

fn plain(n: int) -> int:
    return n

fn positive(n: int) -> int:
    expect n > 0
    return n
"""


class Model(BaseHTTPRequestHandler):
    asked: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body["messages"][-1]["content"]
        Model.asked.append(text)
        if "n * 3" in text:
            answer = {"agrees": False, "reason": "it gives three times n, not twice."}
        else:
            answer = {"agrees": True, "reason": ""}
        data = json.dumps({"message": {"content": json.dumps(answer)}, "prompt_eval_count": 1, "eval_count": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def model():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_check_intent(phase, model, tmp_path):
    Model.asked.clear()
    (tmp_path / "app.nexo").write_text(INTENT.format(base=model))
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    proc = run(phase[1], ["check", "--intent", "app.nexo"], tmp_path, env=env, timeout=600)
    assert proc.stdout.decode() == (
        "Warning in app.nexo, line 6:\n"
        "  ## the double of n\n"
        "  ^^^^^^^^^^^^^^^^^^\n"
        'The comments of "double" and its code disagree: it gives three times n, not twice. '
        "Change the code or the comments so they say the same.\n"
        "Disagreements: 1.\n")
    assert proc.returncode == 1
    # three functions have comments or contracts; `plain` has neither
    assert len(Model.asked) == 3 and not any("fn plain" in a for a in Model.asked)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.nexo", "nexo"] or \
        sorted(p.name for p in tmp_path.iterdir()) == ["app.nexo"]
    (tmp_path / "app.nexo").write_text(INTENT.format(base=model).replace("n * 3", "n * 2"))
    proc = run(phase[1], ["check", "--intent", "app.nexo"], tmp_path, env=env, timeout=600)
    assert proc.stdout.decode() == "The comments and the code agree in app.nexo.\n"
    # without a model the questions cannot be asked
    (tmp_path / "app.nexo").write_text("## one\nfn one() -> int:\n    return 1\n")
    proc = run(phase[1], ["check", "--intent", "app.nexo"], tmp_path, env=env, timeout=600)
    assert proc.stdout.decode() == "Error: No AI provider is configured. Run: nexo ai setup\n"
    assert proc.returncode == 1


def test_release_builds_are_reproducible(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    src = ROOT / "examples" / "shop_api" / "shop.nexo"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    run([str(nexo)], ["build", "--release", str(src), "-o", str(a / "shop")], ROOT, timeout=900)
    run([str(nexo)], ["build", "--release", str(src), "-o", str(b / "shop")], tmp_path, timeout=900)
    assert (a / "shop").read_bytes() == (b / "shop").read_bytes()
    sbom = (a / "shop.sbom.json").read_text()
    assert sbom == (b / "shop.sbom.json").read_text()
    doc = json.loads(sbom)
    assert doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.5"
    import hashlib
    assert doc["metadata"]["component"]["hashes"][0]["content"] == hashlib.sha256((a / "shop").read_bytes()).hexdigest()
    names = {c["name"] for c in doc["components"]}
    assert {"nexo-runtime", "libsqlite3-sys", "rustls", str(src)} <= names
    files = [c for c in doc["components"] if c["type"] == "file"]
    assert files[0]["hashes"][0]["content"] == hashlib.sha256(src.read_bytes()).hexdigest()
    # a development build writes no bill of materials
    run([str(nexo)], ["build", str(src), "-o", str(tmp_path / "dev")], ROOT, timeout=900)
    assert not (tmp_path / "dev.sbom.json").exists()


def test_fuzz():
    """A few random programs give the same output in both phases (the
    nightly workflow runs many more)."""
    import subprocess
    import sys
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    proc = subprocess.run([sys.executable, str(ROOT / "compiler" / "scripts" / "fuzz.py"), f"--nexo={nexo}",
                           "--seed=0", "--count=12"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1200)
    assert proc.returncode == 0, proc.stdout.decode()


def test_a_large_program_compiled_in_parts(tmp_path):
    """Development mode compiles a large program in parts at the same time
    (D135); it prints what phase 0 prints."""
    import importlib.util
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    spec = importlib.util.spec_from_file_location("goals", ROOT / "compiler" / "bench" / "goals.py")
    goals = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(goals)
    (tmp_path / "big.nexo").write_text(goals.program(3000))
    a = run(PHASES[0][1], ["big.nexo"], tmp_path, timeout=600)
    b = run([str(nexo)], ["big.nexo"], tmp_path, timeout=900)
    assert a.returncode == b.returncode == 0
    assert a.stdout == b.stdout


WORKFLOW = '''var answer = input()

fn crash_unless_go():
    var box = none
    if answer == "go":
        box = "ok"
    print(box.upper())

workflow job(n: int):
    step print("one", n)
    step crash_unless_go()
    step print("three", n)

job(1)
job(2)
'''


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_workflow_resumes_after_the_program_stops(phase, tmp_path):
    """A workflow saves its state after every step (section 10, D151): a
    program that stops in a step runs, the next time, from that step on."""
    (tmp_path / "job.nexo").write_text(WORKFLOW)
    proc = run(phase[1], ["job.nexo"], tmp_path, stdin=b"crash\n")
    assert proc.stdout.decode().startswith("one 1\nError in job.nexo, line 7:")
    saved = list((tmp_path / ".nexo" / "workflows").iterdir())
    assert [json.loads(p.read_text()) for p in saved] == [
        {"workflow": "job", "args": "[1]", "done": 1, "until": None, "error": None}]
    # the other phase reads the same file
    other = PHASES[-1] if phase is PHASES[0] else PHASES[0]
    proc = run(other[1], ["job.nexo"], tmp_path, stdin=b"go\n")
    assert proc.stdout.decode() == "OK\nthree 1\none 2\nOK\nthree 2\n"
    assert proc.returncode == 0
    assert not (tmp_path / ".nexo").exists()



GRADE = '''type Level = "low" | "mid" | "high"

## the level of a score from 0 to 100
fn level(score: int) -> Level:
    expect score >= 0
    if score < 50:
        return "low"
    elif score < 80:
        return "mid"
    return "high"

## the points a level is worth
fn points(l: Level) -> int:
    match l:
        "low":
            return 0
        "mid":
            return 1
        "high":
            return 3

## how many digits n has
fn digits(n: int) -> int:
    var count = 1
    var rest = n
    while rest >= 10:
        rest = rest // 10
        count += 1
    return count

test "a middle score":
    assert level(60) == "mid"
'''


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_suggested_cases_raise_branch_coverage(phase, tmp_path):
    """The measure of "Tests proposed automatically" (D131, D152): branch
    coverage with the developer's tests, and with the cases that nexo test
    --suggest proposes added to them."""
    (tmp_path / "grade.nexo").write_text(GRADE)
    before = run(phase[1], ["test", "--coverage", "grade.nexo"], tmp_path).stdout.decode()
    assert "Branch coverage: 2 of 10 (20%).\n" in before
    suggested = run(phase[1], ["test", "--suggest", "grade.nexo"], tmp_path).stdout.decode()
    (tmp_path / "grade.nexo").write_text(GRADE + "\n" + suggested)
    after = run(phase[1], ["test", "--coverage", "grade.nexo"], tmp_path).stdout.decode()
    assert after.splitlines()[4:] == ["4 tests: 4 passed, 0 failed", "Branch coverage: 10 of 10 (100%)."]
