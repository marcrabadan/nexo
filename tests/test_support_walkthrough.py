"""The downloadable walkthrough exercises browser transport against a real Nexo backend."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

ROOT = Path(__file__).resolve().parents[1]


def test_support_walkthrough_session_csrf_model_and_database(tmp_path):
    log = tmp_path / 'demo.log'
    with log.open('w+') as output:
        process = subprocess.Popen([sys.executable, str(ROOT / 'docs/walkthrough/support/run.py'),
                                   '--state', str(tmp_path / 'state')], cwd=ROOT,
                                   stdout=output, stderr=output, start_new_session=True)
        try:
            for _ in range(200):
                if process.poll() is not None:
                    raise AssertionError(log.read_text())
                if 'Local support demo:' in log.read_text():
                    break
                time.sleep(0.05)
            else:
                raise AssertionError('Demo startup timed out: ' + log.read_text())
            base = 'http://127.0.0.1:8765'
            client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            try:
                client.open(base + '/api/tickets')
                raise AssertionError('API allowed a missing session')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            with client.open(base) as response:
                page = response.read().decode()
            csrf = re.search(r'name="nexo-csrf-token" content="([^"]+)"', page)[1]
            data = json.dumps({'text': 'I was charged twice.'}).encode()
            denied = urllib.request.Request(base + '/api/tickets', data=data,
                headers={'Content-Type': 'application/json', 'Origin': base})
            try:
                client.open(denied)
                raise AssertionError('Write allowed a missing CSRF token')
            except urllib.error.HTTPError as error:
                assert error.code == 403
            request = urllib.request.Request(base + '/api/tickets', data=data,
                headers={'Content-Type': 'application/json', 'Origin': base, 'X-CSRF-Token': csrf})
            with client.open(request) as response:
                assert response.status == 201
                ticket = json.load(response)
            assert ticket['text'] == 'I was charged twice.'
            assert ticket['summary'] == 'A customer reports a duplicate order charge.'
            with client.open(base + '/api/tickets') as response:
                assert json.load(response) == [ticket]
            # The API itself rejects direct unauthenticated requests.
            try:
                urllib.request.urlopen('http://127.0.0.1:8081/tickets')
                raise AssertionError('Backend allowed a missing bearer token')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            assert (tmp_path / 'state/support.db').is_file()
        finally:
            import signal
            os.killpg(process.pid, signal.SIGINT) if process.poll() is None else None
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
