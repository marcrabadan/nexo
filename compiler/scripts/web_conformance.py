#!/usr/bin/env python3
"""Run the conformance suite and the examples as WebAssembly (D175): each
program is built with `nexo build --target web` and run by Node's WASI,
with the program's folder as its file system, and its output must equal
the native one. Programs that need what a page does not have (threads,
the network, other processes, C or Python) are listed in WEB_SKIP.
Usage: web_conformance.py [--nexo=path] [-v] [filter]"""
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
args = [a for a in sys.argv[1:] if not a.startswith("-")]
verbose = "-v" in sys.argv
nexo = ROOT / "compiler" / "target" / "debug" / "nexo"
for a in sys.argv[1:]:
    if a.startswith("--nexo="):
        nexo = Path(a.split("=", 1)[1]).resolve()
flt = args[0] if args else ""

RUNNER = """
const { WASI } = require("node:wasi");
const fs = require("node:fs");
const [wasm, ...rest] = process.argv.slice(2);
const wasi = new WASI({ version: "preview1", args: ["app", ...rest], env: { NEXO_LEAKCHECK: "1" },
                        preopens: { ".": "." }, returnOnExit: true });
WebAssembly.compile(fs.readFileSync(wasm))
  .then((m) => WebAssembly.instantiate(m, wasi.getImportObject()))
  .then((i) => { process.exitCode = wasi.start(i); });
"""

# what a page has no way to do: threads, sockets, processes, dynamic libraries
WEB_SKIP_FILE = ROOT / "compiler" / "tests" / "web_skip.txt"


def skipped() -> set[str]:
    if not WEB_SKIP_FILE.exists():
        return set()
    return {l.split("#")[0].strip() for l in WEB_SKIP_FILE.read_text().splitlines() if l.split("#")[0].strip()}


def run(cmd, cwd, stdin=b""):
    p = subprocess.run(cmd, cwd=cwd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def case(cid, src, folder, expected, stdin, extra):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(folder, work)
        (Path(tmp) / "run.js").write_text(RUNNER)
        if extra:
            # `nexo test` and the like are not programs a page runs
            return None
        code, out = run([str(nexo), "build", "--target", "web", src.name, "-o", "../app"], work)
        if code != 0:
            return (cid, expected, out, code) if out != expected else None
        code, out = run(["node", "--no-warnings", "--stack-size=8000", str(Path(tmp) / "run.js"),
                         str(Path(tmp) / "app.wasm")], work, stdin)
    is_error = any(l.startswith("Error in ") for l in expected.splitlines())
    if out != expected or code != (1 if is_error else 0):
        return (cid, expected, out, code)
    return None


def main():
    skip = skipped()
    jobs = []
    conf = ROOT / "tests" / "conformance"
    for src in sorted(conf.rglob("*.nexo")):
        cid = str(src.relative_to(conf).with_suffix(""))
        if flt not in cid or not src.with_suffix(".expected").exists() or cid in skip:
            continue
        extra = src.with_suffix(".args").exists()
        jobs.append((cid, src, src.parent, src.with_suffix(".expected").read_text(encoding="utf-8"), b"", extra))
    ex = ROOT / "examples"
    for src in sorted(ex.glob("*.nexo")):
        cid = "examples/" + src.stem
        if flt not in cid or cid in skip:
            continue
        stdin = src.with_suffix(".stdin").read_bytes() if src.with_suffix(".stdin").exists() else b""
        jobs.append((cid, src, ex, src.with_suffix(".expected").read_text(encoding="utf-8"), stdin, False))
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        results = list(pool.map(lambda j: case(*j), jobs))
    ran = sum(1 for j in jobs if not j[5])
    failures = [r for r in results if r]
    for cid, expected, out, code in failures:
        print(f"FAIL {cid} (exit {code})")
        if verbose:
            print("--- expected\n" + expected + "--- got\n" + out)
    print(f"{ran - len(failures)}/{ran} passed as WebAssembly ({len(skip)} skipped: compiler/tests/web_skip.txt)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
