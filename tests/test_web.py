"""nexo.web served by `nexo file.nexo` (D102-D104), in both phases: the
same requests get the same answers."""
import http.client
import json
import os
import socket
import subprocess

import pytest

from phases import ROOT, commands

PROGRAM = """use nexo.db

db = connect("users.db")
tokens = jwt(key: secret("JWT_KEY"))

@table
type User:
    id: id
    name: str
    email: str @unique

type NewUser:
    name: str
    email: str

server api:
    port = 8080
    auth = tokens

    @public
    post "/login"(name: str):
        return {"token": tokens.sign({"sub": name, "role": "admin"}, expires: 1h)}

    @public
    get "/users/{id}"(id: int):
        return db.User.find(id: id)?

    @public
    get "/users"(limit: int = 10):
        return db.User.all() |> take limit

    post "/users"(user: NewUser):
        return db.User.add(name: user.name, email: user.email)

    @role("admin")
    @limit(2 per minute per ip)
    delete "/users/{id}"(id: int):
        db.User.remove(id)
        return 204

    get "/me"(request):
        return request.user["sub"]

    @public
    get "/broken"():
        return 1000
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(prefix, tmp_path, port):
    (tmp_path / "api.nexo").write_text(PROGRAM, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", NEXO_WEB_LOCAL="1",
               PORT=str(port), JWT_KEY="s3cr3t")
    proc = subprocess.Popen([*prefix, "api.nexo"], cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    line = proc.stderr.readline().decode("utf-8")
    assert line == f"api escucha en http://127.0.0.1:{port}\n", line + proc.stderr.read().decode()
    return proc


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    out = (r.status, dict(r.getheaders()), r.read().decode("utf-8"))
    conn.close()
    return out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_web_api(phase, tmp_path):
    port = free_port()
    proc = start(phase[1], tmp_path, port)
    try:
        status, headers, body = request(port, "GET", "/users/1")
        assert (status, body) == (404, '{"error":"No hay ningún User con id = 1"}')
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Connection"] == "close"
        assert request(port, "POST", "/users", '{"name":"Ana","email":"a@x"}')[0] == 401
        token = json.loads(request(port, "POST", "/login?name=ana")[2])["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert request(port, "POST", "/users", '{"name":"Ana","email":"a@x"}', auth)[::2] == (
            201, '{"id":1,"name":"Ana","email":"a@x"}')
        assert request(port, "POST", "/users", '{"name":"Ana","email":"a@x"}', auth)[::2] == (
            409, '{"error":"Ya hay un User con email = \\"a@x\\""}')
        assert request(port, "POST", "/users", '{"name":"Ana"}', auth)[::2] == (
            400, '{"error":"Datos no válidos: falta el campo \\"email\\" de NewUser"}')
        assert request(port, "POST", "/users", None, auth)[::2] == (
            400, '{"error":"falta el cuerpo JSON de la petición"}')
        assert request(port, "GET", "/users/abc")[::2] == (400, '{"error":"\\"abc\\" no es un valor int válido"}')
        assert request(port, "GET", "/users?limit=1")[::2] == (200, '[{"id":1,"name":"Ana","email":"a@x"}]')
        status, headers, body = request(port, "GET", "/me", headers=auth)
        assert (status, headers["Content-Type"], body) == (200, "text/plain; charset=utf-8", "ana")
        assert request(port, "GET", "/me", headers={"Authorization": "Bearer x.y.z"})[0] == 401
        assert request(port, "DELETE", "/users/1", headers=auth)[0] == 204
        assert request(port, "DELETE", "/users/1", headers=auth)[0] == 204
        status, headers, _ = request(port, "DELETE", "/users/1", headers=auth)
        assert status == 429 and 1 <= int(headers["Retry-After"]) <= 60
        status, headers, _ = request(port, "PUT", "/users/1")
        assert (status, headers["Allow"]) == (405, "GET, DELETE")
        assert request(port, "GET", "/nothing")[::2] == (404, '{"error":"Not Found"}')
        assert request(port, "POST", "/users", "{}", {**auth, "Origin": "http://evil.com"})[::2] == (
            403, '{"error":"la petición viene de otro origen"}')
        status, headers, _ = request(port, "GET", "/users/1", headers={"X-Forwarded-Proto": "http"})
        assert (status, headers["Location"]) == (308, f"https://127.0.0.1:{port}/users/1")
        with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
            s.sendall(b"BAD\r\n\r\n")
            assert s.recv(100).startswith(b"HTTP/1.1 400 Bad Request\r\n")
        # a mistake in a handler: 500 for the client, and the program stops
        # with the diagnostic
        assert request(port, "GET", "/broken")[0] == 500
        assert proc.wait(timeout=20) == 1
        assert proc.stderr.read().decode("utf-8") == (
            "Error en api.nexo, línea 45:\n"
            '  get "/broken"():\n'
            "  ^^^\n"
            "1000 no es un código de estado HTTP. Devuelve un número entre 100 y 599 (204, 404...), "
            "o el valor que se envía como JSON.\n")
    finally:
        proc.kill()
        proc.wait()
