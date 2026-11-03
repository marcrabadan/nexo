#!/usr/bin/env python3
"""Strict experimental Web qualification: failures, skips and empty runs block."""
import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SUITES = (
    'tests/test_web_static.py', 'tests/test_web_ssg.py', 'tests/test_web_ssr.py',
    'tests/test_web_hydration.py', 'tests/test_web_conditional_hydration.py',
    'tests/test_web_m6_completion.py', 'tests/test_web_pwa_lifecycle.py',
)


def results(path):
    """Count actual cases, never trust a green process or aggregate alone."""
    cases = list(ET.parse(path).getroot().iter('testcase'))
    counts = dict(tests=len(cases), passed=0, failed=0, errors=0, skipped=0)
    for case in cases:
        kind = next((name for name in ('error', 'failure', 'skipped') if case.find(name) is not None), None)
        counts[{'error':'errors', 'failure':'failed', 'skipped':'skipped'}.get(kind, 'passed')] += 1
    return counts


def accepted(returncode, counts, error=None):
    return returncode == 0 and counts['tests'] > 0 and counts['passed'] == counts['tests'] and error is None


def fingerprint():
    paths = set((ROOT/'nexo').rglob('*.py')) | set((ROOT/'nexo').glob('web*.js'))
    paths |= {ROOT/name for name in SUITES} | {Path(__file__).resolve(), ROOT/'tests/test_web_frontend.py'}
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('chromium', 'firefox', 'webkit'), required=True)
    parser.add_argument('--output', type=Path, required=True, help='Dedicated evidence directory.')
    args = parser.parse_args(argv)
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    junit, log = output/'junit.xml', output/'pytest.log'
    # An interrupted run must never reuse a previous green report.
    junit.unlink(missing_ok=True)
    (output/'report.json').unlink(missing_ok=True)
    env = dict(os.environ, NEXO_WEB_REQUIRE_BROWSER='1', NEXO_WEB_BROWSER=args.browser, PYTEST_ADDOPTS='')
    command = [sys.executable, '-m', 'pytest', *SUITES, '-q', f'--junitxml={junit}']
    sources = fingerprint()
    started = time.monotonic()
    with log.open('w') as stream:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    error = None
    try:
        counts = results(junit)
    except (OSError, ET.ParseError) as exc:
        counts = dict(tests=0, passed=0, failed=0, errors=0, skipped=0)
        error = type(exc).__name__
    unchanged = sources == fingerprint()
    passed = accepted(completed.returncode, counts, error) and unchanged
    browser_versions = []
    if error is None:
        browser_versions = sorted({item.get('value') for item in ET.parse(junit).getroot().iter('property')
                                   if item.get('name') == 'browser_version'})
    try:
        playwright = version('playwright')
    except PackageNotFoundError:
        playwright = None
    report = dict(schema_version=1, status='passed' if passed else 'blocked',
                  scope='experimental M6 qualification; not a stable-release declaration',
                  browser=args.browser, python=platform.python_version(), playwright=playwright,
                  command=command, returncode=completed.returncode, counts=counts,
                  report_error=error, elapsed_seconds=round(time.monotonic()-started, 3),
                  source_sha256=sources, source_unchanged=unchanged, browser_versions=browser_versions,
                  host_precheck_bypassed=env.get('PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS') == '1',
                  artifacts={'junit':junit.name, 'log':log.name})
    (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f"{args.browser}: {report['status']} ({counts}); evidence: {output}")
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
