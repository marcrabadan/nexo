#!/usr/bin/env python3
"""Reproducible, offline static-effect benchmark; no generated program runs.

python compiler/bench/effects.py --lines 1000 10000 100000 --repeat 3
JSON records corpus hashes, wall times, output bytes and child peak RSS.
Each measurement gets a fresh worker so RUSAGE_CHILDREN is not cumulative.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def corpus(lines, shape):
    if shape == 'imported':
        modules = 10
        count = max(modules, (lines - 3 - (modules - 1)) // 2)
        chunk = (count + modules - 1) // modules
        files = {'app.nexo': 'policy:\n    allow read "data/log"\nuse m0\n'}
        for group in range(modules):
            start, end = group * chunk, min(count, (group + 1) * chunk)
            out = [f'use m{group + 1}'] if end < count else []
            for i in range(start, end):
                out.append(f'pub fn f{i}() -> str!:')
                call = 'read("data/log")' if i == count - 1 else (
                    f'f{i + 1}()' if i + 1 < end else f'm{group + 1}.f{i + 1}()')
                out.append('    return ' + call + '?')
            files[f'm{group}.nexo'] = '\n'.join(out) + '\n'
        return files
    count = max(1, (lines - 2) // 2)
    out = ['policy:', '    allow read "data/log"']
    for i in range(count):
        out.append(f'fn f{i}() -> str!:')
        if i == count - 1:
            expression = 'read("data/log")'
        elif shape == 'recursive':
            # One large cycle with one read, without executing it.
            expression = f'f{(i + 1) % count}()'
        else:
            expression = f'f{i + 1}()'
        out.append('    return ' + expression + '?')
    if shape == 'recursive':
        # Last function reaches the cycle too; control flow is over-approximated.
        out[-1] = '    return f0()? + read("data/log")?'
    while len(out) < lines:
        out.append('# fixed padding')
    return {'app.nexo': '\n'.join(out) + '\n'}


def worker(args):
    files = corpus(args.lines[0], args.shape)
    canonical = "".join(name + "\n" + text for name, text in sorted(files.items()))
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            (Path(tmp) / name).write_text(text)
        cmd = ([sys.executable, '-m', 'nexo.cli'] if args.worker == 'interpreter'
               else [str(ROOT / 'compiler/target/debug/nexo')])
        start = time.perf_counter()
        p = subprocess.run([*cmd, 'check', '--effects', '--json', 'app.nexo'], cwd=tmp,
                           env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, timeout=180)
        elapsed = time.perf_counter() - start
        if p.returncode not in (0, 1) or p.stderr:
            raise RuntimeError(p.stderr.decode('utf-8', 'replace'))
        doc = json.loads(p.stdout)
        summaries = [d['data'] for d in doc['diagnostics'] if d['code'] == 'effects_summary']
        if not summaries:
            raise RuntimeError(doc)
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        if sys.platform != 'darwin':
            rss *= 1024
        return dict(backend=args.worker, lines=sum(s.count('\n') for s in files.values()), shape=args.shape,
                    sha256=hashlib.sha256(canonical.encode()).hexdigest(), wall_seconds=elapsed,
                    peak_rss_bytes=rss, output_bytes=len(p.stdout), summaries=len(summaries),
                    incomplete=sum(not s['complete'] for s in summaries),
                    limited=sum(any(e['reason'] == 'analysis_limit' for e in s['effects']) for s in summaries))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lines', nargs='+', type=int, default=[1000, 10000, 100000])
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--shape', choices=['chain', 'recursive', 'imported'], default='chain')
    parser.add_argument('--worker', choices=['interpreter', 'compiler'])
    args = parser.parse_args()
    if any(n < 4 for n in args.lines) or args.repeat < 1:
        parser.error('lines must be >= 4 and repeat must be positive')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    rows = []
    for backend in ['interpreter', 'compiler']:
        for lines in args.lines:
            samples = []
            for _ in range(args.repeat):
                p = subprocess.run([sys.executable, __file__, '--worker', backend, '--lines', str(lines),
                                    '--shape', args.shape], capture_output=True, text=True, check=True)
                samples.append(json.loads(p.stdout))
            times = sorted(s['wall_seconds'] for s in samples)
            rows.append(dict(backend=backend, lines=lines, shape=args.shape, samples=samples,
                             median_seconds=statistics.median(times), p95_seconds=times[max(0, (95 * len(times) + 99)//100 - 1)]))
    print(json.dumps(dict(platform=platform.platform(), python=sys.version, repeat=args.repeat,
                          compiler_build='debug; cargo build --locked',
                          rustc=subprocess.check_output(['rustc', '--version'], text=True).strip(),
                          base_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                          compiler_sha256=hashlib.sha256((ROOT / 'compiler/target/debug/nexo').read_bytes()).hexdigest(),
                          measurements=rows), indent=2))


if __name__ == '__main__':
    main()
