"""Explicit native loopback ingress, bounded IO/lifetime, no ambient grants."""
import os
import re
import selectors
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from phases import commands, compiler, run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    if not sys_linux():
        pytest.skip("Linux parent-death supervision required")
    return request.param[1]


def sys_linux():
    import sys
    return sys.platform == "linux"


SOURCE = '''policy:
    allow read "."
    allow write "."
server api:
    port = 0
    get "/"():
        return "HELLO"
'''


@contextmanager
def serving(tmp_path, backend, source=SOURCE, *, args=(), env=None):
    (tmp_path / "main.nexo").write_text(source)
    scratch = tmp_path / "compiler-tmp"
    scratch.mkdir(exist_ok=True)
    settings = dict(os.environ, PYTHONPATH=str(ROOT), NEXO_COMPILER=str(compiler()), TMPDIR=str(scratch))
    if env:
        settings.update(env)
    process = subprocess.Popen([*backend, "security", "run", "--web=loopback:0", *args, "main.nexo"],
                               cwd=tmp_path, env=settings, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    prefix = bytearray()
    try:
        poll = selectors.DefaultSelector()
        poll.register(process.stderr, selectors.EVENT_READ)
        until = time.monotonic() + 15
        port = None
        while time.monotonic() < until:
            if poll.select(0.1):
                line = process.stderr.readline()
                prefix.extend(line)
                match = re.search(rb"Shield loopback http://127\.0\.0\.1:(\d+)/", line)
                if match:
                    port = int(match.group(1))
                    break
            if process.poll() is not None:
                break
        poll.close()
        assert port is not None, bytes(prefix).decode(errors="replace") + process.communicate(timeout=2)[1].decode(errors="replace")
        # The child can report readiness before its parent resumes after spawn.
        cleanup_until = time.monotonic() + 1
        while list(scratch.glob("nexo-*")) and time.monotonic() < cleanup_until:
            time.sleep(0.01)
        assert not list(scratch.glob("nexo-*")), "compiled payload artifacts must be removed promptly after exec"
        yield process, port, prefix
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        shutil.rmtree(scratch)


def raw(port, data=None):
    data = data if data is not None else f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.settimeout(8)
        connection.sendall(data)
        chunks = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def test_loopback_serves_eight_requests_and_closes(tmp_path, backend):
    with serving(tmp_path, backend, env={"PORT": "65534", "NEXO_WEB_LOCAL": ""}) as (process, port, _):
        for _ in range(8):
            response = raw(port)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n"), response
            assert response.endswith(b"HELLO")
            assert b"Connection: close\r\n" in response
        assert process.wait(timeout=3) == 0
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)


def test_remote_request_cannot_use_root_file_grants(tmp_path, backend):
    (tmp_path / "private.txt").write_text("PRIVATE")
    source = SOURCE.replace('return "HELLO"', 'return read("private.txt")?')
    with serving(tmp_path, backend, source) as (process, port, _):
        response = raw(port)
        assert response.startswith(b"HTTP/1.1 500"), response
        assert b"PRIVATE" not in response
        assert process.wait(timeout=2) == 1
        assert b"active dependency context" in process.stderr.read()


def test_remote_request_cannot_write_or_catch_security_failure(tmp_path, backend):
    source = SOURCE.replace('return "HELLO"', 'try:\n            write("marker", "BAD")\n        else e:\n            return "CAUGHT"\n        return "HELLO"')
    with serving(tmp_path, backend, source) as (process, port, _):
        response = raw(port)
        assert b"CAUGHT" not in response
        assert process.wait(timeout=2) == 1
        assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("raw_request,status", [
    (b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n", 403),
    (b"GET / HTTP/1.1\r\nHOST\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nOrigin: https://evil.example\r\n\r\n", 403),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nSec-Fetch-Site: cross-site\r\n\r\n", 403),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nX-Forwarded-Proto: https\r\n\r\n", 403),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nHost: PORT\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nTransfer-Encoding: chunked\r\n\r\n", 501),
    (b"GET / HTTP/1.1\r\nHost: PORT\r\nBad Name: value\r\n\r\n", 400),
    (b"GET / HTTP/1.9\r\nHost: PORT\r\n\r\n", 400),
    (b"GET https://example.com/ HTTP/1.1\r\nHost: PORT\r\n\r\n", 400),
])
def test_strict_parser_and_local_origin_checks(tmp_path, backend, raw_request, status):
    with serving(tmp_path, backend) as (process, port, _):
        request = raw_request.replace(b"PORT", f"127.0.0.1:{port}".encode())
        assert raw(port, request).startswith(f"HTTP/1.1 {status}".encode())
        assert raw(port).endswith(b"HELLO")


def test_idle_listener_closes_without_input(tmp_path, backend):
    with serving(tmp_path, backend) as (process, port, _):
        assert process.wait(timeout=5) == 0
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)


