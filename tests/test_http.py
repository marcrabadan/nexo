"""The http client (D98) against a local server, in both phases."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import commands, run

PROGRAM = """r = http.get("{base}/rate")
print(r, r.status, r.text, r.headers["content-type"])
print(r.json()["usd"])
type Rate:
    usd: decimal
    day: date
print(r.json(as: Rate))
print(http.get("{base}/old").json()["day"])
p = http.post("{base}/echo", {{"name": "Ana", "n": 2}})
print(p.status, p.json())
print(http.post("{base}/echo", "plain").json()["type"])
token = secret("NEXO_TEST_TOKEN")
print(http.get("{base}/auth", headers: {{"Authorization": token}}).json())
print(http.get("{base}/hop", headers: {{"Authorization": token}}).json())
print(http.get("{base}/away", headers: {{"Authorization": token, "Cookie": "s=1", "X-Trace": "7"}}).json())
print(http.get("{base}/flaky").text)
try:
    http.get("{base}/missing")
else e is HTTPError:
    print(e.message)
try:
    http.post("{base}/fail", "x")
else e is HTTPError:
    print(e.message)
try:
    http.get("http://127.0.0.1:{closed}/x", retries: 0)
else e is NetworkError:
    print(e.message)
try:
    http.get("ftp://x")
else e:
    print(e.kind, e.message)
print(validate("ana@mail.com", as: email))
try:
    validate("ana@mail", as: email)
else e:
    print(e.message)
"""

EXPECTED = """response(200) 200 {{"usd": 1.08, "day": "2026-10-24"}} application/json
1.08
Rate(usd: 1.08, day: 2026-10-24)
2026-10-24
201 {{"got": "{{\\"name\\":\\"Ana\\",\\"n\\":2}}", "type": "application/json"}}
text/plain; charset=utf-8
{{"auth": "Bearer t0k3n"}}
{{"auth": "Bearer t0k3n", "cookie": none, "trace": none}}
{{"auth": none, "cookie": none, "trace": "7"}}
ok at last
GET {base}/missing respondió 404 Not Found
POST {base}/fail respondió 503 Service Unavailable
No se puede conectar con http://127.0.0.1:{closed}/x: rechaza la conexión
NetworkError No se puede conectar con ftp://x: la dirección debe empezar por http:// o https://
ana@mail.com
"ana@mail" no es un valor email válido
"""


class Handler(BaseHTTPRequestHandler):
    flaky = {"n": 0}

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json", extra=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/rate":
            self.reply(200, '{"usd": 1.08, "day": "2026-10-24"}')
        elif self.path == "/old":
            self.reply(301, "", extra=[("Location", "/rate")])
        elif self.path == "/auth":
            self.reply(200, json.dumps({"auth": self.headers.get("Authorization")}))
        elif self.path in ("/hop", "/away"):
            # the same origin, or another one (localhost is not 127.0.0.1)
            host = "127.0.0.1" if self.path == "/hop" else "localhost"
            self.reply(302, "", extra=[("Location", f"http://{host}:{self.server.server_address[1]}/auth2")])
        elif self.path == "/auth2":
            self.reply(200, json.dumps({"auth": self.headers.get("Authorization"),
                                        "cookie": self.headers.get("Cookie"),
                                        "trace": self.headers.get("X-Trace")}))
        elif self.path == "/flaky":
            # fails twice, then answers: the default retries of a GET
            Handler.flaky["n"] += 1
            if Handler.flaky["n"] % 3:
                self.reply(503, "busy", "text/plain")
            else:
                self.reply(200, "ok at last", "text/plain")
        else:
            self.reply(404, '{"error": "no"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode()
        if self.path == "/fail":
            self.reply(503, "down", "text/plain")
            return
        self.reply(201, json.dumps({"got": body, "type": self.headers.get("Content-Type")}))


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_http_client(phase, server, tmp_path):
    closed = closed_port()
    (tmp_path / "client.nexo").write_text(PROGRAM.format(base=server, closed=closed), encoding="utf-8")
    proc = run(phase[1], ["client.nexo"], tmp_path, env={"NEXO_TEST_TOKEN": "Bearer t0k3n"})
    assert proc.stdout.decode("utf-8") == EXPECTED.format(base=server, closed=closed)
    assert proc.returncode == 0
