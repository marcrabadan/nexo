"""The playground (D176) and `nexo build --target web` (D175).

The playground runs phase 0 in Pyodide: its bundle must be the one
docs/playground/build.py makes from nexo/, and the examples must give
their expected output there (with Pyodide from NEXO_PYODIDE or npm,
skipped without it). A program built for the web must give the output
of the native build, run by the page's own WASI code (web.js) in Node."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from phases import ROOT, compiler, run

sys.path.insert(0, str(ROOT / "docs" / "playground"))
import build as playground  # noqa: E402

NODE = shutil.which("node")
WEB = ROOT / "compiler" / "target" / "web"


def test_the_bundle_is_built_from_the_interpreter():
    assert (ROOT / "docs" / "playground" / "nexo.zip").read_bytes() == playground.interpreter(), \
        "run python3 docs/playground/build.py"
    assert (ROOT / "docs" / "playground" / "examples.json").read_bytes() == playground.examples(), \
        "run python3 docs/playground/build.py"


def test_every_page_links_the_playground():
    for page in ["index", "learn", "examples", "how-it-works", "reference", "shield", "benchmarks", "playground"]:
        assert 'href="playground.html"' in (ROOT / "docs" / f"{page}.html").read_text(), page


def pyodide() -> Path | None:
    given = os.environ.get("NEXO_PYODIDE")
    if given:
        return Path(given)
    cache = Path.home() / ".cache" / "nexo-tests"
    package = cache / "node_modules" / "pyodide"
    if not package.is_dir() and shutil.which("npm"):
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["npm", "install", "--prefix", str(cache), "pyodide@0.27.7"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    return package if (package / "pyodide.js").is_file() else None


RUNNER = """
const { loadPyodide } = require(process.argv[2]);
const NexoPlay = require(process.argv[3]);
const fs = require("fs");
(async () => {
  const docs = process.argv[4];
  const py = await loadPyodide();
  const ex = JSON.parse(fs.readFileSync(docs + "/playground/examples.json", "utf8"));
  const runner = await NexoPlay.start(py, fs.readFileSync(docs + "/playground/nexo.zip"), ex.files);
  const results = {};
  for (const e of ex.examples) {
    let out = "";
    const r = await runner.run("run", e.source, e.stdin, (t) => { out += t; });
    results[e.name] = [r.code, out];
  }
  let tests = "";
  const bank = ex.examples.find((e) => e.name === "bank");
  results["bank [test]"] = [(await runner.run("test", bank.source, "", (t) => { tests += t; })).code, tests];
  results["fmt"] = [0, (await runner.run("fmt", "x=1\\nprint( x )\\n", "", () => {})).source];
  results["error"] = [0, ""];
  await runner.run("run", "print(nmae)\\n", "", (t) => { results["error"][1] += t; });
  console.log(JSON.stringify(results));
})();
"""


def test_the_examples_run_in_pyodide(tmp_path):
    package = pyodide() if NODE else None
    if package is None:
        pytest.skip("needs node and Pyodide (NEXO_PYODIDE, or npm to install it)")
    (tmp_path / "run.js").write_text(RUNNER)
    p = subprocess.run([NODE, str(tmp_path / "run.js"), str(package), str(ROOT / "docs/assets/playground-core.js"),
                        str(ROOT / "docs")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    results = json.loads(p.stdout.decode().strip().splitlines()[-1])
    # the zones and crypto come from Pyodide's own packages, downloaded on
    # demand: skipped when they cannot be
    offline = {"signup"}
    for src in sorted((ROOT / "examples").glob("*.nexo")):
        if src.stem in offline and "needs the Python module" in results[src.stem][1]:
            continue
        assert results[src.stem] == [0, src.with_suffix(".expected").read_text()], src.stem
    assert results["bank [test]"][0] == 0 and results["bank [test]"][1].endswith("2 tests: 2 passed, 0 failed\n")
    assert results["fmt"] == [0, "x = 1\nprint(x)\n"]
    assert results["error"][1] == ('Error in main.nexo, line 1:\n  print(nmae)\n        ^^^^\n'
                                   '"nmae" does not exist. Define it before using it.\n')


WEB_RUNNER = """
const NexoWeb = require(process.argv[2]);
const fs = require("fs");
let out = "";
NexoWeb.run(fs.readFileSync(process.argv[3]), { stdin: process.argv[4], write: (t) => { out += t; }, ask: () => null })
  .then((code) => { process.stdout.write(out); process.exitCode = code; });
