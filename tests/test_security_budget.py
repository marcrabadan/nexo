"""Process-wide runtime resource admission in both Shield engines."""
import json

import pytest
from phases import commands, run


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def execute(tmp_path, backend, body, *, shield=True, memory=False, helper=None):
    (tmp_path / "main.nexo").write_text('policy:\n    allow read "."\n    allow write "."\n' + body)
    if helper:
        (tmp_path / "helper.nexo").write_text(helper)
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": {"helper.nexo": [["allow", "write", "."]]} if helper else {}}))
    options = ["security", "run", "--dependencies", "permissions.json"] if shield else []
    if memory:
        options += ["--streams=memory"]
    result = run(backend, [*options, "main.nexo"], tmp_path, timeout=40)
    assert not (tmp_path / ".nexo" / "streams").exists()
    return result


def denied(result):
    assert result.returncode == 1, result.stdout.decode()
    assert b"process resource budget was exceeded" in result.stdout
    assert b"AFTER\n" not in result.stdout


@pytest.mark.parametrize("count, allowed", [(4096, True), (4097, False), (1000000000, False)])
def test_parallel_submission_bound_before_execution(tmp_path, backend, count, allowed):
    result = execute(tmp_path, backend, f'''results = parallel for n in 1..{count}, max: 1:
    return n
print("AFTER")
''')
    if allowed:
        assert result.returncode == 0, result.stdout.decode()
        assert result.stdout == b"AFTER\n"
    else:
        denied(result)


def test_parallel_budgets_are_shared_with_spawn_and_cannot_be_caught(tmp_path, backend):
    result = execute(tmp_path, backend, '''use nexo.sync
fn child():
    print("CHILD")
results = parallel for n in 1..4096, max: 1:
    return n
try:
    spawn child()
else e:
    print("CAUGHT")
print("AFTER")
''')
    denied(result)
    assert b"CHILD\n" not in result.stdout
    assert b"CAUGHT\n" not in result.stdout


def test_nested_parallel_does_not_get_fresh_budget(tmp_path, backend):
    result = execute(tmp_path, backend, '''results = parallel for n in 1..64, max: 1:
    inner = parallel for m in 1..64, max: 1:
        return m
    return n
print("AFTER")
''')
    denied(result)


def test_parallel_block_and_for_share_submission_budget(tmp_path, backend):
    result = execute(tmp_path, backend, '''results = parallel for n in 1..4095, max: 1:
    return n
parallel:
    print("BLOCK")
    print("BLOCK")
print("AFTER")
''')
    denied(result)
    assert b"BLOCK\n" not in result.stdout


def test_spawned_threads_have_process_wide_live_limit(tmp_path, backend):
    result = execute(tmp_path, backend, '''use nexo.sync
fn child(gate):
    gate.receive()
gate = channel()
for n in 1..33:
    spawn child(gate)
print("AFTER")
''')
    denied(result)


@pytest.mark.parametrize("kind", ["channels", "channel_slots"])
def test_channel_admission_is_cumulative_even_when_handles_are_discarded(tmp_path, backend, kind):
    body = 'for n in 1..257:\n    channel()\n' if kind == "channels" else 'channel(size: 32768)\nchannel(size: 32768)\nchannel(size: 1)\n'
    denied(execute(tmp_path, backend, 'use nexo.sync\n' + body + 'print("AFTER")\n'))


def test_channel_capacity_at_limit_can_be_admitted(tmp_path, backend):
    result = execute(tmp_path, backend, 'use nexo.sync\nchannel(size: 65536)\nprint("AFTER")\n')
    assert result.returncode == 0, result.stdout.decode()