def test_stalled_client_does_not_reset_read_deadline(tmp_path, backend):
    with serving(tmp_path, backend) as (process, port, _):
        with socket.create_connection(("127.0.0.1", port), timeout=1) as slow:
            slow.sendall(b"GET / HTTP/1.1\r\n")
            slow.settimeout(2)
            assert slow.recv(1) == b""
        assert raw(port).endswith(b"HELLO")


def test_busy_handler_is_killed_by_owning_launcher(tmp_path, backend):
    source = SOURCE.replace('return "HELLO"', 'while true:\n            pass')
    with serving(tmp_path, backend, source) as (process, port, _):
        assert raw(port) == b""
        assert process.wait(timeout=2) == 124
        assert b"6-second lifetime" in process.stderr.read()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)


def test_killing_launcher_closes_owned_listener(tmp_path, backend):
    with serving(tmp_path, backend) as (process, port, _):
        assert raw(port).endswith(b"HELLO")
        process.kill()
        process.wait(timeout=2)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    pass
            except OSError:
                break
        else:
            pytest.fail("listener survived its owning launcher")


@pytest.mark.parametrize("args,code", [
    (("--web=loopback:-1",), 2), (("--web=loopback:65536",), 2),
    (("--web=loopback:bad",), 2), (("--web=loopback:0", "--web=memory"), 2),
    (("--web=loopback:0", "--sandbox=linux"), 125),
])
def test_bad_or_incompatible_grants_fail_before_execution(tmp_path, backend, args, code):
    (tmp_path / "main.nexo").write_text(SOURCE + 'print("AFTER")\n')
    result = run(backend, ["security", "run", *args, "main.nexo"], tmp_path)
    assert result.returncode == code, result.stdout.decode()
    assert b"AFTER\n" not in result.stdout


def test_port_mismatch_and_multiple_servers_do_not_bind(tmp_path, backend):
    for source in (SOURCE.replace('port = 0', 'port = 1'), SOURCE + 'server second:\n    port = 0\n    get "/"():\n        return "second"\n'):
        (tmp_path / "main.nexo").write_text(source)
        result = run(backend, ["security", "run", "--web=loopback:0", "main.nexo"], tmp_path)
        assert result.returncode == 1, result.stdout.decode()
        assert b"listener grant" in result.stdout
        assert b"Shield loopback http" not in result.stdout


@pytest.mark.parametrize("grant", ["shell", 'c "libm"', 'python "os"'])
def test_native_capability_grants_rejected_before_startup(tmp_path, backend, grant):
    (tmp_path / "main.nexo").write_text(SOURCE.replace('allow read "."', 'allow ' + grant) + 'print("AFTER")\n')
    result = run(backend, ["security", "run", "--web=loopback:0", "main.nexo"], tmp_path)
    assert result.returncode == 125, result.stdout.decode()
    assert b"AFTER\n" not in result.stdout



def test_trickled_headers_do_not_extend_absolute_deadline(tmp_path, backend):
    with serving(tmp_path, backend) as (process, port, _):
        with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
            start = time.monotonic()
            connection.sendall(b"GET / HTTP/1.1\r\nX")
            time.sleep(0.6)
            connection.sendall(b"a")
            connection.settimeout(2)
            assert connection.recv(1) == b""
            assert time.monotonic() - start < 1.25
        assert raw(port).endswith(b"HELLO")


def test_invalid_connections_consume_the_same_eight_connection_budget(tmp_path, backend):
    with serving(tmp_path, backend) as (process, port, _):
        for _ in range(8):
            assert raw(port, b"GET / HTTP/1.9\r\n\r\n").startswith(b"HTTP/1.1 400")
        assert process.wait(timeout=2) == 0


def test_typed_record_defaults_receive_the_empty_ingress_ceiling(tmp_path, backend):
    source = '''policy:
    allow write "."
fn stamp() -> str:
    try:
        write("marker", "BAD")
    else e:
        pass
    return "ok"
type Payload:
    value: str = stamp()
server api:
    port = 0
    post "/"(payload: Payload):
        return payload.value
'''
    with serving(tmp_path, backend, source) as (process, port, _):
        request = f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
        assert raw(port, request).startswith(b"HTTP/1.1 500")
        assert process.wait(timeout=2) == 1
        assert not (tmp_path / "marker").exists()


def test_jwt_with_initialized_key_authenticates_pure_handlers(tmp_path, backend):
    source = '''policy:
    deny shell
tokens = jwt(key: password.generate(32))
print(tokens.sign({"sub": "ana", "role": "admin"}))
server api:
    port = 0
    auth = tokens
    @role("admin")
    get "/"(request):
        return request.user["sub"]
'''
    with serving(tmp_path, backend, source) as (process, port, _):
        assert raw(port).startswith(b"HTTP/1.1 401")
        token = process.stdout.readline().strip()
        request = f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer ".encode() + token + b"\r\n\r\n"
        response = raw(port, request)
        assert response.startswith(b"HTTP/1.1 200"), response
        assert response.endswith(b"ana")