"""

PROGRAM = '''type Point:
    x: int
    y: int

fn norm(p: Point) -> int:
    return p.x * p.x + p.y * p.y

name = input("name? ")
points = [Point(x: i, y: i + 1) for i in 1..4]
print("Hello, {name}: {points |> map norm(it) |> sum}")
print(time(2026, 3, 29, 1, 30, zone: "Europe/Madrid") + 1h)
print(10.00 EUR / 3)
var total = 0
for i in 1..100000:
    total += i % 7
print(total)
print(run("echo x"))
'''


def test_a_program_built_for_the_web_runs_in_a_page_as_natively(tmp_path):
    nexo = compiler()
    if nexo is None or NODE is None or not (WEB / "libnexo_runtime.a").is_file():
        pytest.skip("needs the compiler, node and compiler/scripts/web_runtime.sh")
    (tmp_path / "app.nexo").write_text('policy:\n    allow shell\n\n' + PROGRAM)
    (tmp_path / "run.js").write_text(WEB_RUNNER)
    out = run([str(nexo)], ["build", "--target", "web", "app.nexo", "-o", "site/app"], tmp_path)
    assert out.returncode == 1 and b"rust-lld" in out.stdout  # the folder must exist
    (tmp_path / "site").mkdir()
    assert run([str(nexo)], ["build", "--target", "web", "app.nexo", "-o", "site/app"], tmp_path).returncode == 0
    assert sorted(p.name for p in (tmp_path / "site").iterdir()) == ["app.html", "app.js", "app.wasm"]
    assert '<script src="app.js"></script>' in (tmp_path / "site" / "app.html").read_text()
    web = subprocess.run([NODE, str(tmp_path / "run.js"), str(tmp_path / "site" / "app.js"),
                          str(tmp_path / "site" / "app.wasm"), "Ana\n"], stdout=subprocess.PIPE, timeout=120)
    native = run([str(nexo)], ["app.nexo"], tmp_path, stdin=b"Ana\n").stdout.decode()
    lines = web.stdout.decode().splitlines()
    assert lines[:4] == native.splitlines()[:4] == [
        "name? Hello, Ana: 84", "2026-03-29T03:30:00+02:00", "3.33 EUR", "300000"]
    assert web.returncode == 1
    assert lines[-1] == ("A shell command is not available in a program built for the web: a page has no threads, "
                         "other processes or network sockets. Build it for this machine (without --target web) to "
                         "use it.")


def test_other_targets_are_not_in_this_edition(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs the compiler")
    (tmp_path / "app.nexo").write_text("print(1)\n")
    out = run([str(nexo)], ["build", "--target", "arduino", "app.nexo"], tmp_path)
    assert (out.returncode, out.stdout.decode()) == (1, 'Error: "arduino" is not a target of this edition. Write '
                                                        "--target web for WebAssembly, or leave --target out for "
                                                        "this machine.\n")
    empty = tmp_path / "none"
    empty.mkdir()
    out = run([str(nexo)], ["build", "--target", "web", "app.nexo"], tmp_path, env={"NEXO_WEB_RUNTIME": str(empty)})
    assert (out.returncode, out.stdout.decode()) == (1, f"Error: There is no runtime for the web in {empty}. Build it "
                                                        "with compiler/scripts/web_runtime.sh, or set "
                                                        "NEXO_WEB_RUNTIME to its folder.\n")