@pytest.mark.parametrize("kind", ["events", "stream_handlers", "queries", "unregistered_steps"])
def test_registration_budget_is_cumulative_across_all_registries(tmp_path, backend, kind):
    # Prime the same budget through events, then cross its boundary through
    # another registry. Even an unregistered deferred query consumes a slot.
    actions = {"events": 'on changed():\n    print("BAD")\n',
               "stream_handlers": 'when kafka("orders") as msg:\n    print("BAD")\n',
               "queries": 'kafka("orders")\n  |> count\n  |> each show\n',
               "unregistered_steps": 'kafka("orders")\n  |> window 1m\n'}
    body = 'fn show(n):\n    print("BAD")\nfor n in 1..1024:\n    on changed():\n        print("BAD")\n' + actions[kind] + 'print("AFTER")\n'
    denied(execute(tmp_path, backend, body, memory=True))


def test_registrations_from_dependency_use_the_same_process_budget(tmp_path, backend):
    helper = 'pub fn register():\n    on changed():\n        print("BAD")\n'
    denied(execute(tmp_path, backend, 'use helper\nfor n in 1..1024:\n    helper.register()\non changed():\n    print("BAD")\nprint("AFTER")\n', helper=helper))


@pytest.mark.parametrize("budget", ["keys", "key_bytes", "key_utf8"])
def test_key_budget_is_checked_before_handler_side_effects(tmp_path, backend, budget):
    if budget == "keys":
        # The 4096th admission runs the first handler of message 1366. The
        # second must fail before its callback. Only that second writes.
        body = '''when kafka("orders") as msg, key: msg.id:
    print("")
for n in 1..2:
    when kafka("orders") as msg, key: msg.id:
        if msg.id == 1366:
            write("marker", "BAD")?
for n in 1..1366:
    kafka("orders").send(n)
'''
    else:
        (tmp_path / "key1.txt").write_text("x" * 600000 if budget == "key_bytes" else "é" * 300000)
        (tmp_path / "key2.txt").write_text("y" * 600000 if budget == "key_bytes" else "ø" * 300000)
        body = '''when kafka("orders") as msg, key: msg.text():
    if msg.id == 2:
        write("marker", "BAD")?
kafka("orders").send(read("key1.txt"))
kafka("orders").send(read("key2.txt"))
'''
    denied(execute(tmp_path, backend, body + 'print("AFTER")\n', memory=True))
    assert not (tmp_path / "marker").exists()


def test_duplicate_keys_do_not_consume_new_admissions(tmp_path, backend):
    # 16 handlers x 300 messages would exceed 4096 keys if duplicates charged.
    result = execute(tmp_path, backend, '''for n in 1..16:
    when kafka("orders") as msg, key: msg.text():
        print("once")
for n in 1..300:
    kafka("orders").send("same")
''', memory=True)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"once\n" * 16


@pytest.mark.parametrize("kind", ["registrations", "channels", "tasks"])
def test_ordinary_runtime_keeps_its_existing_limits(tmp_path, backend, kind):
    body = {'registrations': 'for n in 1..1025:\n    on changed():\n        print("unused")\n',
            'channels': 'use nexo.sync\nfor n in 1..257:\n    channel()\n',
            'tasks': 'results = parallel for n in 1..4097, max: 1:\n    return n\n'}[kind]
    result = execute(tmp_path, backend, body + 'print("AFTER")\n', shield=False)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"AFTER\n"


def test_parallel_can_run_on_caller_when_spawn_uses_all_thread_slots(tmp_path, backend):
    result = execute(tmp_path, backend, '''use nexo.sync
fn child(gate):
    gate.receive()
gate = channel(size: 32)
for n in 1..32:
    spawn child(gate)
results = parallel for n in [1, 2, 3], max: 3:
    return n
print(results)
for n in 1..32:
    gate.send("go")
''')
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"[1, 2, 3]\n"


def test_spawned_registration_cannot_reset_budget_on_return(tmp_path, backend):
    result = execute(tmp_path, backend, '''use nexo.sync
fn register(done):
    repeat 1024:
        on changed():
            pass
    done.send("ready")
done = channel()
spawn register(done)
done.receive()
on changed():
    pass
print("AFTER")
''')
    denied(result)
