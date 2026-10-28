"""Operator route ceilings use selected static templates before auth and handlers."""
import pytest
from test_ingress_permissions import grant
from test_security_ingress import backend, raw, serving, SOURCE
from phases import run


def routes(tmp_path, entries, permissions=None):
    return grant(tmp_path, permissions or [["allow", "read", "."]], version=2, routes=entries)


def entry(path="/", permissions=None, method="GET"):
    return {"method": method, "path": path, "permissions": permissions or []}


@pytest.mark.parametrize("case", ["allow", "missing", "empty", "method", "server", "deny"])
def test_route_and_server_limits_intersect(tmp_path, backend, case):
    (tmp_path / "public.txt").write_text("PUBLIC")
    permissions = [["allow", "read", "public.txt"]]
    if case == "deny":
        permissions.append(["deny", "read", "public.txt"])
    entries = [] if case == "missing" else [entry(permissions=[] if case == "empty" else permissions, method="POST" if case == "method" else "GET")]
    args = routes(tmp_path, entries, [["allow", "env", "UNRELATED"]] if case == "server" else None)
    source = SOURCE.replace('return "HELLO"', 'return read("public.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        response = raw(port)
        if case == "allow":
            assert response.startswith(b"HTTP/1.1 200"), response
            assert response.endswith(b"PUBLIC")
        else:
            assert response.startswith(b"HTTP/1.1 500"), response
            assert b"PUBLIC" not in response
            assert process.wait(timeout=2) == 1


@pytest.mark.parametrize("template", [True, False])
def test_template_identity_ignores_query_and_actual_parameter(tmp_path, backend, template):
    (tmp_path / "public.txt").write_text("PUBLIC")
    args = routes(tmp_path, [entry("/items/{id}" if template else "/items/42", [["allow", "read", "public.txt"]])])
    source = SOURCE.replace('get "/"():', 'get "/items/{id}"(id):').replace('return "HELLO"', 'return read("public.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        request = f"GET /items/42?route=/ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        response = raw(port, request)
        assert response.startswith(b"HTTP/1.1 200" if template else b"HTTP/1.1 500"), response


@pytest.mark.parametrize("entries", [None, {}, [entry(), entry()], [dict(entry(), extra=True)], [entry(method="get")], [entry(path="relative")], [entry(path="/?a")], [entry(permissions=[["allow", "write", "."]])], [entry()] * 129])
def test_invalid_route_config_fails_before_payload(tmp_path, backend, entries):
    args = routes(tmp_path, entries)
    (tmp_path / "main.nexo").write_text(SOURCE)
    result = run(backend, ["security", "run", "--web=loopback:0", *args, "main.nexo"], tmp_path)
    assert result.returncode != 0
    assert b"Shield loopback" not in result.stdout


def test_route_permissions_do_not_leak_to_next_request(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    args = routes(tmp_path, [entry("/allowed", [["allow", "read", "public.txt"]])])
    source = SOURCE.replace('get "/"():', 'get "/allowed"():').replace('return "HELLO"', 'return read("public.txt")?') + '    get "/other"():\n        return read("public.txt")?\n'
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        for path, status in [("/allowed", 200), ("/other", 500)]:
            response = raw(port, f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            assert response.startswith(f"HTTP/1.1 {status}".encode()), response
        assert process.wait(timeout=2) == 1


@pytest.mark.parametrize("allowed", [True, False])
def test_route_ceiling_applies_before_jwt_secret_lookup(tmp_path, backend, allowed):
    args = routes(tmp_path, [entry(permissions=[["allow", "env", "ROUTE_JWT_KEY"]] if allowed else [])], [["allow", "env", "ROUTE_JWT_KEY"]])
    source = '''policy:
    allow env "ROUTE_JWT_KEY"
tokens = jwt(key: secret("ROUTE_JWT_KEY"))
print(tokens.sign({"sub": "ana"}))
server api:
    port = 0
    auth = tokens
    get "/"(request):
        return request.user["sub"]
'''
    with serving(tmp_path, backend, source, args=args, env={"ROUTE_JWT_KEY": "fixture-key"}) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 401")
        token = process.stdout.readline().strip()
        response = raw(port, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer ".encode() + token + b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200" if allowed else b"HTTP/1.1 500"), response
        if not allowed:
            assert process.wait(timeout=2) == 1
