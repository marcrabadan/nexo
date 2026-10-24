#!/usr/bin/env python3
"""Time each benchmark as a native program built by `nexo build --release`
and as the same program in Rust (rustc -O, with overflow checks, since Nexo
detects int overflow). The exit criterion of phase 2 (section 25) is under
2x Rust's time. Usage: compiler/bench/compare.py [name...]"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEXO = HERE.parent / "target" / "release" / "nexo"
RUNS = 5


def best(cmd) -> tuple[float, str]:
    times, out = [], ""
    for _ in range(RUNS):
        t0 = time.perf_counter()
        p = subprocess.run(cmd, stdout=subprocess.PIPE, check=True)
        times.append(time.perf_counter() - t0)
        out = p.stdout.decode()
    return min(times), out


def main() -> int:
    names = sys.argv[1:] or sorted(p.stem for p in (HERE / "rust").glob("*.rs"))
    worst = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        print(f"{'benchmark':<10} {'nexo':>8} {'rust':>8} {'ratio':>6}")
        for name in names:
            nx, rs = tmp / f"{name}-nexo", tmp / f"{name}-rust"
            subprocess.run([str(NEXO), "build", "--release", str(HERE / f"{name}.nexo"), "-o", str(nx)], check=True)
            subprocess.run(["rustc", "-O", "-C", "overflow-checks=on", "-o", str(rs), str(HERE / "rust" / f"{name}.rs")],
                           check=True)
            tn, on = best([str(nx)])
            tr, orr = best([str(rs)])
            same = on.split() == orr.split()
            ratio = tn / tr
            worst = max(worst, ratio)
            print(f"{name:<10} {tn:>7.3f}s {tr:>7.3f}s {ratio:>5.2f}x{'' if same else '  (different output!)'}")
    print(f"worst ratio: {worst:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
