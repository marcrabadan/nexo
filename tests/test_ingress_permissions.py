"""Frozen operator ingress grants, intersected with all existing ceilings."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from phases import run
from test_security_ingress import backend, raw, serving, SOURCE


def grant(tmp_path, permissions=(), **changes):
    doc = {"version": 1, "application": "main.nexo", "server": "api", "port": 0, "permissions": list(permissions) if isinstance(permissions, tuple) else permissions}
    doc.update(changes)
    (tmp_path / "ingress.json").write_text(json.dumps(doc))
    return ("--ingress", "ingress.json")


def test_allowed_file_read_uses_operator_and_root_grants(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    args = grant(tmp_path, [["allow", "read", "public.txt"]])
    source = SOURCE.replace('return "HELLO"', 'return read("public.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        response = raw(port)
        assert response.startswith(b"HTTP/1.1 200"), response
        assert response.endswith(b"PUBLIC")


def test_ingress_grant_cannot_widen_application_policy(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    args = grant(tmp_path, [["allow", "read", "public.txt"]])
    source = SOURCE.replace('allow read "."', 'deny read "."').replace('return "HELLO"', 'return read("public.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


def test_ungranted_file_is_not_authorized_by_root(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    args = grant(tmp_path, [["allow", "read", "public.txt"]])
    source = SOURCE.replace('return "HELLO"', 'return read("private.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        response = raw(port)
        assert response.startswith(b"HTTP/1.1 500")
        assert b"PRIVATE" not in response
        assert process.wait(timeout=2) == 1


@pytest.mark.parametrize("order", [False, True])
def test_explicit_deny_wins_regardless_of_rule_order(tmp_path, backend, order):
    (tmp_path / "public.txt").write_text("PUBLIC")
    permissions = [["allow", "read", "."], ["deny", "read", "public.txt"]]
    args = grant(tmp_path, permissions[::-1] if order else permissions)
    source = SOURCE.replace('return "HELLO"', 'return read("public.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


@pytest.mark.parametrize("allowed", [True, False])
def test_env_access_requires_both_grants(tmp_path, backend, allowed):
    args = grant(tmp_path, [["allow", "env", "INGRESS_TEST_VALUE"]] if allowed else [])
    source = SOURCE.replace('allow read "."', 'allow env "INGRESS_TEST_VALUE"').replace('return "HELLO"', 'tokens = jwt(key: secret("INGRESS_TEST_VALUE"))\n        return tokens.sign({"sub": "reader"})')
    with serving(tmp_path, backend, source, args=args, env={"INGRESS_TEST_VALUE": "VALUE"}) as (process, port, _):
        response = raw(port)
        if allowed:
            assert response.startswith(b"HTTP/1.1 200"), response
            assert len(response.split(b"\r\n\r\n", 1)[1].split(b".")) == 3
        else:
            assert response.startswith(b"HTTP/1.1 500"), response
            assert process.wait(timeout=2) == 1


def test_jwt_can_read_operator_granted_secret_env(tmp_path, backend):
    args = grant(tmp_path, [["allow", "env", "INGRESS_TEST_KEY"]])
    source = '''policy:
    allow env "INGRESS_TEST_KEY"
tokens = jwt(key: secret("INGRESS_TEST_KEY"))
print(tokens.sign({"sub": "ana"}))
server api:
    port = 0
    auth = tokens
    get "/"(request):
        return request.user["sub"]
'''
    with serving(tmp_path, backend, source, args=args, env={"INGRESS_TEST_KEY": "public-test-fixture-key"}) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 401")
        token = process.stdout.readline().strip()
        request = f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer ".encode() + token + b"\r\n\r\n"
        response = raw(port, request)
        assert response.startswith(b"HTTP/1.1 200"), response
        assert response.endswith(b"ana")


@pytest.mark.parametrize("allowed", [True, False])
def test_network_grant_permits_only_the_authorized_host(tmp_path, backend, allowed):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = b"UPSTREAM"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    upstream = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        args = grant(tmp_path, [["allow", "net", "127.0.0.1" if allowed else "localhost"]])
        source = SOURCE.replace('allow read "."', 'allow net "127.0.0.1"').replace('return "HELLO"', f'response = http.get("http://127.0.0.1:{upstream.server_port}/data")?\n        return response.text')
        with serving(tmp_path, backend, source, args=args) as (process, port, _):
            response = raw(port)
            if allowed:
                assert response.startswith(b"HTTP/1.1 200"), response
                assert response.endswith(b"UPSTREAM")
                assert seen == ["/data"]
            else:
                assert response.startswith(b"HTTP/1.1 500"), response
                assert process.wait(timeout=2) == 1
                assert not seen
    finally:
        upstream.shutdown()
        thread.join(timeout=2)
        upstream.server_close()


def test_dependency_ceiling_is_not_replaced_by_ingress_grant(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "helper.nexo").write_text('pub fn register(callback: any):\n    callback()\n')
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {"helper.nexo": []}}))
    args = (*grant(tmp_path, [["allow", "read", "public.txt"]]), "--dependencies", "permissions.json")
    source = '''policy:
    allow read "."
use helper
api = __web_server("api", port: 0)
fn handler():
    return read("public.txt")?
fn register():
    __web_route(api, "GET", "/", handler, [])
helper.register(register)
'''
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


def test_manifest_is_not_reloaded_after_application_start(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    args = grant(tmp_path, [["allow", "read", "public.txt"]])
    source = SOURCE.replace('return "HELLO"', 'return read("private.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        grant(tmp_path, [["allow", "read", "."]])
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


def test_read_grant_does_not_follow_retargeted_symlink(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    link = tmp_path / "selected"
    link.symlink_to("public.txt")
    args = grant(tmp_path, [["allow", "read", "selected"]])
    source = SOURCE.replace('return "HELLO"', 'return read("selected")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        link.unlink()
        link.symlink_to("private.txt")
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


def test_read_root_replacement_does_not_widen_frozen_directory(tmp_path, backend):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "value.txt").write_text("PUBLIC")
    private = tmp_path / "private"
    private.mkdir()
    (private / "value.txt").write_text("PRIVATE")
    args = grant(tmp_path, [["allow", "read", "data"]])
    source = SOURCE.replace('return "HELLO"', 'return read("data/value.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        folder.rename(tmp_path / "old-data")
        folder.symlink_to("private", target_is_directory=True)
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"extra": 1}, {"permissions": {}},
    {"server": "x" * 257}, {"permissions": [["allow", "env", "VALUE"]] * 129},
    {"permissions": [["allow", "env", *(["VALUE"] * 65)]]},
    {"application": "missing.nexo"}, {"port": 1}, {"port": True},
    {"server": ""}, {"server": "x\u0000"}, {"permissions": [["allow", "write", "."]]},
    {"permissions": [["allow", "shell"]]}, {"permissions": [["allow", "c", "libm"]]},
    {"permissions": [["allow", "python", "os"]]}, {"permissions": [["allow", "read"]]},
    {"permissions": [["allow", "env", "*"]]}, {"permissions": [["allow", "env", "BAD=NAME"]]},
    {"permissions": [["allow", "net", "https://example.com/"]]}, {"permissions": [["allow", "net", "*"]]},
    {"permissions": [["unknown", "read", "."]]}, {"permissions": [["allow", "read", 1]]},
])
def test_invalid_manifest_fails_before_startup(tmp_path, backend, changes):
    args = grant(tmp_path, **changes)
    (tmp_path / "main.nexo").write_text(SOURCE + 'print("AFTER")\n')
    result = run(backend, ["security", "run", "--web=loopback:0", *args, "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"Ingress permission configuration" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert b"Shield loopback http" not in result.stdout


@pytest.mark.parametrize("kind", ["duplicate", "oversize", "deep"])
def test_ambiguous_or_unbounded_json_is_rejected(tmp_path, backend, kind):
    grant(tmp_path)
    text = (tmp_path / "ingress.json").read_text()
    if kind == "duplicate":
        text = text.replace('"version": 1', '"version": 1, "version": 1')
    elif kind == "oversize":
        text += " " * 65536
    else:
        text = "[" * 10000 + "0" + "]" * 10000
    (tmp_path / "ingress.json").write_text(text)
    (tmp_path / "main.nexo").write_text(SOURCE + 'print("AFTER")\n')
    result = run(backend, ["security", "run", "--web=loopback:0", "--ingress", "ingress.json", "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"Ingress permission configuration" in result.stdout
    assert b"AFTER\n" not in result.stdout


def test_server_identity_must_match_before_bind(tmp_path, backend):
    args = grant(tmp_path, server="other")
    (tmp_path / "main.nexo").write_text(SOURCE)
    result = run(backend, ["security", "run", "--web=loopback:0", *args, "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"Ingress permission configuration" in result.stdout
    assert b"Shield loopback http" not in result.stdout


@pytest.mark.parametrize("args", [
    ["--ingress", "ingress.json"], ["--web=memory", "--ingress", "ingress.json"],
    ["--web=loopback:0", "--ingress"],
    ["--web=loopback:0", "--ingress", "ingress.json", "--ingress", "ingress.json"],
])
def test_ingress_option_must_be_unambiguous_and_loopback_only(tmp_path, backend, args):
    grant(tmp_path)
    (tmp_path / "main.nexo").write_text(SOURCE + 'print("AFTER")\n')
    result = run(backend, ["security", "run", *args, "main.nexo"], tmp_path)
    assert result.returncode == 2, result.stdout.decode()
    assert b"AFTER\n" not in result.stdout



def test_startup_cannot_replace_the_snapshot_by_editing_manifest(tmp_path, backend):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    args = grant(tmp_path, [["allow", "read", "public.txt"]])
    wide = {"version": 1, "application": "main.nexo", "server": "api", "port": 0,
            "permissions": [["allow", "read", "."]]}
    (tmp_path / "wide.json").write_text(json.dumps(wide))
    startup = '''try:
    write("ingress.json", read("wide.json"))
else e:
    pass
'''
    source = SOURCE.replace('server api:', startup + 'server api:').replace('return "HELLO"', 'return read("private.txt")?')
    with serving(tmp_path, backend, source, args=args) as (process, port, _):
        assert json.loads((tmp_path / "ingress.json").read_text()) == wide
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


def test_inherited_snapshot_is_not_authority_without_cli_manifest(tmp_path, backend):
    (tmp_path / "private.txt").write_text("PRIVATE")
    fake = {"version": 1, "application": str(tmp_path / "main.nexo"), "server": "api", "port": 0,
            "permissions": [["allow", "read", str(tmp_path)]]}
    source = SOURCE.replace('return "HELLO"', 'return read("private.txt")?')
    with serving(tmp_path, backend, source, env={"NEXO_SECURITY_INGRESS_SNAPSHOT": json.dumps(fake)}) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1


def test_manifest_and_read_targets_resolve_from_manifest_directory(tmp_path, backend):
    control = tmp_path / "operator"
    control.mkdir()
    (control / "public.txt").write_text("PUBLIC")
    doc = {"version": 1, "application": "../main.nexo", "server": "api", "port": 0,
           "permissions": [["allow", "read", "public.txt"]]}
    (control / "ingress.json").write_text(json.dumps(doc))
    source = SOURCE.replace('return "HELLO"', 'return read("operator/public.txt")?')
    with serving(tmp_path, backend, source, args=("--ingress", "operator/ingress.json")) as (process, port, _):
        assert raw(port).endswith(b"PUBLIC")


def test_grants_cannot_be_used_with_another_existing_application(tmp_path, backend):
    (tmp_path / "other.nexo").write_text(SOURCE)
    args = grant(tmp_path, application="other.nexo")
    (tmp_path / "main.nexo").write_text(SOURCE + 'print("AFTER")\n')
    result = run(backend, ["security", "run", "--web=loopback:0", *args, "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"Ingress permission configuration" in result.stdout
    assert b"AFTER\n" not in result.stdout
