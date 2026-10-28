"""Context inheritance and operator authorization, with a local fake model."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from phases import commands, run


@pytest.fixture(params=commands(), ids=lambda x: x[0])
def backend(request):
    return request.param[1]


@pytest.fixture
def model():
    seen = []
    control = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if any(m["role"] == "tool" for m in body["messages"]):
                message = {"role": "assistant", "content": "done"}
            else:
                name = body["tools"][0]["function"]["name"]
                message = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": control.get("arguments", {})}}]}
            data = json.dumps({"message": message, "done": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen, control
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configuration(tmp_path, agent_permissions=None, tool_permissions=None, allowed=True, confirm=False):
    return {"version": 1, "agents": {"main.nexo::assistant": {
        "permissions": agent_permissions if agent_permissions is not None else [["allow", "net", "127.0.0.1"], ["allow", "read", "."], ["allow", "write", "."]],
        "tools": ["main.nexo::report"] if allowed else []}},
        "tools": {"main.nexo::report": {"permissions": tool_permissions if tool_permissions is not None else [["allow", "read", "public.txt"]], "confirm": confirm}}}


def program(base, tool='return read("public.txt")?', suffix='print(assistant.handle("request"))'):
    return f'''policy:
    allow net "127.0.0.1"
    allow read "."
    allow write "."
ai:
    provider = "ollama"
    url = "{base}"
@tool "Reads a report"
fn report() -> str!:
    {tool}
agent assistant:
    model = "ollama:mock"
    tools = [report]
{suffix}
'''


def execute(tmp_path, backend, source, config, stdin=b"", extra=(), env=None):
    (tmp_path / "main.nexo").write_text(source)
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    (tmp_path / "authorization.json").write_text(json.dumps(config))
    return run(backend, ["security", "run", "--authorization", "authorization.json", *extra, "main.nexo"], tmp_path, stdin=stdin, env=env)


def test_authorized_tool_and_root_restoration(tmp_path, backend, model):
    source = program(model[0], suffix='print(assistant.handle("request"))\nprint(read("private.txt"))')
    result = execute(tmp_path, backend, source, configuration(tmp_path))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"done\nPRIVATE\n"
    assert model[1][1]["messages"][-1] == {"role": "tool", "content": "PUBLIC"}


@pytest.mark.parametrize("scope", ["agent", "tool"])
def test_each_ceiling_blocks_tool_side_effects(tmp_path, backend, model, scope):
    kwargs = {f"{scope}_permissions": [["allow", "net", "127.0.0.1"], ["allow", "read", "public.txt"]]}
    source = program(model[0], tool='write("marker", "BAD")?\n    return "bad"')
    result = execute(tmp_path, backend, source, configuration(tmp_path, **kwargs))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()
    assert len(model[1]) == 1


def test_agent_allowlist_checked_before_model_arguments_or_call(tmp_path, backend, model):
    result = execute(tmp_path, backend, program(model[0], tool='write("marker", "BAD")?\n    return "bad"'), configuration(tmp_path, allowed=False))
    assert result.returncode == 1, result.stdout.decode()
    assert b"not authorized to dispatch" in result.stdout
    assert not (tmp_path / "marker").exists()
    assert len(model[1]) == 1


def test_missing_agent_denied_before_any_model_request(tmp_path, backend, model):
    config = configuration(tmp_path)
    config["agents"] = {}
    result = execute(tmp_path, backend, program(model[0]), config)
    assert result.returncode == 1, result.stdout.decode()
    assert b"not authorized to dispatch" in result.stdout
    assert not model[1]


@pytest.mark.parametrize("answer,executed", [(b"yes\n", True), (b"no\n", False), (b"", False)])
def test_operator_forces_human_confirmation(tmp_path, backend, model, answer, executed):
    source = program(model[0], tool='write("marker", "OK")?\n    return "ok"')
    result = execute(tmp_path, backend, source, configuration(tmp_path, tool_permissions=[["allow", "write", "marker"]], confirm=True), stdin=answer)
    assert result.returncode == 0, result.stdout.decode()
    assert b"Use report()" in result.stdout
    assert (tmp_path / "marker").exists() == executed


@pytest.mark.parametrize("max_tasks", [1, 2])
def test_parallel_keeps_dependency_callers_and_restores_root(tmp_path, backend, max_tasks):
    (tmp_path / "helper.nexo").write_text(f'''pub fn invoke(callback: any) -> list[str]!:
    results = parallel for i in [1, 2, 3], max: {max_tasks}:
        return callback()
    return results
''')
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {"helper.nexo": [["allow", "read", "public.txt"]]}}))
    source = '''policy:
    allow read "."
use helper
fn grab() -> str!:
    return read("public.txt")?
print(helper.invoke(grab))
print(read("private.txt"))
'''
    (tmp_path / "main.nexo").write_text(source)
    for name in ("public", "private"):
        (tmp_path / f"{name}.txt").write_text(name.upper())
    result = run(backend, ["security", "run", "--dependencies", "permissions.json", "main.nexo"], tmp_path)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b'["PUBLIC", "PUBLIC", "PUBLIC"]\nPRIVATE\n'
    (tmp_path / "main.nexo").write_text(source.replace('read("public.txt")', 'read("private.txt")'))
    result = run(backend, ["security", "run", "--dependencies", "permissions.json", "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"PRIVATE" not in result.stdout.split(b"Error")[0]


@pytest.mark.parametrize("source", ['pipeline flow:\n    pass\n', 'server api:\n    pass\n', 'use nexo.stream\n'])
def test_authorization_rejects_unsupported_async_even_without_dependencies(tmp_path, backend, source):
    result = execute(tmp_path, backend, 'policy:\n    deny shell\n' + source, {"version": 1, "agents": {}, "tools": {}})
    assert result.returncode == 1
    assert b"does not support event loops" in result.stdout


def test_agent_needs_its_own_network_grant(tmp_path, backend, model):
    result = execute(tmp_path, backend, program(model[0]), configuration(tmp_path, agent_permissions=[]))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not model[1]


def test_tool_parallel_cannot_drop_its_ceiling(tmp_path, backend, model):
    body = 'results = parallel for i in [1, 2], max: 2:\n        write("marker", "BAD")?\n        return "x"\n    return "ok"'
    result = execute(tmp_path, backend, program(model[0], tool=body), configuration(tmp_path))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_nested_agent_cannot_widen_outer_tool_permissions(tmp_path, backend, model):
    source = program(model[0], tool='return wider.handle("nested")?')
    source = source.replace('@tool "Reads a report"', '''@tool "Writes a marker"
fn writer() -> str!:
    write("marker", "BAD")?
    return "bad"
agent wider:
    model = "ollama:mock"
    tools = [writer]
@tool "Reads a report"''')
    config = configuration(tmp_path, tool_permissions=[["allow", "net", "127.0.0.1"], ["allow", "read", "public.txt"]])
    config["agents"]["main.nexo::wider"] = {"permissions": [["allow", "net", "127.0.0.1"], ["allow", "write", "."]], "tools": ["main.nexo::writer"]}
    config["tools"]["main.nexo::writer"] = {"permissions": [["allow", "write", "."]], "confirm": False}
    result = execute(tmp_path, backend, source, config)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()
    assert len(model[1]) == 2


@pytest.mark.parametrize("mutation", ["version", "unknown", "missing", "confirm", "unknown_tool", "duplicate_tool", "alias", "unknown_permission", "bad_targets", "directory"])
def test_malformed_authorization_rejected_before_execution(tmp_path, backend, mutation):
    config = configuration(tmp_path)
    if mutation == "version":
        config["version"] = True
    elif mutation == "unknown":
        config["model_permissions"] = True
    elif mutation == "missing":
        del config["tools"]
    elif mutation == "confirm":
        config["tools"]["main.nexo::report"]["confirm"] = "yes"
    elif mutation == "unknown_tool":
        config["agents"]["main.nexo::assistant"]["tools"] = ["main.nexo::unknown"]
    elif mutation == "duplicate_tool":
        config["agents"]["main.nexo::assistant"]["tools"] *= 2
    elif mutation == "alias":
        config["tools"]["./main.nexo::report"] = config["tools"]["main.nexo::report"]
    elif mutation == "unknown_permission":
        config["tools"]["main.nexo::report"]["permissions"] = [["allow", "all", "*"]]
    elif mutation == "bad_targets":
        config["tools"]["main.nexo::report"]["permissions"] = [["allow", "read"]]
    elif mutation == "directory":
        config["tools"][".::report"] = config["tools"].pop("main.nexo::report")
    result = execute(tmp_path, backend, 'policy:\n    deny shell\nprint("EXECUTED")\n', config)
    assert result.returncode == 1, result.stdout.decode()
    assert b"authorization" in result.stdout.lower()
    assert b"EXECUTED" not in result.stdout


@pytest.mark.parametrize("text", [
    '{"version":1,"version":1,"agents":{},"tools":{}}',
    '{"version":1,"\\u0076ersion":1,"agents":{},"tools":{}}',
    '{"version":1,"agents":{},"tools":{"main.nexo::report":{"permissions":[],"confirm":true,"confirm":false}}}',
])
def test_duplicate_json_keys_are_rejected(tmp_path, backend, text):
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nprint("EXECUTED")\n')
    (tmp_path / "authorization.json").write_text(text)
    result = run(backend, ["security", "run", "--authorization", "authorization.json", "main.nexo"], tmp_path)
    assert result.returncode == 1
    assert b"EXECUTED" not in result.stdout


def test_authorization_combines_with_dependency_ceilings(tmp_path, backend, model):
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {}}))
    result = execute(tmp_path, backend, program(model[0]), configuration(tmp_path), extra=("--dependencies", "permissions.json"))
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"done\n"


def test_dependency_profile_requires_explicit_agent_authorization(tmp_path, backend, model):
    (tmp_path / "main.nexo").write_text(program(model[0]))
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {}}))
    result = run(backend, ["security", "run", "--dependencies", "permissions.json", "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"not authorized to dispatch" in result.stdout
    assert not model[1]


def test_dependency_manifest_rejects_duplicate_json_keys(tmp_path, backend):
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nprint("EXECUTED")\n')
    (tmp_path / "permissions.json").write_text('{"version":1,"modules":{},"modules":{}}')
    result = run(backend, ["security", "run", "--dependencies", "permissions.json", "main.nexo"], tmp_path)
    assert result.returncode == 1
    assert b"EXECUTED" not in result.stdout


@pytest.mark.parametrize("answer,executed", [(b"yes\n", True), (b"no\n", False)])
def test_approval_precedes_argument_record_defaults(tmp_path, backend, model, answer, executed):
    model[2]["arguments"] = {"payload": {}}
    source = program(model[0], tool='return payload.value')
    source = source.replace('@tool "Reads a report"', '''fn stamp() -> str:
    try:
        write("marker", "DEFAULT")
    else e:
        pass
    return "ok"
type Payload:
    value: str = stamp()
@tool "Reads a report"''').replace('fn report() -> str!:', 'fn report(payload: Payload) -> str!:')
    config = configuration(tmp_path, tool_permissions=[["allow", "write", "marker"]], confirm=True)
    result = execute(tmp_path, backend, source, config, stdin=answer)
    assert result.returncode == 0, result.stdout.decode()
    assert b'Use report(payload: {})' in result.stdout
    assert (tmp_path / "marker").exists() == executed


def test_profile_control_input_cannot_be_used_as_human_approval(tmp_path, backend, model):
    # Exercise the dispatch contract with the trusted profile marker, without
    # claiming kernel confinement in this independent API integration test.
    config = configuration(tmp_path, tool_permissions=[["allow", "write", "marker"]], confirm=True)
    source = program(model[0], tool='write("marker", "BAD")?\n    return "bad"')
    result = execute(tmp_path, backend, source, config, stdin=b"yes\n",
                     env={"NEXO_SECURITY_SANDBOX": "linux-offline-v1"})
    assert result.returncode == 1, result.stdout.decode()
    assert b"not authorized to dispatch" in result.stdout
    assert b"Use report()" not in result.stdout
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("allowed", [False, True])
def test_tool_identity_includes_its_declaring_module(tmp_path, backend, model, allowed):
    (tmp_path / "helper.nexo").write_text('''@tool "Writes a marker"
pub fn report() -> str!:
    write("marker", "OK")?
    return "ok"
''')
    source = program(model[0]).replace('@tool "Reads a report"', 'use helper\n@tool "Reads a report"').replace('tools = [report]', 'tools = [helper.report]')
    config = configuration(tmp_path)
    if allowed:
        config["tools"]["helper.nexo::report"] = {"permissions": [["allow", "write", "marker"]], "confirm": False}
        config["agents"]["main.nexo::assistant"]["tools"] = ["helper.nexo::report"]
    result = execute(tmp_path, backend, source, config)
    assert result.returncode == (0 if allowed else 1), result.stdout.decode()
    assert (tmp_path / "marker").exists() == allowed
    if not allowed:
        assert b"not authorized to dispatch" in result.stdout


def test_human_approval_cannot_widen_agent_grant(tmp_path, backend, model):
    config = configuration(tmp_path, agent_permissions=[["allow", "net", "127.0.0.1"]],
                           tool_permissions=[["allow", "write", "marker"]], confirm=True)
    source = program(model[0], tool='write("marker", "BAD")?\n    return "bad"')
    result = execute(tmp_path, backend, source, config, stdin=b"yes\n")
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_spawned_tool_keeps_permissions_after_agent_returns(tmp_path, backend, model):
    body = 'spawn background(ready, release)\n    return "queued"'
    source = program(model[0], tool=body, suffix='print(assistant.handle("request"))\nready.receive()\nrelease.send("go")')
    source = source.replace('@tool "Reads a report"', '''use nexo.sync
ready = channel()
release = channel()
fn background(ready, release) -> str!:
    ready.send("started")
    release.receive()
    write("marker", "BAD")?
    return "done"
@tool "Reads a report"''')
    result = execute(tmp_path, backend, source, configuration(tmp_path))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_concurrent_tool_approvals_authorize_one_call_each(tmp_path, backend, model):
    source = program(model[0], tool='write("marker", "OK")?\n    print("EFFECT")\n    return "ok"', suffix='''use nexo.sync
fn run_worker(bot: any, done: any) -> str!:
    done.send(bot.handle("request")?)
    return "done"
done = channel(size: 2)
spawn run_worker(assistant, done)
spawn run_worker(assistant, done)
done.receive()
done.receive()''')
    config = configuration(tmp_path, tool_permissions=[["allow", "write", "marker"]], confirm=True)
    result = execute(tmp_path, backend, source, config, stdin=b"yes\nno\n")
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout.count(b"Use report()") == 2
    assert result.stdout.count(b"EFFECT\n") == 1
    assert (tmp_path / "marker").read_text() == "OK"


def test_event_from_tool_cannot_widen_tool_permissions(tmp_path, backend, model):
    source = program(model[0], tool='emit changed()\n    return "done"')
    source = source.replace('@tool "Reads a report"', 'on changed():\n    write("marker", "BAD")\n@tool "Reads a report"')
    result = execute(tmp_path, backend, source, configuration(tmp_path))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_handler_registered_by_tool_keeps_permissions_after_agent_returns(tmp_path, backend, model):
    source = program(model[0], tool='on changed():\n        write("marker", "BAD")\n    return "registered"',
                     suffix='print(assistant.handle("request"))\nemit changed()')
    result = execute(tmp_path, backend, source, configuration(tmp_path))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_event_data_checks_apply_with_authorization_only(tmp_path, backend, model):
    source = program(model[0], tool='emit changed(read)\n    return "done"')
    source = source.replace('@tool "Reads a report"', 'on changed(value):\n    write("marker", "BAD")\n@tool "Reads a report"')
    result = execute(tmp_path, backend, source, configuration(tmp_path))
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_stream_from_tool_cannot_widen_publisher_permissions(tmp_path, backend, model):
    source = program(model[0], tool='kafka("orders").send("go")\n    return "done"')
    source = source.replace('@tool "Reads a report"', 'when kafka("orders") as msg:\n    write("marker", "BAD")?\n@tool "Reads a report"')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--streams=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()
    assert not (tmp_path / ".nexo" / "streams").exists()


def test_stream_registered_by_tool_keeps_permissions_after_agent_returns(tmp_path, backend, model):
    source = program(model[0], tool='when kafka("orders") as msg:\n        write("marker", "BAD")?\n    return "registered"',
                     suffix='print(assistant.handle("request"))\nkafka("orders").send("go")')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--streams=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()
    assert not (tmp_path / ".nexo" / "streams").exists()


def test_stream_payload_checks_apply_with_authorization_only(tmp_path, backend, model):
    source = program(model[0], tool='kafka("orders").send(read)\n    return "done"')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--streams=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert not (tmp_path / ".nexo" / "streams").exists()


def test_window_flush_keeps_tool_publisher_permissions_after_agent_returns(tmp_path, backend, model):
    source = program(model[0], tool='kafka("orders").send(1)\n    return "done"')
    source = source.replace('@tool "Reads a report"', '''fn show(n):
    write("marker", "BAD")?
kafka("orders")
  |> window 1m
  |> count
  |> each show
@tool "Reads a report"''')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--streams=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_pipeline_registered_by_tool_keeps_permissions_after_agent_returns(tmp_path, backend, model):
    source = program(model[0], tool='kafka("orders")\n      |> window 1m\n      |> count\n      |> each show\n    return "registered"',
                     suffix='print(assistant.handle("request"))\nkafka("orders").send(1)')
    source = source.replace('@tool "Reads a report"', 'fn show(n):\n    write("marker", "BAD")?\n@tool "Reads a report"')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--streams=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_memory_http_called_by_tool_cannot_widen_tool_permissions(tmp_path, backend, model):
    source = program(model[0], tool='api.dispatch("GET", "/")\n    return "done"')
    source = source.replace('@tool "Reads a report"', '''server api:
    get "/"():
        write("marker", "BAD")?
        return "done"
@tool "Reads a report"''')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--web=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_memory_route_registered_by_tool_keeps_its_ceiling(tmp_path, backend, model):
    source = program(model[0], tool='__web_route(api, "GET", "/", handler, [])\n    return "registered"',
                     suffix='print(assistant.handle("request"))\napi.dispatch("GET", "/")')
    source = source.replace('@tool "Reads a report"', '''api = __web_server("api")
fn handler():
    write("marker", "BAD")?
    return "done"
@tool "Reads a report"''')
    result = execute(tmp_path, backend, source, configuration(tmp_path), extra=("--web=memory",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_loopback_profile_rejects_interactive_tool_approval_before_prompt(tmp_path, backend, model):
    source = program(model[0], tool='write("marker", "BAD")?\n    return "done"')
    config = configuration(tmp_path,
                           agent_permissions=[["allow", "net", "127.0.0.1"], ["allow", "write", "marker"]],
                           tool_permissions=[["allow", "write", "marker"]], confirm=True)
    result = execute(tmp_path, backend, source, config, stdin=b"yes\n", extra=("--web=loopback:0",))
    assert result.returncode == 1, result.stdout.decode()
    assert b"not authorized to dispatch" in result.stdout
    assert b"Use report(" not in result.stdout
    assert not (tmp_path / "marker").exists()
