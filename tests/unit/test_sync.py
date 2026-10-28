"""nexo.sync (section 5): channels, spawn, select and shared."""
from helpers import error_of, run

SYNC = "use nexo.sync\n"


def test_sync_words_need_use():
    # without `use nexo.sync`, spawn and shared are ordinary names
    assert run("spawn = 1\nshared = 2\nprint(spawn + shared)\n") == "3\n"
    assert error_of("c = channel()\n").key == "name_not_found"


def test_channels_carry_values_between_tasks():
    src = SYNC + (
        "fn producer(c):\n    for i in 1..3:\n        c.send(i)\n    c.close()\n"
        "c = channel()\nspawn producer(c)\nvar got = []\nfor x in c:\n    got.add(x)\nprint(got)\n"
    )
    assert run(src) == "[1, 2, 3]\n"


def test_values_travel_as_copies():
    src = SYNC + (
        "type T:\n    n: int\nfn change(t, out):\n    t.n = 2\n    out.send(t)\n"
        "t = T(1)\nout = channel(size: 1)\nspawn change(t, out)\nback = out.receive()\nback.n += 10\nprint(t, back)\n"
    )
    assert run(src) == "T(n: 1) T(n: 12)\n"


def test_receive_after_close_gives_none_and_send_fails():
    assert run(SYNC + "c = channel(size: 1)\nc.close()\nprint(c.receive())\n") == "none\n"
    assert error_of(SYNC + "c = channel(size: 1)\nc.close()\nc.send(1)\n").key == "channel_closed"


def test_typed_channels_check_what_they_send():
    assert error_of(SYNC + "c = channel[int](size: 1)\nc.send(\"x\")\n").key == "type_mismatch"


def test_select_takes_the_first_ready_arm_or_the_time_limit():
    src = SYNC + (
        "a = channel(size: 1)\nb = channel(size: 1)\nb.send(2)\n"
        "select:\n    x from a: print(\"a\", x)\n    x from b: print(\"b\", x)\n"
        "select:\n    x from a: print(\"a\", x)\n    after 1ms: print(\"idle\")\n"
    )
    assert run(src) == "b 2\nidle\n"


def test_shared_variables_can_be_changed_by_tasks():
    src = SYNC + "shared hits = {\"n\": 0}\nfn hit():\n    hits[\"n\"] = hits[\"n\"] + 1\nparallel for i in 1..5:\n    hit()\nprint(hits)\n"
    assert run(src) == '{"n": 5}\n'


def test_a_spawned_task_cannot_read_changing_variables():
    src = SYNC + "var n = 1\nfn f(c):\n    c.send(n)\nc = channel(size: 1)\nspawn f(c)\nprint(c.receive())\n"
    assert error_of_thread(src) == "spawn_read"


def error_of_thread(src):
    """A spawned task that fails ends the program: run it as a process."""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.nexo"
        f.write_text(src)
        p = subprocess.run([sys.executable, "-m", "nexo", str(f)], capture_output=True, text=True)
    assert p.returncode == 1
    return "spawn_read" if "spawned task cannot read" in p.stderr else p.stderr


def test_contracts_that_read_shared_variables_hold_the_lock(monkeypatch):
    # an ensure (and the old() value it uses) is read like a statement:
    # with the lock, so a task cannot change the variable halfway
    from nexo.stdlib import sync
    held = []
    enter, leave = sync.SharedLock.__enter__, sync.SharedLock.__exit__

    def counting_enter(self):
        held.append(1)
        return enter(self)

    monkeypatch.setattr(sync.SharedLock, "__enter__", counting_enter)
    monkeypatch.setattr(sync.SharedLock, "__exit__", leave)
    src = SYNC + (
        "shared xs = [1]\n"
        "fn f(n: int):\n    ensure len(xs) >= old(len(xs))\n    pass\n"
        "f(1)\n"
    )
    before = len(held)
    assert run(src) == ""
    # the old() snapshot and the ensure
    assert len(held) - before == 2


def test_failed_thread_creation_restores_task_accounting(monkeypatch):
    from nexo.stdlib import sync
    from nexo import security_budget
    monkeypatch.setenv("NEXO_SECURITY_ENFORCE", "1")
    budget = security_budget.Budget()
    monkeypatch.setattr(security_budget, "PROCESS", budget)
    def cannot_start(self):
        raise RuntimeError("thread creation rejected")
    monkeypatch.setattr(sync.threading.Thread, "start", cannot_start)
    error = error_of(SYNC + "fn idle():\n    pass\nspawn idle()\n")
    assert error.key == "spawn_failed"
    assert sync.STATE.alive == 1
    assert budget.threads == 0
    assert budget.used["tasks"] == 1
    sync.wait_all()  # must not wait for a task that never started
