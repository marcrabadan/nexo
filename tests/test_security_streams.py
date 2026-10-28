"""Shield process-local streams: context ceilings, no persistent state bypass."""
import json

import pytest
from phases import commands, run


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def execute(tmp_path, backend, body, helper=None, permissions=(), memory=True, dependencies=True):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    if helper is not None:
        (tmp_path / "helper.nexo").write_text(helper)
    (tmp_path / "main.nexo").write_text('policy:\n    allow read "."\n    allow write "."\n' + body)
    modules = {"helper.nexo": list(permissions)} if helper is not None else {}
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": modules}))
    options = (["--streams=memory"] if memory else []) + (["--dependencies", "permissions.json"] if dependencies else [])
    result = run(backend, ["security", "run", *options, "main.nexo"], tmp_path, timeout=20)
    assert not (tmp_path / ".nexo" / "streams").exists(), "Shield must never write broker files"
    return result


def test_plain_messages_deliver_and_program_exits_without_listener(tmp_path, backend):
    result = execute(tmp_path, backend, '''use nexo.stream
when kafka("orders") as msg:
    print(msg.id, msg.json()["value"])
kafka("orders").send({"value": 7})
print("finished")
''')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"1 7\nfinished\n"


@pytest.mark.parametrize("dependencies", [False, True])
def test_persistent_send_is_denied_without_explicit_memory_profile(tmp_path, backend, dependencies):
    # Bare kafka(...).send(...) previously bypassed policy and wrote a log even
    # without a write grant; this checks the runtime boundary, not just the lexer.
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nkafka("ungranted").send("data")\n')
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {}}))
    options = ["--dependencies", "permissions.json"] if dependencies else []
    result = run(backend, ["security", "run", *options, "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"explicit in-memory profile" in result.stdout
    assert not (tmp_path / ".nexo" / "streams").exists()



REGISTER = 'pub fn register(callback: any):\n    callback()\n'


def test_handler_retains_registration_context_and_restores_root(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn register():
    when kafka("orders") as msg:
        print(read("public.txt")?)
helper.register(register)
kafka("orders").send("go")
print(read("private.txt"))
''', REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\n"


def test_root_handler_cannot_discard_registration_ceiling(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn register():
    when kafka("orders") as msg:
        write("marker", "BAD")?
helper.register(register)
kafka("orders").send("go")
''', REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"dead-letter" not in result.stdout.lower()
    assert not (tmp_path / "marker").exists()


def test_root_handler_cannot_widen_restricted_publisher(tmp_path, backend):
    helper = 'pub fn publish():\n    kafka("orders").send("go")\n'
    result = execute(tmp_path, backend, '''use helper
when kafka("orders") as msg:
    write("marker", "BAD")?
helper.publish()
''', helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_key_function_keeps_publisher_ceiling_and_is_not_retried(tmp_path, backend):
    helper = 'pub fn publish():\n    kafka("orders").send("go")\n'
    result = execute(tmp_path, backend, '''use helper
fn key(msg) -> str!:
    write("marker", "BAD")?
    return "key"
when kafka("orders") as msg, key: key(msg)?:
    print("HANDLER")
helper.publish()
''', helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"HANDLER\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_each_handler_restores_the_previous_context(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn register():
    when kafka("orders") as msg:
        print(read("public.txt")?)
helper.register(register)
when kafka("orders") as msg:
    print(read("private.txt")?)
kafka("orders").send("go")
''', REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\n"


def test_sequential_keys_are_deduplicated_in_memory(tmp_path, backend):
    result = execute(tmp_path, backend, '''when kafka("orders") as msg, key: msg.text():
    print(msg.text())
kafka("orders").send("one")
kafka("orders").send("one")
kafka("orders").send("two")
''')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"one\ntwo\n"


def test_ordinary_handler_failures_retry_and_restore_root(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn register():
    when kafka("orders") as msg:
        print("attempt")
        fail("temporary")
helper.register(register)
kafka("orders").send("go")
print(read("private.txt"))
''', REGISTER, [])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout.count(b"attempt\n") == 4
    assert b"PRIVATE\n" in result.stdout


@pytest.mark.parametrize("value", ["read", "[read]", '{"items": [read]}'])
def test_payload_authority_is_rejected_before_delivery(tmp_path, backend, value):
    result = execute(tmp_path, backend, f'''when kafka("orders") as msg:
    write("marker", "BAD")?
kafka("orders").send({value})
''')
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("step", ["window 1m", "sliding 1m", "session 1m", "count"])
def test_deferred_stream_pipelines_require_memory_profile(tmp_path, backend, step):
    result = execute(tmp_path, backend, f'kafka("orders")\n  |> {step}\n', memory=False)
    assert result.returncode == 1, result.stdout.decode()
    assert b"explicit in-memory profile" in result.stdout


@pytest.mark.parametrize("name", ["", "../escape", "bad/name", "bad\nname", "x" * 257])
def test_memory_names_are_bounded_and_not_paths(tmp_path, backend, name):
    result = execute(tmp_path, backend, f'kafka({json.dumps(name)}).send("data")\n')
    assert result.returncode == 1, result.stdout.decode()
    assert b"stream name is invalid" in result.stdout


@pytest.mark.parametrize("options", [["--streams=disk"], ["--streams=memory", "--streams=memory"]])
def test_unknown_or_duplicate_profile_rejected_before_execution(tmp_path, backend, options):
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nprint("EXECUTED")\n')
    result = run(backend, ["security", "run", *options, "main.nexo"], tmp_path)
    assert result.returncode == 2
    assert b"EXECUTED\n" not in result.stdout


def test_memory_profile_without_dependency_file_is_data_only(tmp_path, backend):
    result = execute(tmp_path, backend, 'kafka("orders").send(read)\n', dependencies=False)
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout


def test_key_retains_registration_context_after_register_returns(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn register():
    when kafka("orders") as msg, key: read("private.txt")?:
        print("HANDLER")
helper.register(register)
kafka("orders").send("go")
''', REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"HANDLER\n" not in result.stdout


def test_nested_publish_cannot_widen_outer_handler_permissions(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
when kafka("inner") as msg:
    write("marker", "BAD")?
fn register():
    when kafka("outer") as msg:
        kafka("inner").send(msg.text())
helper.register(register)
kafka("outer").send("go")
''', REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_spawn_registered_handler_retains_original_permissions(tmp_path, backend):
    helper = '''use nexo.sync
pub fn launch(callback: any, done: any):
    spawn register(callback, done)
fn register(callback, done):
    callback()
    done.send("registered")
'''
    result = execute(tmp_path, backend, '''use nexo.sync
use helper
fn register():
    when kafka("orders") as msg:
        write("marker", "BAD")?
done = channel()
helper.launch(register, done)
done.receive()
kafka("orders").send("go")
''', helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_concurrent_publishers_restore_their_caller_contexts(tmp_path, backend):
    helper = '''use nexo.sync
pub fn launch(done: any):
    spawn publish(done)
fn publish(done):
    kafka("orders").send("go")
    done.send("finished")
'''
    result = execute(tmp_path, backend, '''use nexo.sync
use helper
when kafka("orders") as msg:
    print(read("public.txt")?)
done = channel(size: 2)
helper.launch(done)
helper.launch(done)
done.receive()
done.receive()
print(read("private.txt"))
''', helper, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPUBLIC\nPRIVATE\n"


@pytest.mark.parametrize("budget", ["message", "retained_bytes", "retained_messages"])
def test_broker_message_history_limits_fail_closed(tmp_path, backend, budget):
    if budget == "message":
        (tmp_path / "large.txt").write_text("x" * 1048577)
        body = 'kafka("orders").send(read("large.txt"))\n'
    elif budget == "retained_bytes":
        (tmp_path / "large.txt").write_text("x" * 1048576)
        body = 'value = read("large.txt")\nrepeat 9:\n    kafka("orders").send(value)\n'
    else:
        body = 'repeat 4097:\n    kafka("orders").send("x")\n'
    result = execute(tmp_path, backend, body + 'print("COMPLETED")\n')
    assert result.returncode == 1, result.stdout.decode()
    assert b"broker budget was exceeded" in result.stdout
    assert b"COMPLETED\n" not in result.stdout


def test_security_violations_are_not_catchable_or_sent_to_dead_letter(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn register():
    when kafka("orders") as msg:
        try:
            write("marker", "BAD")
        else e:
            print("CAUGHT")
helper.register(register)
try:
    kafka("orders").send("go")
else e:
    print("CAUGHT")
''', REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"CAUGHT\n" not in result.stdout
    assert b"Dead-letter" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_linux_profile_preserves_memory_mode_or_rejects_host_before_execution(tmp_path, backend):
    import subprocess
    from pathlib import Path
    from phases import compiler
    binary = compiler()
    if binary is None:
        pytest.skip("requires native compiler and worker")
    status = json.loads(subprocess.run([str(Path(binary).with_name("nexo-shield-worker")), "probe"], capture_output=True, text=True, check=True).stdout)
    (tmp_path / "main.nexo").write_text('''policy:
    deny shell
when kafka("orders") as msg:
    print(msg.text())
kafka("orders").send("MEMORY_OK")
''')
    result = run(backend, ["security", "run", "--sandbox=linux", "--streams=memory", "main.nexo"], tmp_path)
    assert not (tmp_path / ".nexo" / "streams").exists()
    if status["supported"]:
        assert result.returncode == 0, result.stdout.decode()
        assert result.stdout == b"MEMORY_OK\n"
    else:
        assert result.returncode == 125, result.stdout.decode()
        assert b"MEMORY_OK\n" not in result.stdout


@pytest.mark.parametrize("step, expected", [("window 1m", b"2\n"), ("session 1m", b"2\n"), ("sliding 1m", b"1\n2\n"), ("", b"1\n1\n")])
def test_memory_pipeline_aggregates_and_flushes_at_exit(tmp_path, backend, step, expected):
    pipeline = (f"  |> {step}\n" if step else "") + "  |> count\n  |> each show\n"
    result = execute(tmp_path, backend, 'fn show(n):\n    print(n)\nkafka("orders")\n' + pipeline + 'kafka("orders").send(1)\nkafka("orders").send(2)\n')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == expected


@pytest.mark.parametrize("step", ["window 1m", "session 1m", "sliding 1m", ""])
def test_deferred_pipeline_keeps_registration_context(tmp_path, backend, step):
    pipeline = (f"      |> {step}\n" if step else "") + "      |> count\n      |> each show\n"
    result = execute(tmp_path, backend, 'use helper\nfn show(n):\n    write("marker", "BAD")?\nfn register():\n    kafka("orders")\n' + pipeline + 'helper.register(register)\nkafka("orders").send(1)\n', REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("step", ["window 1m", "session 1m", "sliding 1m", ""])
def test_deferred_pipeline_keeps_past_publishers_at_exit(tmp_path, backend, step):
    pipeline = (f"  |> {step}\n" if step else "") + "  |> count\n  |> each show\n"
    helper = 'pub fn publish():\n    kafka("orders").send(1)\n'
    result = execute(tmp_path, backend, 'use helper\nfn show(n):\n    write("marker", "BAD")?\nkafka("orders")\n' + pipeline + 'helper.publish()\nkafka("orders").send(2)\n', helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("step", ["window 1m", "session 1m", "sliding 1m"])
def test_window_intersects_every_contributing_publisher(tmp_path, backend, step):
    helper = 'pub fn publish():\n    kafka("orders").send(1)\n'
    result = execute(tmp_path, backend, 'use helper\nfn show(n):\n    print(read("private.txt")?)\nkafka("orders")\n  |> ' + step + '\n  |> count\n  |> each show\nhelper.publish()\nkafka("orders").send(2)\n', helper, [["allow", "read", "public.txt"]])
    assert result.returncode == 1, result.stdout.decode()
    assert b"PRIVATE" not in result.stdout


def test_window_success_restores_root_and_allows_reentrant_publication(tmp_path, backend):
    result = execute(tmp_path, backend, 'use helper\nfn show(n):\n    print(read("public.txt")?)\n    kafka("nested").send(n)\nfn register():\n    kafka("orders")\n      |> sliding 1m\n      |> count\n      |> each show\nhelper.register(register)\nkafka("orders").send(1)\nprint(read("private.txt"))\n', REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\n"


@pytest.mark.parametrize("step", ["window 0.0001s", "sliding 0.0001s", "session 0.0001s"])
def test_submillisecond_windows_are_rejected(tmp_path, backend, step):
    result = execute(tmp_path, backend, 'kafka("orders")\n  |> ' + step + '\n')
    assert result.returncode == 1, result.stdout.decode()

@pytest.mark.parametrize("step", ["window 1m", "session 1m", "sliding 1m"])
def test_window_permissions_include_earlier_and_later_messages(tmp_path, backend, step):
    helper = 'pub fn publish(stamp: any):\n    kafka("orders").send(2, at: stamp)\n'
    result = execute(tmp_path, backend, '''use helper
fn show(n):
    if n > 1:
        write("marker", "BAD")?
kafka("orders")
  |> STEP
  |> count
  |> each show
start = time(2026, 10, 24, 9, 0, zone: "UTC")
kafka("orders").send(1, at: start)
helper.publish(start + 10s)
kafka("orders").send(3, at: start + 2m)
'''.replace('STEP', step), helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_security_error_in_deferred_map_stops_following_steps(tmp_path, backend):
    result = execute(tmp_path, backend, '''use helper
fn transform(row):
    write("marker", "BAD")?
    return row
fn show(row):
    print("AFTER")
fn register():
    kafka("orders")
      |> sliding 1m
      |> map transform
      |> each show
helper.register(register)
kafka("orders").send(1)
print("CONTINUED")
''', REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"AFTER" not in result.stdout
    assert b"CONTINUED\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_user_function_return_boundary_still_blocks_pipeline_callables(tmp_path, backend):
    helper = 'pub fn build() -> any:\n    return [1] |> map [read]\n'
    result = execute(tmp_path, backend, 'use helper\nprint(helper.build())\n', helper, [["allow", "read", "."]])
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
