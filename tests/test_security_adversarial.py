"""Reproducible hostile-input checks, against local runtime boundaries only."""
import json
import pytest
from phases import run
from test_security_ingress import backend, raw, serving, SOURCE


@pytest.mark.parametrize("depth", [129, 12000])
def test_deep_json_request_is_rejected_without_killing_server(tmp_path, backend, depth):
    source = SOURCE.replace('get "/"():', 'post "/"(request):').replace('return "HELLO"', 'return request.json()?') + '    get "/health"():\n        return "ALIVE"\n'
    body = b'[' * depth + b'0' + b']' * depth
    with serving(tmp_path, backend, source) as (process, port, _):
        request = f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        response = raw(port, request)
        assert response.startswith(b"HTTP/1.1 400"), response[:200]
        response = raw(port, f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert response.endswith(b"ALIVE"), response


def test_deep_dependency_manifest_fails_without_panic(tmp_path, backend):
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nprint("PAYLOAD")\n')
    (tmp_path / "permissions.json").write_text('[' * 12000 + '0' + ']' * 12000)
    result = run(backend, ["security", "run", "--dependencies", "permissions.json", "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout[-500:]
    assert b"PAYLOAD" not in result.stdout
    assert b"stack overflow" not in result.stdout and b"panicked" not in result.stdout


@pytest.mark.parametrize("depth", [128, 129, 12000])
def test_memory_dispatch_has_same_json_depth_boundary(tmp_path, backend, depth):
    body = json.dumps('[' * depth + '0' + ']' * depth)
    (tmp_path / "main.nexo").write_text('''policy:
    allow read "body.json"
server api:
    post "/"(request):
        value = request.json()?
        return "OK"
r = api.dispatch("POST", "/", body: BODY)
print(r["status"])
'''.replace("BODY", body))
    result = run(backend, ["security", "run", "--web=memory", "main.nexo"], tmp_path)
    assert result.returncode == 0, result.stdout[-500:]
    assert result.stdout.strip() == (b"201" if depth == 128 else b"400"), result.stdout


@pytest.mark.parametrize("depth", [129, 12000])
def test_signed_deep_jwt_is_rejected_in_both_runtimes(tmp_path, backend, depth):
    import base64
    import hashlib
    import hmac
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    header = encode(b'{"alg":"HS256"}')
    payload = encode(b'{"sub":"ana","meta":' + b'[' * depth + b'0' + b']' * depth + b'}')
    signed = header + b'.' + payload
    token = (signed + b'.' + encode(hmac.new(b'fixture-key', signed, hashlib.sha256).digest())).decode()
    source = '''policy:
    allow env "ADVERSARIAL_JWT_KEY"
tokens = jwt(key: secret("ADVERSARIAL_JWT_KEY"))
server api:
    auth = tokens
    get "/"():
        return "SHOULD_NOT_RUN"
r = api.dispatch("GET", "/", headers: {"authorization": TOKEN})
print(r["status"])
'''.replace("TOKEN", json.dumps("Bearer " + token))
    (tmp_path / "main.nexo").write_text(source)
    result = run(backend, ["security", "run", "--web=memory", "main.nexo"], tmp_path, env={"ADVERSARIAL_JWT_KEY": "fixture-key"})
    assert result.returncode == 0, result.stdout[-500:]
    assert result.stdout.strip() == b"401", result.stdout
