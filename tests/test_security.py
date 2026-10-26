"""Shield API enforcement in both backends; these are not OS sandbox tests."""
import json

import pytest

from phases import commands, compiler, run


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def execute(tmp_path, backend, source, command="run", env=None):
    (tmp_path / "app.nexo").write_text(source)
    return run(backend, ["security", command, "app.nexo"], tmp_path, env=env)


def events(proc):
    return [json.loads(line) for line in proc.stdout.decode().splitlines() if line.startswith('{"event":')]


def test_allowed_read_write_and_trace(tmp_path, backend):
    (tmp_path / "input.txt").write_text("hello")
    proc = execute(tmp_path, backend, 'policy:\n    allow read "input.txt"\n    allow write "output.txt"\n'
                   'write("output.txt", read("input.txt"))\nprint("done")\n', "trace")
    assert proc.returncode == 0, proc.stdout.decode()
    assert (tmp_path / "output.txt").read_text() == "hello"
    assert [e["operation"] for e in events(proc)] == ["file.read", "file.write"]
    assert all(e["decision"] == "allow" for e in events(proc))


@pytest.mark.parametrize("path", ["secret.txt", "data/../secret.txt", "data/link.txt", "data/up/secret.txt"])
def test_denied_read_cannot_be_caught(tmp_path, backend, path):
    (tmp_path / "data").mkdir()
    (tmp_path / "secret.txt").write_text("CONFIDENTIAL")
    (tmp_path / "data/link.txt").symlink_to("../secret.txt")
    (tmp_path / "data/up").symlink_to("..", target_is_directory=True)
    proc = execute(tmp_path, backend, 'policy:\n    allow read "data/"\n'
                   f'try:\n    print(read("{path}"))\nelse e:\n    print("caught")\nprint("escaped")\n', "trace")
    assert proc.returncode == 1, proc.stdout.decode()
    assert "CONFIDENTIAL" not in proc.stdout.decode()
    assert "\ncaught\n" not in proc.stdout.decode() and "\nescaped\n" not in proc.stdout.decode()
    assert events(proc)[-1]["decision"] == "deny"


def test_denied_write_is_not_performed(tmp_path, backend):
    proc = execute(tmp_path, backend, 'policy:\n    deny shell\nwrite("output.txt", "bad")\n')
    assert proc.returncode == 1
    assert not (tmp_path / "output.txt").exists()


@pytest.mark.parametrize("body", ['use python os', 'use c "libm"', 'run("touch marker")', 'test()'])
def test_unconfined_integrations_fail_closed(tmp_path, backend, body):
    proc = execute(tmp_path, backend, 'policy:\n    allow python "os"\n    allow c "libm"\n    allow shell\n' + body + '\n', "trace")
    assert proc.returncode == 1
    assert events(proc)[-1]["reason"] == "isolation_unavailable"
    assert not (tmp_path / "marker").exists()


def test_explicit_deny_wins_and_record_cannot_disable_shield(tmp_path, backend):
    (tmp_path / "secret.txt").write_text("CONFIDENTIAL")
    record = tmp_path / "record"
    proc = execute(tmp_path, backend, 'policy:\n    allow read "."\n    deny read "secret.txt"\nprint(read("secret.txt"))\n',
                   "trace", {"NEXO_POLICY_RECORD": str(record)})
    assert proc.returncode == 1
    assert events(proc)[-1]["reason"] == "explicit_deny"
    assert not record.exists()


def test_module_cannot_escalate(tmp_path, backend):
    (tmp_path / "helper.nexo").write_text('pub fn steal() -> str!:\n    return read("secret.txt")?\n')
    (tmp_path / "secret.txt").write_text("CONFIDENTIAL")
    proc = execute(tmp_path, backend, 'policy:\n    deny shell\nuse helper\nprint(helper.steal())\n')
    assert proc.returncode == 1
    assert "CONFIDENTIAL" not in proc.stdout.decode()
    assert "The policy does not allow reading" in proc.stdout.decode()


@pytest.mark.parametrize("body", [
    '__policy([["allow", "read"]], false)', 'reset = __policy',
    'fn __policy(entries):\n    pass', '__policy = print',
    'fn malicious(__policy):\n    pass', 'print("{__policy}")',
])
def test_policy_initializer_is_not_a_language_api(tmp_path, backend, body):
    proc = execute(tmp_path, backend, body + '\n', "check")
    assert proc.returncode == 1
    assert "internal policy initializer" in proc.stdout.decode()


def test_environment_and_audit_redaction(tmp_path, backend):
    proc = execute(tmp_path, backend, 'policy:\n    deny env "SHIELD_TEST_KEY"\n'
                   'use nexo.db\nconnect(":memory:", key: secret("SHIELD_TEST_KEY"))\n',
                   "trace", {"SHIELD_TEST_KEY": "credential-must-never-be-logged"})
    assert proc.returncode == 1
    event = events(proc)[-1]
    assert event["operation"] == "environment.read" and event["target"] == "[redacted]"
    assert "SHIELD_TEST_KEY" not in json.dumps(event)
    assert "credential-must-never-be-logged" not in proc.stdout.decode()


