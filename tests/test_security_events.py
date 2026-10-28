"""Synchronous event dispatch intersects registration and emitter authority."""
import json

import pytest
from phases import commands, run, compiler


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def execute(tmp_path, backend, body, helper=None, permissions=(), configured=True):
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    if helper is not None:
        (tmp_path / "helper.nexo").write_text(helper)
    modules = {"helper.nexo": list(permissions)} if helper is not None else {}
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": modules}))
    source = 'policy:\n    allow read "."\n    allow write "."\n' + body
    (tmp_path / "main.nexo").write_text(source)
    options = ["--dependencies", "permissions.json"] if configured else []
    return run(backend, ["security", "run", *options, "main.nexo"], tmp_path, timeout=20)


REGISTER = 'pub fn register(callback: any):\n    callback()\n'


def test_registered_handler_retains_caller_after_registration_returns(tmp_path, backend):
    body = '''use helper
fn register():
    on changed():
        print(read("public.txt"))
helper.register(register)
emit changed()
print(read("private.txt"))
'''
    result = execute(tmp_path, backend, body, REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\n"


def test_root_handler_cannot_lose_its_registration_ceiling(tmp_path, backend):
    body = '''use helper
fn register():
    on changed():
        write("marker", "BAD")
helper.register(register)
emit changed()
'''
    result = execute(tmp_path, backend, body, REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_privileged_root_handler_cannot_widen_emitter_permissions(tmp_path, backend):
    helper = 'pub fn trigger():\n    emit changed()\n'
    body = '''use helper
on changed():
    write("marker", "BAD")
helper.trigger()
'''
    result = execute(tmp_path, backend, body, helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_each_handler_restores_the_previous_context(tmp_path, backend):
    body = '''use helper
fn register():
    on changed():
        print(read("public.txt"))
helper.register(register)
on changed():
    print(read("private.txt"))
emit changed()
print("root", read("private.txt"))
'''
    result = execute(tmp_path, backend, body, REGISTER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\nPRIVATE\nroot PRIVATE\n"


def test_nested_events_cannot_widen_outer_handler_ceiling(tmp_path, backend):
    body = '''use helper
on privileged():
    write("marker", "BAD")
fn register():
    on changed():
        emit privileged()
helper.register(register)
emit changed()
'''
    result = execute(tmp_path, backend, body, REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_caught_handler_error_restores_root_context(tmp_path, backend):
    body = '''use helper
fn register():
    on changed():
        fail("missing")
helper.register(register)
try:
    emit changed()
else e:
    print("caught")
print(read("private.txt"))
'''
    result = execute(tmp_path, backend, body, REGISTER, [])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"caught\nPRIVATE\n"


@pytest.mark.parametrize("configured", [False, True])
def test_handlers_registered_during_emit_wait_for_the_next_emit(tmp_path, backend, configured):
    body = '''on tick():
    print("first")
    on tick():
        print("later")
emit tick()
emit tick()
'''
    result = execute(tmp_path, backend, body, configured=configured)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"first\nfirst\nlater\n"


@pytest.mark.parametrize("value", ["read", "[read]", '{"items": [read]}'])
def test_event_payloads_cannot_publish_unsealed_authority(tmp_path, backend, value):
    body = f'''on consume(value):
    write("marker", "BAD")
emit consume({value})
'''
    result = execute(tmp_path, backend, body)
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_plain_event_payloads_remain_usable(tmp_path, backend):
    body = '''on consume(value):
    print(value["items"][0])
emit consume({"items": [7], "ok": true})
emit nobody_listens("safe")
'''
    result = execute(tmp_path, backend, body)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"7\n"


def test_ordinary_events_can_still_receive_callback_arguments(tmp_path, backend):
    body = '''on consume(action):
    print(action("public.txt"))
emit consume(read)
'''
    result = execute(tmp_path, backend, body, configured=False)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PUBLIC\n"


def test_spawn_registered_handler_keeps_the_original_context(tmp_path, backend):
    helper = '''use nexo.sync
pub fn launch(callback: any, done: any):
    spawn register(callback, done)
fn register(callback, done):
    callback()
    done.send("registered")
'''
    body = '''use nexo.sync
use helper
fn register():
    on changed():
        write("marker", "BAD")
done = channel()
helper.launch(register, done)
done.receive()
emit changed()
'''
    result = execute(tmp_path, backend, body, helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_spawn_emitter_keeps_the_original_context(tmp_path, backend):
    helper = '''use nexo.sync
pub fn launch():
    spawn trigger()
fn trigger():
    emit changed()
'''
    body = '''use helper
on changed():
    write("marker", "BAD")
helper.launch()
'''
    result = execute(tmp_path, backend, body, helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_event_security_violation_is_not_catchable(tmp_path, backend):
    body = '''use helper
fn register():
    on changed():
        try:
            write("marker", "BAD")
        else e:
            print("CAUGHT")
helper.register(register)
emit changed()
'''
    result = execute(tmp_path, backend, body, REGISTER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"\nCAUGHT\n" not in result.stdout
    assert not (tmp_path / "marker").exists()


def test_concurrent_registration_does_not_change_the_active_emit(tmp_path, backend):
    body = '''use nexo.sync
gate = channel()
release = channel()
done = channel()
on changed():
    gate.send("started")
    release.receive()
    print("first")
fn emitter(done):
    emit changed()
    done.send("finished")
spawn emitter(done)
gate.receive()
on changed():
    print("later")
release.send("go")
done.receive()
spawn emitter(done)
gate.receive()
release.send("go")
done.receive()
'''
    result = execute(tmp_path, backend, body)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"first\nfirst\nlater\n"


def test_linux_profile_supports_inline_events_or_fails_before_execution(tmp_path, backend):
    import subprocess
    binary = compiler()
    if binary is None:
        pytest.skip("requires native compiler and worker")
    from pathlib import Path
    worker = Path(binary).with_name("nexo-shield-worker")
    status = json.loads(subprocess.run([str(worker), "probe"], capture_output=True, text=True, check=True).stdout)
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "main.nexo").write_text('''policy:
    allow read "public.txt"
on changed(value):
    print(value, read("public.txt"))
emit changed("EVENT")
''')
    result = run(backend, ["security", "run", "--sandbox=linux", "main.nexo"], tmp_path)
    if status["supported"]:
        assert result.returncode == 0, result.stdout.decode()
        assert result.stdout == b"EVENT PUBLIC\n"
    else:
        assert result.returncode == 125, result.stdout.decode()
        assert b"EVENT PUBLIC" not in result.stdout


@pytest.mark.parametrize("declaration", ['when changed():\n    pass\n', 'every 1s:\n    pass\n', 'lifecycle start:\n    pass\n'])
def test_non_inline_event_modes_rejected_before_any_side_effect(tmp_path, backend, declaration):
    result = execute(tmp_path, backend, 'write("marker", "BAD")\n' + declaration)
    assert result.returncode == 1, result.stdout.decode()
    assert b"does not support event loops" in result.stdout
    assert not (tmp_path / "marker").exists()


def test_parallel_emits_keep_ordered_task_output(tmp_path, backend):
    body = '''on changed(value):
    print(value)
results = parallel for i in [1, 2, 3], max: 2:
    emit changed(i)
    return i
print(results)
'''
    result = execute(tmp_path, backend, body)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"1\n2\n3\n[1, 2, 3]\n"


def test_parallel_emitter_cannot_widen_its_callers_permissions(tmp_path, backend):
    helper = '''pub fn trigger() -> list[int]:
    results = parallel for i in [1, 2], max: 2:
        emit changed()
        return i
    return results
'''
    body = '''use helper
on changed():
    write("marker", "BAD")
helper.trigger()
'''
    result = execute(tmp_path, backend, body, helper, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert not (tmp_path / "marker").exists()
