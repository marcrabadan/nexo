#!/usr/bin/env python3
"""Time each benchmark in Nexo (`nexo build --release`) and in C, Rust, Go,
Java, JavaScript and Python: the same algorithm in every language, checked
to print the same output. Each time is the best of RUNS runs of the whole
process, start-up included (`hello` measures the start-up alone). The
results go to languages.json, which the site shows.

    python3 compiler/bench/languages.py [--interpreter] [name...]

--interpreter also times the phase 0 interpreter (once, up to 10 minutes
a benchmark).
"""
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NEXO = HERE.parent / "target" / "release" / "nexo"
RUNS = 5
NAMES = ["hello", "fib", "loop", "lists", "maps", "records", "strings", "parallel"]
ENV = {k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"}


def java_class(name):
    return name[0].upper() + name[1:]


def build(tmp, name):
    """(language, argv) for every implementation of a benchmark."""
    out = {}
    nx = tmp / f"{name}-nexo"
    subprocess.run([str(NEXO), "build", "--release", str(HERE / f"{name}.nexo"), "-o", str(nx)], check=True)
    out["Nexo"] = [str(nx)]
    c = tmp / f"{name}-c"
    subprocess.run(["clang", "-O2", "-pthread", "-o", str(c), str(HERE / "c" / f"{name}.c")], check=True)
    out["C"] = [str(c)]
    rs = tmp / f"{name}-rust"
    subprocess.run(["rustc", "-O", "-C", "overflow-checks=on", "-o", str(rs), str(HERE / "rust" / f"{name}.rs")],
                   check=True)
    out["Rust"] = [str(rs)]
    go = tmp / f"{name}-go"
    subprocess.run(["go", "build", "-o", str(go), str(HERE / "go" / f"{name}.go")], check=True, env=ENV)
    out["Go"] = [str(go)]
    classes = tmp / "java"
    subprocess.run(["javac", "-d", str(classes), str(HERE / "java" / f"{java_class(name)}.java")], check=True, env=ENV)
    out["Java"] = ["java", "-cp", str(classes), java_class(name)]
    out["JavaScript"] = ["node", str(HERE / "js" / f"{name}.js")]
    out["Python"] = [sys.executable, str(HERE / "python" / f"{name}.py")]
    return out


def best(cmd, runs, timeout=None):
    times, out = [], ""
    for _ in range(runs):
        t0 = time.perf_counter()
        p = subprocess.run(cmd, stdout=subprocess.PIPE, check=True, env=ENV, timeout=timeout)
        times.append(time.perf_counter() - t0)
        out = p.stdout.decode()
    return min(times), out


def version(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV)
    return p.stdout.decode().strip().splitlines()[0]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    interpreter = "--interpreter" in sys.argv
    names = args or NAMES
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in names:
            commands = build(tmp, name)
            if interpreter:
                commands["Nexo interpreter"] = [str(ROOT / ".venv" / "bin" / "nexo"), str(HERE / f"{name}.nexo")]
            row, expected = {}, None
            for lang, cmd in commands.items():
                slow = lang == "Nexo interpreter"
                try:
                    t, out = best(cmd, 1 if slow else RUNS, timeout=600 if slow else None)
                except subprocess.TimeoutExpired:
                    row[lang] = None  # over 10 minutes
                    print(f"{name:<9} {lang:<17} over 600s", flush=True)
                    continue
                if expected is None:
                    expected = out.split()
                elif out.split() != expected:
                    sys.exit(f"{name} in {lang} prints {out!r}, not what Nexo prints")
                row[lang] = round(t, 4)
                print(f"{name:<9} {lang:<17} {t:8.3f}s", flush=True)
            results[name] = row
    data = {
        "date": date.today().isoformat(),
        "machine": f"{platform.processor() or platform.machine()}, {os.cpu_count()} cores, {platform.system()}",
        "runs": RUNS,
        "versions": {
            "Nexo": "nexo build --release, LLVM IR built by " + version(["clang", "--version"]),
            "C": version(["clang", "--version"]) + ", -O2",
            "Rust": version(["rustc", "--version"]) + ", -O, overflow checks on",
            "Go": version(["go", "version"]),
            "Java": version(["java", "-version"]),
            "JavaScript": "Node.js " + version(["node", "--version"]),
            "Python": "CPython " + platform.python_version(),
        },
        "results": results,
    }
    path = HERE / "languages.json"
    if args and path.exists():  # a partial run keeps the other benchmarks
        old = json.loads(path.read_text())
        old["results"].update(results)
        results = data["results"] = old["results"]
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