@pytest.mark.parametrize("command", ["check", "run", "trace", "audit"])
def test_security_requires_policy(tmp_path, backend, command):
    proc = execute(tmp_path, backend, 'print("must not execute")\n', command)
    assert proc.returncode == 1
    assert "needs a policy block" in proc.stdout.decode()
    assert "\nmust not execute\n" not in proc.stdout.decode()


@pytest.mark.parametrize("command", ["check", "audit"])
def test_static_commands_do_not_execute(tmp_path, backend, command):
    proc = execute(tmp_path, backend, 'policy:\n    allow write "marker"\nwrite("marker", "bad")\n', command)
    assert proc.returncode == 0, proc.stdout.decode()
    assert not (tmp_path / "marker").exists()


def test_unsupported_sandbox_configuration(tmp_path, backend):
    proc = run(backend, ["security", "run", "--sandbox=unknown", "app.nexo"], tmp_path)
    assert proc.returncode == 2


def test_release_record_cannot_bypass_policy(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    (tmp_path / "app.nexo").write_text('policy:\n    deny read "secret.txt"\nprint(read("secret.txt"))\n')
    (tmp_path / "secret.txt").write_text("CONFIDENTIAL")
    built = run([str(nexo)], ["build", "--release", "app.nexo", "-o", "app"], tmp_path, timeout=600)
    assert built.returncode == 0, built.stdout.decode()
    proc = run([str(tmp_path / "app")], [], tmp_path, env={"NEXO_POLICY_RECORD": str(tmp_path / "record")})
    assert proc.returncode == 1
    assert "CONFIDENTIAL" not in proc.stdout.decode()
    assert not (tmp_path / "record").exists()


@pytest.fixture
def network():
    import http.server
    import threading
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/forbidden")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"allowed response")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server.server_port, hits
    server.shutdown()
    server.server_close()
    worker.join()


def test_network_and_redirect_enforce_before_request(tmp_path, backend, network):
    port, hits = network
    proc = execute(tmp_path, backend, 'policy:\n    allow net "127.0.0.1"\n'
                   f'print(http.get("http://127.0.0.1:{port}/ok", retries: 0).text)\n'
                   f'print(http.get("http://127.0.0.1:{port}/redirect", retries: 0).text)\n', "trace")
    assert proc.returncode == 1, proc.stdout.decode()
    assert hits == ["/ok", "/redirect"]
    assert "allowed response" in proc.stdout.decode()
    net_events = [e for e in events(proc) if e["operation"] == "network.connect"]
    assert [e["decision"] for e in net_events] == ["allow", "allow", "deny"]


def test_denied_network_never_connects(tmp_path, backend, network):
    port, hits = network
    proc = execute(tmp_path, backend, 'policy:\n    deny shell\n'
                   f'http.get("http://127.0.0.1:{port}/forbidden", retries: 0)\n')
    assert proc.returncode == 1
    assert hits == []


@pytest.mark.parametrize("command", ["check", "run", "audit"])
@pytest.mark.parametrize("dependency", [False, True])
def test_denied_python_import_does_not_start_compiler_probe(tmp_path, command, dependency):
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    probe = tmp_path / "python-probe"
    probe.write_text('#!/bin/sh\ntouch probe-started\nexit 1\n')
    probe.chmod(0o700)
    body = 'use python os\n'
    if dependency:
        (tmp_path / "helper.nexo").write_text(body)
        body = 'use helper\n'
    proc = execute(tmp_path, [str(nexo)], 'policy:\n    allow python "os"\n' + body,
                   command, {"NEXO_PYTHON": str(probe)})
    assert proc.returncode == (1 if command == "run" else 0), proc.stdout.decode()
    assert not (tmp_path / "probe-started").exists()


def test_ambiguous_url_cannot_bypass_host_policy(tmp_path, backend, network):
    port, hits = network
    # WHATWG URL parsing treats a backslash as a slash; a lexical hostname
    # extraction would instead authorize the text after @.
    url = json.dumps(f"http://127.0.0.1:{port}\\@allowed.example/")
    proc = execute(tmp_path, backend, 'policy:\n    allow net "allowed.example"\n'
                   f'print(http.get({url}, retries: 0).text)\n')
    assert proc.returncode == 1
    assert hits == []
    assert "ambiguous URL" in proc.stdout.decode()


@pytest.mark.parametrize("grant", ["deny shell", 'allow write ".nexo"'])
def test_persistent_workflow_is_rejected_before_steps_or_state(tmp_path, backend, grant):
    proc = execute(tmp_path, backend, 'policy:\n    ' + grant + '\n'
                   'workflow job():\n    step print("WORKFLOW_EXECUTED")\ntry:\n    job()\nelse e:\n    print("CAUGHT")\n')
    assert proc.returncode == 1, proc.stdout.decode()
    assert "cannot confine persistent workflow state" in proc.stdout.decode()
    assert "\nWORKFLOW_EXECUTED\n" not in proc.stdout.decode()
    assert "\nCAUGHT\n" not in proc.stdout.decode()
    assert not (tmp_path / ".nexo").exists()
