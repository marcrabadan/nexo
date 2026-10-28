"""Shield in-process HTTP route dispatch; no listener authority is implied."""
import json

import pytest
from phases import commands, run


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def execute(tmp_path, backend, source, helper=None, permissions=(), options=(), memory=True, dependencies=True):
    (tmp_path / "main.nexo").write_text('policy:\n    allow read "."\n    allow write "."\n' + source)
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    if helper:
        (tmp_path / "helper.nexo").write_text(helper)
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {"helper.nexo": list(permissions)} if helper else {}}))
    flags = (["--web=memory"] if memory else []) + (["--dependencies", "permissions.json"] if dependencies else [])
    return run(backend, ["security", "run", *flags, *options, "main.nexo"], tmp_path, timeout=25)


BASIC = '''server api:
    get "/hello/{name}"(name: str):
        return "hello {name}"
    post "/echo"(request):
        return request.json()?
'''
REGISTER = 'pub fn register(callback: any):\n    callback()\n'


def test_inline_routes_decode_arguments_and_serialize_results_without_listener(tmp_path, backend):
    (tmp_path / "body.json").write_text('{"value":7}')
    result = execute(tmp_path, backend, BASIC + '''var r = api.dispatch("GET", "/hello/Ana")
print(r["status"], r["body"])
print(api.url)
r = api.dispatch("POST", "/echo", body: read("body.json"))
print(r["status"], r["body"])
print(r["headers"]["X-Content-Type-Options"])
''')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b'200 hello Ana\nmemory://api\n201 {"value":7}\nnosniff\n'


@pytest.mark.parametrize("dependencies", [True, False])
def test_servers_without_memory_profile_fail_before_initialization(tmp_path, backend, dependencies):
    result = execute(tmp_path, backend, 'server api:\n    get "/"():\n        return "BAD"\nprint("AFTER")\n', memory=False, dependencies=dependencies)
    assert result.returncode == 1, result.stdout.decode()
    assert b"AFTER\n" not in result.stdout
    assert b"in-memory profile" in result.stdout or b"does not support" in result.stdout


