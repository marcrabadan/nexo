"""Online broker IPC and CLI contract. Tests make no external network calls."""
import json
import os
import platform
from pathlib import Path
import socket
import struct
import subprocess

import pytest

from phases import commands, compiler, run


@pytest.fixture(scope="module")
def worker():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        pytest.skip("online profile requires Linux x86_64")
    result = Path(compiler()).with_name("nexo-shield-worker") if compiler() else None
    if result is None:
        pytest.skip("requires native compiler")
    assert result.is_file()
    return result


def send(channel, data):
    channel.sendall(struct.pack("!I", len(data)) + data)


def receive(channel):
    def exact(size):
        data = b""
        while len(data) < size:
            chunk = channel.recv(size - len(data))
            assert chunk, "broker closed an incomplete frame"
            data += chunk
        return data
    size = struct.unpack("!I", exact(4))[0]
    assert size <= 8 * 1024 * 1024
    return exact(size)


def test_broker_checks_root_ceiling_and_private_ips_on_real_ipc(worker):
    parent, child = socket.socketpair()
    parent.settimeout(5)
    # Clean helper environment is the production contract, not a network-test
    # override. Every request below is rejected before any external connection.
    proc = subprocess.Popen([str(worker), "__broker", "--allow-host", "127.0.0.1",
                             "--deny-host", "blocked.example.com"], stdin=child,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"LANG": "C.UTF-8"})
    child.close()
    try:
        for url in ("http://127.0.0.1/", "http://blocked.example.com/", "https://unlisted.example.com/"):
            send(parent, json.dumps({"method": "GET", "url": url, "body": False,
                                    "headers": [], "timeout_ms": 100}).encode())
            send(parent, b"")
            assert json.loads(receive(parent)) == {"error": True}
            assert receive(parent) == b""
        send(parent, b"[" * 10000)
        send(parent, b"")
        assert json.loads(receive(parent)) == {"error": True}
        assert receive(parent) == b""
        # Oversized frames terminate the channel without allocating their body.
        parent.sendall(struct.pack("!I", 1024 * 1024 + 1))
        assert proc.wait(timeout=5) == 125
        assert not proc.stdout.read()
    finally:
        parent.close()
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


