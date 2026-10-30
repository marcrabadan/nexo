"""Phase 3 first slice: typed results and cooperative dispatch deadlines."""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import ROOT, commands, run


@pytest.fixture(params=commands(), ids=lambda c: c[0])
def backend(request):
    return request.param[1]


@pytest.fixture
def model():
    control = {"provider": "ollama", "content": '{"answer":"ready","score":4}'}
    seen = []
    provider_entered = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/late": control["release_late"].wait(timeout=20)
            elif self.path == "/started": provider_entered.wait(timeout=5)
            else: time.sleep(control.get("tool_delay", 0))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            provider_entered.set()
            if control.get("disconnect"):
                import socket
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            time.sleep(control.get("delay", 0))
            used = any(m.get("role") == "tool" for m in body.get("messages", []))
            used |= any("tool_result" in json.dumps(m) or "functionResponse" in json.dumps(m)
                        for m in body.get("messages", body.get("contents", [])))
            text = control["content"]
            if control.get("repeat_calls"): used=False
            calls = None if used else control.get("calls")
            if control.get("nested_ask") and not body.get("tools"): calls=None
            provider = control["provider"]
            if provider == "anthropic":
                content = ([{"type": "tool_use", "id": c.get("id", str(i)),
                             "name": c["function"]["name"], "input": c["function"]["arguments"]}
                            for i, c in enumerate(calls)] if calls is not None else
                           [{"type": "text", "text": text}])
                answer = {"content": content}
            elif provider == "gemini":
                parts = ([{"functionCall": {"name": c["function"]["name"], "args": c["function"]["arguments"]}}
                          for c in calls] if calls is not None else [{"text": text}])
                answer = {"candidates": [{"content": {"parts": parts}}]}
            else:
                message = {"role": "assistant", "content": "" if calls is not None else text}
                if calls is not None:
                    message["tool_calls"] = calls
                answer = ({"choices": [{"message": message}]} if provider == "openai" else {"message": message})
            answer.update(control.get("usage_response", {}))
            payload = control.get("raw", json.dumps(answer)).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", control, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def source(model, suffix, tool="", confirm=False):
    base, control, _ = model
    return f'''ai:
    provider = "{control['provider']}"
    model = "mock"
    key = secret("NEXO_AI_KEY")
    url = "{base}"
type Finding:
    answer: str
    score: 1..5
{tool}
agent reviewer:
    tools = [{"report" if tool else ""}]
    goal = "Review the supplied input"
    max_steps = 3
    confirm = [{"report" if confirm else ""}]
{suffix}
'''


def execute(tmp_path, backend, program, stdin=b""):
    (tmp_path / "main.nexo").write_text(program)
    return run(backend, ["main.nexo"], tmp_path, stdin=stdin,
               env={"NEXO_AI_KEY": "local-test", "XDG_CONFIG_HOME": str(tmp_path / "config")})


@pytest.mark.parametrize("provider", ["ollama", "openai", "anthropic", "gemini"])
def test_typed_final_response(tmp_path, backend, model, provider):
    model[1]["provider"] = provider
    result = execute(tmp_path, backend, source(model, 'finding = reviewer.handle("text", as: Finding, timeout: 5s)\nprint(finding.answer, finding.score)'))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"ready 4\n"
    assert len(model[2]) == 1
    assert "Answer only with JSON" in json.dumps(model[2][0])


@pytest.mark.parametrize("content", ['{"answer":"ready","score":99}', 'not JSON', '{"score":4}'])
def test_invalid_final_result_is_not_retried(tmp_path, backend, model, content):
    model[1]["content"] = content
    result = execute(tmp_path, backend, source(model, '''try:
    reviewer.handle("text", as: Finding)
else e is InvalidData:
    print("invalid output")'''))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid output\n"
    assert len(model[2]) == 1


@pytest.mark.parametrize("options", ['as: 7', 'timeout: 0s', 'timeout: -1s', 'timeout: 25h', 'timeout: 3'])
def test_invalid_options_fail_before_provider(tmp_path, backend, model, options):
    result = execute(tmp_path, backend, source(model, f'reviewer.handle("text", {options})'))
    assert result.returncode == 1, result.stdout.decode()
    assert b"An agent timeout" in result.stdout or b"expects" in result.stdout, result.stdout.decode()
    assert not model[2]


TOOL = '''@tool "Writes a local marker"
fn report() -> str!:
    write("marker", "executed")?
    return "done"
'''


def tool_call(arguments=None, name="report", id="call-1"):
    return {"id": id, "function": {"name": name, "arguments": {} if arguments is None else arguments}}


