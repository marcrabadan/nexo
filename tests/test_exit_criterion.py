"""The exit criterion of phase 4 (section 25): "a web API with a database and
`ask` is built with Nexo alone". examples/shop_api/shop.nexo passes its own
tests and, served by each phase, answers HTTP requests with SQLite and a
model (a local server that answers as Anthropic's API does)."""
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import ROOT, commands, run

APP = ROOT / "examples" / "shop_api"


class Anthropic(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        if prompt.startswith("Analyse this review"):
            text = '{"tone": "negative", "score": 2}' if "broke" in prompt else '{"tone": "positive", "score": 5}'
        else:
            text = "Mixed: some love it, one broke."
        data = json.dumps({"content": [{"type": "text", "text": text}],
                           "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def model():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Anthropic)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(port, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
    r = conn.getresponse()
    out = (r.status, json.loads(r.read() or b"null"))
    conn.close()
    return out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_shop_passes_its_tests(phase, tmp_path):
    shutil.copy(APP / "shop.nexo", tmp_path)
    proc = run(phase[1], ["test", "shop.nexo"], tmp_path, env={"SHOP_JWT_KEY": "k", "XDG_CONFIG_HOME": str(tmp_path)})
    out = proc.stdout.decode("utf-8")
    assert out.endswith("3 tests: 3 passed, 0 failed\n"), out
    assert proc.returncode == 0


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_shop_serves(phase, model, tmp_path):
    shutil.copy(APP / "shop.nexo", tmp_path)
    (tmp_path / "nexo.toml").write_text(f'[ai]\nprovider = "anthropic"\nurl = "{model}"\n')
    port = free_port()
    env = dict(os.environ, PYTHONPATH=str(ROOT), NEXO_WEB_LOCAL="1", PORT=str(port), SHOP_JWT_KEY="k",
               ANTHROPIC_KEY="a", XDG_CONFIG_HOME=str(tmp_path), NEXO_LEAKCHECK="1")
    proc = subprocess.Popen([*phase[1], "shop.nexo"], cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        assert proc.stderr.readline().decode() == f"shop listens on http://127.0.0.1:{port}\n"
        status, login = request(port, "POST", "/login?name=ana")
        token = login["token"]
        assert request(port, "POST", "/products", {"name": "Desk", "price": 120})[0] == 401
        assert request(port, "POST", "/products", {"name": "Desk", "price": 120}, token) == (
            201, {"id": 1, "name": "Desk", "price": 120})
        assert request(port, "POST", "/products", {"name": "Desk", "price": 99}, token) == (
            409, {"error": 'There is already a Product with name = "Desk"'})
        assert request(port, "POST", "/products/1/reviews", {"text": "Great desk"}) == (
            201, {"id": 1, "product": {"id": 1, "name": "Desk", "price": 120}, "text": "Great desk",
                  "tone": "positive", "score": 5})
        assert request(port, "POST", "/products/1/reviews", {"text": "A leg broke"})[1]["tone"] == "negative"
        assert request(port, "GET", "/products/1/summary") == (
            200, {"product": "Desk", "reviews": 2, "score": 3.5, "summary": "Mixed: some love it, one broke."})
        assert request(port, "GET", "/products/7") == (404, {"error": "There is no Product with id = 7"})
        assert request(port, "POST", "/products/7/reviews", {"text": "?"})[0] == 404
        assert request(port, "GET", "/products") == (200, [{"id": 1, "name": "Desk", "price": 120}])
    finally:
        proc.kill()
        proc.wait()