@pytest.mark.parametrize("backend", commands(), ids=lambda item: item[0])
@pytest.mark.parametrize("proxy", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"])
def test_online_proxy_configuration_fails_before_program_runs(tmp_path, backend, proxy, worker):
    (tmp_path / "main.nexo").write_text('policy:\n    allow net "example.com"\nprint("EXECUTED")\n')
    proc = run(backend[1], ["security", "run", "--sandbox=linux-online", "main.nexo"], tmp_path,
               env={proxy: "http://127.0.0.1:1", "NO_PROXY": "*"})
    assert proc.returncode == 125
    assert b"does not support configured proxies" in proc.stdout
    assert b"EXECUTED" not in proc.stdout


@pytest.mark.parametrize("backend", commands(), ids=lambda item: item[0])
def test_online_probe_reports_proxy_requirement(tmp_path, backend, worker):
    proc = run(backend[1], ["security", "probe", "--sandbox=linux-online"], tmp_path,
               env={"ALL_PROXY": "http://127.0.0.1:1"})
    assert proc.returncode == 0, proc.stdout.decode()
    result = json.loads(proc.stdout)
    assert result["profile"] == "linux-online-v1"
    assert result["supported"] is False
    assert result["proxies_unconfigured"] is False


@pytest.mark.parametrize("backend", commands(), ids=lambda item: item[0])
def test_conflicting_sandbox_profiles_are_rejected(tmp_path, backend, worker):
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nprint("EXECUTED")\n')
    proc = run(backend[1], ["security", "run", "--sandbox=linux", "--sandbox=linux-online", "main.nexo"], tmp_path)
    assert proc.returncode == 2
    assert b"Choose exactly one" in proc.stdout
    assert b"EXECUTED" not in proc.stdout


@pytest.mark.parametrize("allowed", [True, False], ids=["allowed-redirect", "denied-redirect"])
def test_compiled_runtime_uses_broker_and_strips_cross_origin_credentials(worker, tmp_path, allowed):
    """Exercise the runtime adapter over real IPC, independently of kernel support."""
    import threading
    source = tmp_path / "main.nexo"
    grants = '"example.com", "other.example.com"' if allowed else '"example.com"'
    source.write_text(f'policy:\n    allow net {grants}\n'
                      'print(http.get("http://example.com/start", headers: {"Authorization": "token", "Cookie": "session", "X-Trace": "safe"}).text)\n')
    built = run([str(compiler())], ["build", source.name, "-o", "app"], tmp_path)
    assert built.returncode == 0, built.stdout.decode()
    parent, child = socket.socketpair()
    parent.settimeout(5)
    proc = subprocess.Popen([str(tmp_path / "app")], cwd=tmp_path, stdin=child,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=dict(os.environ, NEXO_SECURITY_ENFORCE="1", NEXO_SECURITY_SANDBOX="linux-online-v1"))
    child.close()
    failures = []

    def broker():
        try:
            first = json.loads(receive(parent))
            assert receive(parent) == b""
            assert first["url"] == "http://example.com/start"
            assert dict(first["headers"])["Authorization"] == "token"
            send(parent, json.dumps({"status": 302, "headers": [["location", "//other.example.com/final"]]}).encode())
            send(parent, b"")
            if not allowed:
                assert parent.recv(1) == b"", "denied redirect reached the broker"
                return
            second = json.loads(receive(parent))
            assert receive(parent) == b""
            assert second["url"] == "http://other.example.com/final"
            assert dict(second["headers"]) == {"X-Trace": "safe"}
            send(parent, json.dumps({"status": 200, "headers": []}).encode())
            send(parent, b"broker response")
        except BaseException as exc:
            failures.append(exc)
        finally:
            parent.close()

    thread = threading.Thread(target=broker, daemon=True)
    thread.start()
    try:
        stdout, stderr = proc.communicate(timeout=10)
        thread.join(timeout=6)
        assert not thread.is_alive()
        assert not failures, failures
        assert proc.returncode == (0 if allowed else 1), stderr.decode()
        if allowed:
            assert stdout == b"broker response\n"
        else:
            assert b"policy" in stderr.lower()
            assert b"broker response" not in stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        parent.close()


@pytest.fixture(scope="module")
def online_supported(worker):
    result = subprocess.run([str(worker), "probe-online"], capture_output=True, text=True, check=True)
    status = json.loads(result.stdout)
    if not status["supported"]:
        pytest.skip(f"online kernel profile unavailable: {status}")


@pytest.mark.parametrize("backend", commands(), ids=lambda item: item[0])
def test_real_online_worker_blocks_private_destination(tmp_path, backend, online_supported):
    (tmp_path / "main.nexo").write_text('policy:\n    allow net "127.0.0.1"\n'
                                      'try:\n    http.get("http://127.0.0.1/", retries: 0)\n'
                                      'else e is NetworkError:\n    print("blocked")\n')
    proc = run(backend[1], ["security", "run", "--sandbox=linux-online", "main.nexo"], tmp_path)
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout == b"blocked\n"


def test_online_wall_deadline_stops_payload_and_broker(tmp_path, online_supported):
    (tmp_path / "main.nexo").write_text('policy:\n    allow net "timeout-job.example.com"\nwhile true:\n    pass\n')
    proc = run([str(compiler())], ["security", "run", "--sandbox=linux-online", "--wall-seconds", "1",
                                  "--cpu-seconds", "10", "main.nexo"], tmp_path, timeout=15)
    assert proc.returncode == 124, proc.stdout.decode()
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                command = (entry / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            assert not (b"__broker\x00" in command and b"timeout-job.example.com" in command), command
