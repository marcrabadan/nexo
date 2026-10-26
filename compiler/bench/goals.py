#!/usr/bin/env python3
"""Measure the goals of the "More effective" table of the specification
(section "Goals and guarantees", D127) on this machine:

- run time of each benchmark against the same program in Rust (under 1.5x);
- peak memory (RSS) of the same runs against Rust (under 1.3x);
- compiling 100,000 lines in development mode (under 10 s);
- start-up: running a program that prints one line (under 20 ms).

Writes compiler/bench/goals.json. Usage, from the repository root:
    python3 compiler/bench/goals.py [--runs N]
It needs `cargo build --release` in compiler/ and rustc."""
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEXO = HERE.parent / "target" / "release" / "nexo"

GOALS = {"time": 1.5, "memory": 1.3, "compile_s": 10.0, "startup_ms": 20.0}


def run_once(cmd) -> tuple[float, int, str]:
    """Wall time, peak RSS in KB and stdout of one run."""
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    out = p.stdout.read()
    _, status, usage = os.wait4(p.pid, 0)
    t = time.perf_counter() - t0
    p.returncode = os.waitstatus_to_exitcode(status)
    if p.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed with {p.returncode}")
    return t, usage.ru_maxrss, out.decode()


def best(cmd, runs) -> tuple[float, int, str]:
    times, rss, out = [], [], ""
    for _ in range(runs):
        t, m, out = run_once(cmd)
        times.append(t)
        rss.append(m)
    return min(times), min(rss), out


def program(lines: int) -> str:
    """A program of about `lines` lines: functions, records and loops."""
    out, n = [], 0
    i = 0
    while n < lines:
        out += [
            f"type P{i}:",
            "    x: int",
            "    y: int",
            "    fn norm(self) -> int:",
            "        return self.x * self.x + self.y * self.y",
            f"fn f{i}(n: int) -> int:",
            "    var total = 0",
            "    for k in 1..n:",
            "        if k % 3 == 0:",
            f"            total += P{i}(k, {i}).norm() % 7",
            "        elif k % 5 == 0:",
            "            total -= 1",
            "        else:",
            "            total += k % 2",
            "    return total",
            f"print(f{i}(3))",
        ]
        n += 16
        i += 1
    return "\n".join(out) + "\n"


def main() -> int:
    runs = 10
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])
    names = sorted(p.stem for p in (HERE / "rust").glob("*.rs"))
    result = {"date": time.strftime("%Y-%m-%d"), "machine": f"{platform.processor() or platform.machine()}, "
              f"{os.cpu_count()} cores, {platform.system()}", "runs": runs, "goals": GOALS, "benchmarks": {}}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        print(f"{'benchmark':<10} {'time':>7} {'memory':>7}")
        for name in names:
            nx, rs = tmp / f"{name}-nexo", tmp / f"{name}-rust"
            subprocess.run([str(NEXO), "build", "--release", str(HERE / f"{name}.nexo"), "-o", str(nx)], check=True)
            subprocess.run(["rustc", "-O", "-C", "overflow-checks=on", "-o", str(rs), str(HERE / "rust" / f"{name}.rs")],
                           check=True)
            tn, mn, on = best([str(nx)], runs)
            tr, mr, orr = best([str(rs)], runs)
            if on.split() != orr.split():
                raise SystemExit(f"{name}: Nexo and Rust print different results")
            b = {"nexo_s": round(tn, 4), "rust_s": round(tr, 4), "time": round(tn / tr, 2),
                 "nexo_kb": mn, "rust_kb": mr, "memory": round(mn / mr, 2)}
            result["benchmarks"][name] = b
            print(f"{name:<10} {b['time']:>6.2f}x {b['memory']:>6.2f}x")
        src = tmp / "big.nexo"
        text = program(100_000)
        src.write_text(text)
        nlines = text.count("\n")
        t0 = time.perf_counter()
        subprocess.run([str(NEXO), "build", str(src), "-o", str(tmp / "big")], check=True)
        compile_s = time.perf_counter() - t0
        hello = tmp / "hello"
        subprocess.run([str(NEXO), "build", "--release", str(HERE / "hello.nexo"), "-o", str(hello)], check=True)
        startup = best([str(hello)], max(runs, 20))[0]
    bs = result["benchmarks"].values()
    result["worst_time"] = max(b["time"] for b in bs)
    result["worst_memory"] = max(b["memory"] for b in bs)
    result["compile"] = {"lines": nlines, "seconds": round(compile_s, 2)}
    result["startup_ms"] = round(startup * 1000, 1)
    result["met"] = {
        "time": result["worst_time"] < GOALS["time"],
        "memory": result["worst_memory"] < GOALS["memory"],
        "compile": compile_s < GOALS["compile_s"],
        "startup": result["startup_ms"] < GOALS["startup_ms"],
    }
    print(f"worst time {result['worst_time']}x (goal < {GOALS['time']}x), "
          f"worst memory {result['worst_memory']}x (goal < {GOALS['memory']}x)")
    print(f"compile {nlines} lines: {compile_s:.2f} s (goal < {GOALS['compile_s']:.0f} s)")
    print(f"start-up: {result['startup_ms']} ms (goal < {GOALS['startup_ms']:.0f} ms)")
    (HERE / "goals.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