@pytest.mark.parametrize("provider", ["ollama", "openai", "anthropic", "gemini"])
def test_malformed_tool_arguments_never_dispatch(tmp_path, backend, model, provider):
    model[1].update(provider=provider, calls=[tool_call([])])
    result = execute(tmp_path, backend, source(model, '''try:
    reviewer.handle("text")
else e is AIError:
    print("invalid tool call")''', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid tool call\n"
    assert not (tmp_path / "marker").exists()


def test_unknown_tool_parameter_is_rejected(tmp_path, backend, model):
    model[1]["calls"] = [tool_call({"unexpected": 1})]
    result = execute(tmp_path, backend, source(model, 'print(reviewer.handle("text"))', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert not (tmp_path / "marker").exists()
    assert len(model[2]) == 2
    assert "the tool parameter unexpected is not declared" in json.dumps(model[2][1])


def test_duplicate_tool_call_ids_reject_entire_batch(tmp_path, backend, model):
    model[1]["calls"] = [tool_call(), tool_call()]
    result = execute(tmp_path, backend, source(model, '''try:
    reviewer.handle("text")
else e is AIError:
    print("invalid batch")''', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid batch\n"
    assert not (tmp_path / "marker").exists()


def test_deadline_drops_delayed_dispatch(tmp_path, backend, model):
    model[1].update(delay=0.15, calls=[tool_call()])
    result = execute(tmp_path, backend, source(model, '''try:
    reviewer.handle("text", timeout: 30ms)
else e is Timeout:
    print("deadline expired")''', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"deadline expired\n"
    assert not (tmp_path / "marker").exists()
    assert len(model[2]) == 1


def test_approval_precedes_defaults_and_expired_approval_cannot_dispatch(tmp_path, backend, model):
    tool = '''fn default_value() -> str!:
    write("default-marker", "evaluated")?
    return "value"
type Request:
    value: str = default_value()
@tool "Writes a marker"
fn report(request: Request) -> str!:
    write("marker", request.value)?
    return "done"
'''
    model[1]["calls"] = [tool_call({"request": {}})]
    (tmp_path / "main.nexo").write_text(source(model, '''try:
    reviewer.handle("text", timeout: 500ms)
else e is Timeout:
    print("deadline expired")''', tool, confirm=True))
    env = dict(os.environ, PYTHONPATH=str(ROOT), NEXO_AI_KEY="local-test", NEXO_LEAKCHECK="1",
               XDG_CONFIG_HOME=str(tmp_path / "config"))
    with (tmp_path / "output").open("wb") as output:
        proc = subprocess.Popen([*backend, "main.nexo"], cwd=tmp_path, env=env,
                                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
        try:
            until = time.monotonic() + 20
            while b"(y/n): " not in (tmp_path / "output").read_bytes():
                assert proc.poll() is None, (tmp_path / "output").read_text()
                assert time.monotonic() < until, "approval prompt did not appear"
                time.sleep(0.01)
            assert not (tmp_path / "default-marker").exists()
            time.sleep(0.6)
            proc.communicate(b"yes\n", timeout=10)
            assert proc.returncode == 0, (tmp_path / "output").read_text()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    assert b"deadline expired" in (tmp_path / "output").read_bytes()
    assert not (tmp_path / "marker").exists()
    assert not (tmp_path / "default-marker").exists()
    assert len(model[2]) == 1


@pytest.mark.parametrize("type_name,content,expected", [("str", "plain text", b"plain text\n"), ("int", "7", b"7\n")])
def test_scalar_final_response(tmp_path, backend, model, type_name, content, expected):
    model[1]["content"] = content
    result = execute(tmp_path, backend, source(model, f'print(reviewer.handle("text", as: {type_name}))'))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == expected


@pytest.mark.parametrize("calls", [{}, [tool_call(id=7)], [tool_call(name=7)]])
def test_malformed_call_metadata_rejects_before_dispatch(tmp_path, backend, model, calls):
    model[1]["calls"] = calls
    result = execute(tmp_path, backend, source(model, 'try:\n    reviewer.handle("text")\nelse e is AIError:\n    print("invalid metadata")', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid metadata\n"
    assert not (tmp_path / "marker").exists()


def test_refusal_does_not_evaluate_record_defaults_without_manifest(tmp_path, backend, model):
    tool = '''fn default_value() -> str!:
    write("default-marker", "evaluated")?
    return "value"
type Request:
    value: str = default_value()
@tool "Writes a marker"
fn report(request: Request) -> str!:
    write("marker", request.value)?
    return "done"
'''
    model[1]["calls"] = [tool_call({"request": {}})]
    result = execute(tmp_path, backend, source(model, 'print(reviewer.handle("text"))', tool, confirm=True), stdin=b"no\n")
    assert result.returncode == 0, result.stdout.decode()
    assert len(model[2]) == 2
    assert "The user did not allow this call" in json.dumps(model[2][1])
    assert not (tmp_path / "marker").exists()
    assert not (tmp_path / "default-marker").exists()


@pytest.mark.parametrize("raw", [
    "[" * 150 + "0" + "]" * 150,
    '{"message":{"content":0,"tool_calls":[]}}',
    json.dumps({"message": {"content": "done", "tool_calls": [tool_call('{"value": NaN}')]}}),
])
def test_invalid_provider_json_or_text_never_dispatch(tmp_path, backend, model, raw):
    model[1]["raw"] = raw
    result = execute(tmp_path, backend, source(model, 'try:\n    reviewer.handle("text")\nelse e is AIError:\n    print("invalid provider response")', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid provider response\n"
    assert not (tmp_path / "marker").exists()


def test_invalid_final_result_after_tool_does_not_replay_effect(tmp_path, backend, model):
    model[1].update(calls=[tool_call()], content='{"answer":"ready","score":99}')
    result = execute(tmp_path, backend, source(model, 'try:\n    reviewer.handle("text", as: Finding)\nelse e is InvalidData:\n    print("invalid final output")', TOOL))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid final output\n"
    assert (tmp_path / "marker").read_text() == "executed"
    assert len(model[2]) == 2


def test_expiry_after_admitted_tool_blocks_next_call_in_batch(tmp_path, backend, model):
    base, control, seen = model
    control.update(tool_delay=0.7, calls=[tool_call(), tool_call(name="other", id="call-2")])
    tool = f'''@tool "Long operation already admitted"
fn report() -> str!:
    http.get("{base}/slow", retries: 0)?
    write("marker", "admitted work completed")?
    return "done"
@tool "Must not be admitted after expiry"
fn other() -> str!:
    write("second-marker", "bad")?
    return "done"
'''
    program = source(model, 'try:\n    reviewer.handle("text", timeout: 500ms)\nelse e is Timeout:\n    print("next dispatch blocked")', tool)
    program = program.replace("tools = [report]", "tools = [report, other]")
    result = execute(tmp_path, backend, program)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"next dispatch blocked\n"
    assert not (tmp_path / "marker").exists()
    assert not (tmp_path / "second-marker").exists()
    assert len(seen) == 1


def test_unsupported_output_alias_fails_before_provider(tmp_path, backend, model):
    program = source(model, 'type Status = "active" | "closed"\nreviewer.handle("text", as: Status)')
    result = execute(tmp_path, backend, program)
    assert result.returncode == 1, result.stdout.decode()
    assert b"JSON-compatible type" in result.stdout, result.stdout.decode()
    assert not model[2]


@pytest.mark.parametrize("content", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_non_json_final_number_is_rejected(tmp_path, backend, model, content):
    model[1]["content"] = content
    result = execute(tmp_path, backend, source(model, 'try:\n    reviewer.handle("text", as: float)\nelse e is InvalidData:\n    print("invalid number")'))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"invalid number\n"
    assert len(model[2]) == 1


def test_early_network_failure_keeps_its_error_kind(tmp_path, backend, model):
    model[1]["disconnect"] = True
    program = source(model, 'try:\n    reviewer.handle("text", timeout: 5s)\nelse e is NetworkError:\n    print("network failure")')
    result = execute(tmp_path, backend, program)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"network failure\n"
    assert len(model[2]) == 1


def test_deadline_does_not_replace_policy_denial(tmp_path, backend, model):
    policy = 'policy:\n    allow env "NEXO_AI_KEY"\n    deny net "127.0.0.1"\n'
    program = policy + source(model, 'reviewer.handle("text", timeout: 5s)', TOOL)
    result = execute(tmp_path, backend, program)
    assert result.returncode == 1, result.stdout.decode()
    assert b"policy denies" in result.stdout, result.stdout.decode()
    assert b"exceeded its deadline" not in result.stdout
    assert not model[2]
    assert not (tmp_path / "marker").exists()


def test_cancelled_before_provider_closes_admission(tmp_path, backend, model):
    program = source(model, '\n'.join([
        'control = reviewer.control()', 'control.cancel()',
        'print(control.cancelled)', 'try:',
        '    reviewer.handle("text", cancel: control)',
        'else e is Cancelled:', '    print("cancelled")',
        'try:\n    write("outside", "scope restored")\nelse e:\n    print("unexpected write error")']))
    result = execute(tmp_path, backend, program)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"true\ncancelled\n"
    assert not model[2]
    assert (tmp_path / "outside").read_text() == "scope restored"


def test_cancelled_tool_cannot_start_a_later_write(tmp_path, backend, model):
    model[1]["calls"] = [tool_call()]
    tool = '''control = reviewer.control()
@tool "Closes its invocation"
fn report() -> str!:
    control.cancel()
    write("marker", "must not execute")?
    return "done"
'''
    # Control belongs to an existing agent; it is independent of its creator.
    prefix = source(model, 'control = reviewer.control()')
    program = prefix + tool.replace('control = reviewer.control()\n', '') + '''
agent worker:
    tools = [report]
    max_steps = 3
try:
    worker.handle("text", cancel: control)
else e is Cancelled:
    print("cancelled")
try:
    write("outside", "scope restored")
else e:
    print("unexpected write error")
'''
    result = execute(tmp_path, backend, program)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"cancelled\n"
    assert not (tmp_path / "marker").exists()
    assert len(model[2]) == 1
    assert (tmp_path / "outside").exists()


def test_external_cancellation_during_provider_blocks_tools(tmp_path, backend, model):
    model[1].update(delay=0.4,calls=[tool_call()])
    # A separate task can close a control while the provider request is in flight.
    program='use nexo.sync\n'+source(model,'control = reviewer.control()',TOOL)+'''
fn cancel_later(control,url,client):
    try:
        client.get(url,retries: 0)
    else e:
        print("handshake failed")
    control.cancel()
spawn cancel_later(control,"HANDSHAKE",http)
try:
    reviewer.handle("text", cancel: control)
else e is Cancelled:
    print("cancelled")
'''
    program=program.replace('HANDSHAKE',model[0]+'/started')
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert result.stdout==b'cancelled\n'
    assert len(model[2])==1
    assert not (tmp_path/'marker').exists()


def test_spawned_tool_child_inherits_closed_admission(tmp_path,backend,model):
    model[1]['calls']=[tool_call()]
    prefix='use nexo.sync\n'+source(model,'control = reviewer.control()')
    program=prefix+'''
fn child(go,done):
    go.receive()
    try:
        write("marker", "must not execute")
    else e is Cancelled:
        done.send("blocked")
@tool "Starts inherited work"
fn report() -> str:
    go = channel(size: 1)
    done = channel(size: 1)
    spawn child(go,done)
    control.cancel()
    go.send("go")
    return done.receive()
agent worker:
    tools = [report]
try:
    worker.handle("text", cancel: control)
else e is Cancelled:
    print("cancelled")
'''
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert result.stdout==b'cancelled\n'
    assert len(model[2])==1
    assert not (tmp_path/'marker').exists()


def test_nested_agent_inherits_closed_admission(tmp_path,backend,model):
    model[1]['calls']=[tool_call()]
    program=source(model,'control = reviewer.control()')+'''
agent nested:
    goal = "Nested work"
@tool "Attempts nested work"
fn report() -> str!:
    control.cancel()
    return nested.handle("must not dispatch")?
agent worker:
    tools = [report]
try:
    worker.handle("text", cancel: control)
else e is Cancelled:
    print("cancelled")
'''
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert result.stdout==b'cancelled\n'
    assert len(model[2])==1


def test_declared_input_type_validates_and_hides_private_fields(tmp_path,backend,model):
    program=source(model,'print(reviewer.handle(Request("PUBLIC-TEXT", "PRIVATE-CRED-SENTINEL"), as: Finding))')
    program=program.replace('agent reviewer:', 'type Request:\n    text: str\n    credential: str @private\nagent reviewer:\n    input_type = Request')
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert len(model[2])==1
    assert 'PUBLIC-TEXT' in json.dumps(model[2])
    assert 'PRIVATE-CRED-SENTINEL' not in json.dumps(model[2])


def test_wrong_declared_input_type_fails_before_provider(tmp_path,backend,model):
    program=source(model,'try:\n    reviewer.handle("wrong type")\nelse e is InvalidData:\n    print("invalid input")')
    program=program.replace('agent reviewer:', 'type Request:\n    text: str\nagent reviewer:\n    input_type = Request')
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert result.stdout==b'invalid input\n'
    assert not model[2]
