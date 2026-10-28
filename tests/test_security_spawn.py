"""Process-lifetime spawned tasks inherit immutable security ceilings."""
import json
from pathlib import Path

import pytest
from phases import commands, run


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def execute(tmp_path, backend, source, helper=None, permissions=()):
    (tmp_path / "main.nexo").write_text(source)
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    if helper is not None:
        (tmp_path / "helper.nexo").write_text(helper)
    modules = {"helper.nexo": list(permissions)} if helper is not None else {}
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": modules}))
    return run(backend, ["security", "run", "--dependencies", "permissions.json", "main.nexo"], tmp_path, timeout=15)


HELPER = '''use nexo.sync
pub fn launch(callback: any, ready: any, release: any):
    spawn callback(ready, release)
'''
ROOT = '''policy:
    allow read "."
    allow write "."
use nexo.sync
use helper
fn callback(ready, release) -> str!:
    ready.send("started")
    release.receive()
    print(read("TARGET")?)
    return "done"
ready = channel()
release = channel()
helper.launch(callback, ready, release)
ready.receive()
print(read("private.txt"))
release.send("continue")
'''


def test_spawn_preserves_caller_after_the_launcher_returns(tmp_path, backend):
    source = ROOT.replace("TARGET", "public.txt")
    result = execute(tmp_path, backend, source, HELPER, [["allow", "read", "public.txt"]])
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"PRIVATE\nPUBLIC\n"


def test_spawn_cannot_regain_root_permissions_after_return(tmp_path, backend):
    source = ROOT.replace("TARGET", "private.txt")
    result = execute(tmp_path, backend, source, HELPER, [["allow", "read", "public.txt"]])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    # The root printed once; the restricted child must never print it again.
    assert result.stdout.count(b"\nPRIVATE\n") + result.stdout.startswith(b"PRIVATE\n") <= 1


def test_nested_spawn_keeps_original_caller_ceiling(tmp_path, backend):
    helper = '''use nexo.sync
pub fn launch(callback: any, ready: any, release: any):
    spawn second(callback, ready, release)
fn second(callback, ready, release):
    spawn callback(ready, release)
'''
    result = execute(tmp_path, backend, ROOT.replace("TARGET", "private.txt"), helper, [["allow", "read", "public.txt"]])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout


def test_program_waits_for_descendants_without_explicit_channel_join(tmp_path, backend):
    source = '''policy:
    allow write "result.txt"
use nexo.sync
fn child() -> str!:
    write("result.txt", "FINISHED")?
    return "done"
fn parent():
    spawn child()
spawn parent()
'''
    result = execute(tmp_path, backend, source)
    assert result.returncode == 0, result.stdout.decode()
    assert (tmp_path / "result.txt").read_text() == "FINISHED"


@pytest.mark.parametrize("value", ["read", "[read]", '{"nested": [read]}', "channel()"])
def test_channels_cannot_export_unsealed_authority(tmp_path, backend, value):
    source = f'''policy:
    allow read "."
use nexo.sync
c = channel(size: 1)
c.send({value})
print("ESCAPED")
'''
    result = execute(tmp_path, backend, source)
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert b"\nESCAPED\n" not in result.stdout


@pytest.mark.parametrize("statement", ['shared fn_handle = read', 'shared box = {"fn": read}', 'shared xs = []\nxs.add(read)', 'shared box = {}\nbox["fn"] = read'])
def test_shared_state_cannot_export_unsealed_authority(tmp_path, backend, statement):
    source = 'policy:\n    allow read "."\nuse nexo.sync\n' + statement + '\nprint("ESCAPED")\n'
    result = execute(tmp_path, backend, source)
    assert result.returncode == 1, result.stdout.decode()
    assert b"cannot export callable" in result.stdout
    assert b"\nESCAPED\n" not in result.stdout


def test_shared_plain_data_and_select_remain_usable(tmp_path, backend):
    source = '''policy:
    deny shell
use nexo.sync
shared hits = {"n": 0}
fn worker(done):
    hits["n"] += 1
    done.send({"value": [1, 2, 3]})
done = channel(size: 1)
spawn worker(done)
select:
    result from done: print(result)
    after 5s: print("TIMEOUT")
print(hits)
'''
    result = execute(tmp_path, backend, source)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b'{"value": [1, 2, 3]}\n{"n": 1}\n'


def test_spawned_security_violation_is_not_catchable(tmp_path, backend):
    source = '''policy:
    allow read "."
use nexo.sync
use helper
fn callback(ready, release):
    ready.send("started")
    release.receive()
    try:
        print(read("private.txt"))
    else e:
        print("CAUGHT")
ready = channel()
release = channel()
helper.launch(callback, ready, release)
ready.receive()
release.send("go")
'''
    result = execute(tmp_path, backend, source, HELPER, [])
    assert result.returncode == 1, result.stdout.decode()
    assert b"active dependency context" in result.stdout
    assert b"\nCAUGHT\n" not in result.stdout


def test_linux_profile_still_rejects_spawn_before_execution(tmp_path, backend):
    (tmp_path / "main.nexo").write_text('policy:\n    deny shell\nuse nexo.sync\nfn idle():\n    pass\nspawn idle()\nprint("EXECUTED")\n')
    result = run(backend, ["security", "run", "--sandbox=linux", "main.nexo"], tmp_path)
    assert result.returncode == 1, result.stdout.decode()
    assert b"does not support event loops" in result.stdout
    assert b"\nEXECUTED\n" not in result.stdout