def test_handler_keeps_registration_context_after_registrar_returns(tmp_path, backend):
    source = '''use helper
api = __web_server("api")
fn handler():
    write("marker", "BAD")?
fn register():
    __web_route(api, "GET", "/", handler, [])
helper.register(register)
api.dispatch("GET", "/")
print("AFTER")
'''
    result = execute(tmp_path, backend, source, REGISTER)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_caller_context_cannot_be_dropped_by_privileged_route(tmp_path, backend):
    source = '''use helper
server api:
    get "/"():
        write("marker", "BAD")?
        return "done"
fn invoke():
    api.dispatch("GET", "/")
helper.register(invoke)
'''
    result = execute(tmp_path, backend, source, REGISTER)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_successful_dispatch_restores_root_context(tmp_path, backend):
    source = '''use helper
api = __web_server("api")
fn handler():
    return read("public.txt")?
fn register():
    __web_route(api, "GET", "/", handler, [])
helper.register(register)
print(api.dispatch("GET", "/")["body"])
print(read("private.txt"))
'''
    result = execute(tmp_path, backend, source, REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\n"


@pytest.mark.parametrize("method,target,status", [("GET", "/missing", 404), ("POST", "/hello/Ana", 405), ("GET", "/hello/Ana", 200)])
def test_http_errors_remain_responses(tmp_path, backend, method, target, status):
    result = execute(tmp_path, backend, BASIC + f'print(api.dispatch("{method}", "{target}")["status"])\n')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == f"{status}\n".encode()


@pytest.mark.parametrize("arguments", [
    '"get", "/"', '"GET", "https://example.com/"', '"GET", "//example.com/"',
    '"GET", "/x#fragment"', '"GET", "/bad path"',
    '"GET", "/", headers: {"Host": "evil"}',
    '"GET", "/", headers: {"Content-Length": "1"}',
    '"GET", "/", headers: {"Transfer-Encoding": "chunked"}',
    '"GET", "/", headers: {"X": "a", "x": "b"}',
    '"GET", "/", headers: {"X": "bad\\r\\nheader"}',
    '"GET", "/", body: {}',
])
def test_invalid_memory_requests_fail_closed(tmp_path, backend, arguments):
    result = execute(tmp_path, backend, BASIC + f'api.dispatch({arguments})\nprint("AFTER")\n')
    assert result.returncode == 1, result.stdout.decode()
    assert b"in-memory HTTP request" in result.stdout
    assert b"AFTER\n" not in result.stdout


@pytest.mark.parametrize("options", [("--web=external",), ("--web=memory", "--web=memory")])
def test_web_mode_flag_is_strict(tmp_path, backend, options):
    result = execute(tmp_path, backend, 'print("AFTER")\n', memory=False, options=options)
    assert result.returncode == 2, result.stdout.decode()
    assert b"AFTER\n" not in result.stdout


def test_request_and_server_admission_are_bounded(tmp_path, backend):
    result = execute(tmp_path, backend, 'repeat 17:\n    __web_server("api")\nprint("AFTER")\n')
    assert result.returncode == 1, result.stdout.decode()
    assert b"process resource budget" in result.stdout
    assert b"AFTER\n" not in result.stdout


def test_jwt_roles_and_public_routes_work_with_local_dispatch(tmp_path, backend):
    result = execute(tmp_path, backend, '''tokens = jwt(key: password.generate(32))
server api:
    auth = tokens
    @public
    get "/public"():
        return "open"
    @role("admin")
    get "/private"(request):
        return request.user["sub"]
print(api.dispatch("GET", "/public")["status"])
print(api.dispatch("GET", "/private")["status"])
var token = tokens.sign({"sub": "ana", "role": "user"})
print(api.dispatch("GET", "/private", headers: {"Authorization": "Bearer {token}"})["status"])
token = tokens.sign({"sub": "ana", "role": "admin"})
print(api.dispatch("GET", "/private", headers: {"Authorization": "Bearer {token}"})["body"])
''')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"200\n401\n403\nana\n"


def test_typed_conversion_defaults_run_inside_registration_ceiling(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
api = __web_server("api")
fn fallback() -> str:
    try:
        write("marker", "BAD")
    else e:
        pass
    return "value"
fn handler(value: str = fallback()):
    return value
fn register():
    __web_route(api, "GET", "/", handler, [str])
helper.register(register)
api.dispatch("GET", "/")
print("AFTER")
''', REGISTER)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_request_count_is_shared_between_servers_and_stops_before_handler(tmp_path, backend):
    result = execute(tmp_path, backend, '''server first:
    get "/"():
        return "one"
server second:
    get "/"():
        write("marker", "BAD")?
        return "two"
repeat 4096:
    first.dispatch("GET", "/")
second.dispatch("GET", "/")
print("AFTER")
''')
    assert result.returncode == 1, result.stdout.decode()
    assert b"process resource budget" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("part", ["target", "headers", "body", "response"])
def test_http_memory_size_limits_are_enforced(tmp_path, backend, part):
    size = {"target": 8193, "headers": 65537, "body": 1048577, "response": 8388609}[part]
    (tmp_path / "large.txt").write_text(("/" if part == "target" else "") + "x" * size)
    requests = {"target": 'api.dispatch("GET", read("large.txt"))',
                "headers": 'api.dispatch("GET", "/", headers: {"X": read("large.txt")})',
                "body": 'api.dispatch("GET", "/", body: read("large.txt"))',
                "response": 'api.dispatch("GET", "/")'}
    result = execute(tmp_path, backend, 'server api:\n    get "/"():\n        return read("large.txt")?\n' + requests[part] + '\nprint("AFTER")\n')
    assert result.returncode == 1, result.stdout.decode()
    assert b"in-memory HTTP request" in result.stdout
    assert b"AFTER\n" not in result.stdout


def test_security_errors_cannot_be_translated_to_http_500_or_caught(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
server api:
    get "/"():
        write("marker", "BAD")?
        return "done"
fn invoke():
    try:
        api.dispatch("GET", "/")
    else e:
        print("CAUGHT")
helper.register(invoke)
print("AFTER")
''', REGISTER)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"CAUGHT\n" not in result.stdout
    assert b"AFTER\n" not in result.stdout


def test_nested_dispatch_intersects_routes_and_restores_on_success(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
server root:
    get "/"():
        return read("public.txt")?
api = __web_server("api")
fn handler():
    return root.dispatch("GET", "/")["body"]
fn register():
    __web_route(api, "GET", "/", handler, [])
helper.register(register)
print(api.dispatch("GET", "/")["body"])
print(read("private.txt"))
''', REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\n"


@pytest.mark.parametrize("value", ["read", "[read]"])
def test_memory_http_response_boundaries_reject_callables_without_dependencies(tmp_path, backend, value):
    result = execute(tmp_path, backend, f'server api:\n    get "/"():\n        return {value}\napi.dispatch("GET", "/")\nprint("AFTER")\n', dependencies=False)
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert b"AFTER\n" not in result.stdout


def test_record_body_defaults_cannot_widen_route_context(tmp_path, backend):
    (tmp_path / "body.json").write_text("{}")
    result = execute(tmp_path, backend, '''use helper
fn stamp() -> str:
    try:
        write("marker", "BAD")
    else e:
        pass
    return "ok"
type Payload:
    value: str = stamp()
api = __web_server("api")
fn handler(payload: Payload):
    return payload.value
fn register():
    __web_route(api, "POST", "/", handler, [Payload])
helper.register(register)
api.dispatch("POST", "/", body: read("body.json"))
print("AFTER")
''', REGISTER)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_linux_profile_forwards_web_mode_or_rejects_host_before_execution(tmp_path, backend):
    import subprocess
    from pathlib import Path
    from phases import compiler
    binary = compiler()
    if binary is None:
        pytest.skip("requires native compiler and worker")
    probe = subprocess.run([str(Path(binary).with_name("nexo-shield-worker")), "probe"], capture_output=True, text=True, check=True)
    supported = json.loads(probe.stdout)["supported"]
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nserver api:\n    get "/"():\n        return "WEB_OK"\nprint(api.dispatch("GET", "/")["body"])\n')
    result = run(backend, ["security", "run", "--sandbox=linux", "--web=memory", "main.nexo"], tmp_path)
    if supported:
        assert result.returncode == 0, result.stdout.decode()
        assert result.stdout == b"WEB_OK\n"
    else:
        assert result.returncode == 125, result.stdout.decode()
        assert b"WEB_OK\n" not in result.stdout


def test_parallel_dispatch_keeps_ordered_results(tmp_path, backend):
    result = execute(tmp_path, backend, '''server api:
    get "/hello/{name}"(name: str):
        return name
results = parallel for name in ["one", "two", "three"], max: 2:
    return api.dispatch("GET", "/hello/{name}")["body"]
print(results)
''')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b'["one", "two", "three"]\n'


def test_spawned_dispatch_retains_callers_context(tmp_path, backend):
    helper = '''use nexo.sync
pub fn launch(callback: any, endpoint: any):
    spawn callback(endpoint)
'''
    result = execute(tmp_path, backend, '''use nexo.sync
use helper
server api:
    get "/"():
        write("marker", "BAD")?
        return "done"
fn worker(endpoint: any):
    endpoint.dispatch("GET", "/")
helper.launch(worker, api)
''', helper)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_route_registered_during_dispatch_keeps_installer_ceiling(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
api = __web_server("api")
fn privileged():
    write("marker", "BAD")?
    return "done"
fn install():
    __web_route(api, "GET", "/installed", privileged, [])
    return "installed"
__web_route(api, "GET", "/install", install, [])
fn invoke():
    api.dispatch("GET", "/install")
helper.register(invoke)
api.dispatch("GET", "/installed")
print("AFTER")
''', REGISTER)
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_ordinary_http_failure_restores_registration_ceiling(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
api = __web_server("api")
fn handler():
    fail("ordinary")
fn register():
    __web_route(api, "GET", "/", handler, [])
helper.register(register)
print(api.dispatch("GET", "/")["status"])
print(read("private.txt"))
''', REGISTER)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout.endswith(b"500\nPRIVATE\n")


@pytest.mark.parametrize("statement", [
    '__web_server(7)', '__web_server("")',
    '__web_route(api, "GET", 7, handler, [])',
    '__web_route(api, 7, "/", handler, [])',
    '__web_route(api, "GET", "/", handler, [], public: "yes")',
    '__web_route(api, "GET", "/", read, [])',
])
def test_malformed_internal_server_metadata_fails_before_dispatch(tmp_path, backend, statement):
    result = execute(tmp_path, backend, 'api = __web_server("api")\nfn handler():\n    print("BAD")\n' + statement + '\nprint("AFTER")\n')
    assert result.returncode == 1, result.stdout.decode()
    assert b"in-memory HTTP request" in result.stdout
    assert b"AFTER\n" not in result.stdout
    assert b"BAD\n" not in result.stdout
