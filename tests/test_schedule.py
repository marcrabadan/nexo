"""Scheduled tasks (section 4, D208) in both phases: a program with
`every` runs its tasks when their time comes until it is stopped, an
error that leaves a task is shown and the next turn comes, a mistake
stops the program, and tasks run next to a server. The next time of day
is computed by nexo/stdlib/schedule.py here and by the same cases in
compiler/runtime/src/time.rs (cargo test)."""
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request

import pytest

from nexo.stdlib.schedule import next_at
from nexo.stdlib.clock import wall_to_ms
from phases import ROOT, commands

PHASES = commands()


def watch(phase, folder, done, env=None, timeout=45):
    """Run task.nexo until done(lines) holds or it ends; its lines and its
    exit status (None when it had to be stopped)."""
    full = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **(env or {}))
    proc = subprocess.Popen([*phase[1], "task.nexo"], cwd=folder, env=full, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    lines: list = []
    reader = threading.Thread(target=lambda: lines.extend(l.decode().rstrip("\n") for l in iter(proc.stdout.readline, b"")),
                              daemon=True)
    reader.start()
    end = time.time() + timeout
    while proc.poll() is None and not done(lines) and time.time() < end:
        time.sleep(0.1)
    status = proc.poll()
    if status is None:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    reader.join(timeout=30)
    return lines, status


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_tasks_run_and_an_error_does_not_stop_them(phase, tmp_path):
    (tmp_path / "task.nexo").write_text('''var n = 0

fn tick():
    n += 1
    print("tick {n}")
    if n == 2:
        fail("the second one fails")

every 1s:
    tick()?

every monday at 08:00:
    print("monday")

print("top level done")
''')
    lines, status = watch(phase, tmp_path, lambda seen: "tick 3" in seen)
    assert status is None
    assert lines[:9] == [
        "top level done", "Scheduled tasks: 2 (Ctrl-C to stop)", "tick 1", "tick 2",
        "Error in task.nexo, line 7:", '  fail("the second one fails")', "  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^",
        "Unhandled error: the second one fails. Handle it with try and else e.", "tick 3"]


@pytest.mark.parametrize("server", [False, True], ids=["alone", "with a server"])
@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_a_mistake_in_a_task_stops_the_program(phase, server, tmp_path):
    web = 'server api:\n    get "/"():\n        return "here"\n\n' if server else ""
    (tmp_path / "task.nexo").write_text(web + 'word = "x"\n\nevery 1s:\n    print("once")\n    print(int(word))\n')
    lines, status = watch(phase, tmp_path, lambda seen: False, env={"PORT": str(free_port()), "NEXO_WEB_LOCAL": "1"})
    assert status == 1
    assert "once" in lines and lines.count("once") == 1
    assert lines[-1] == "int: does not convert text; use validate(x, as: int)?. Check the arguments of int."


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_tasks_run_next_to_a_server(phase, tmp_path):
    port = free_port()
    (tmp_path / "task.nexo").write_text('''var turns = 0

every 1s:
    turns += 1

server api:
    get "/turns"():
        return {"turns": turns}
''')
    answers: list = []

    def done(seen):
        if not any("listens on" in line for line in seen):
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/turns", timeout=5) as r:
                answers.append(r.read().decode())
        except OSError:
            return False
        return answers[-1] not in ('{"turns":0}', '{"turns":1}')

    lines, status = watch(phase, tmp_path, done, env={"PORT": str(port), "NEXO_WEB_LOCAL": "1"})
    assert status is None
    assert sorted(lines[:2]) == ["Scheduled tasks: 1 (Ctrl-C to stop)", f"api listens on http://127.0.0.1:{port}"]
    assert answers[-1] in ('{"turns":2}', '{"turns":3}')


def ms(zone, *fields) -> int:
    return wall_to_ms(*fields, 0, 0, zone)


def test_the_next_time_of_day():
    """2026-11-16 is a Monday. The same cases are in time.rs."""
    monday_noon = ms("UTC", 2026, 11, 16, 12, 0)
    assert next_at(monday_noon, "day", 13 * 60, "UTC") == ms("UTC", 2026, 11, 16, 13, 0)
    assert next_at(monday_noon, "day", 9 * 60, "UTC") == ms("UTC", 2026, 11, 17, 9, 0)
    assert next_at(monday_noon, "day", 12 * 60, "UTC") == ms("UTC", 2026, 11, 17, 12, 0)
    assert next_at(monday_noon, "monday", 13 * 60, "UTC") == ms("UTC", 2026, 11, 16, 13, 0)
    assert next_at(monday_noon, "monday", 8 * 60, "UTC") == ms("UTC", 2026, 11, 23, 8, 0)
    assert next_at(monday_noon, "sunday", 8 * 60, "UTC") == ms("UTC", 2026, 11, 22, 8, 0)
    # the clock of the zone, not UTC's: 09:00 in Madrid is 08:00 UTC in winter
    assert next_at(monday_noon, "day", 9 * 60, "Europe/Madrid") == ms("UTC", 2026, 11, 17, 8, 0)
    # and 07:00 UTC after the clocks go forward on 2027-03-28
    saturday = ms("UTC", 2027, 3, 27, 12, 0)
    assert next_at(saturday, "day", 9 * 60, "Europe/Madrid") == ms("UTC", 2027, 3, 28, 7, 0)
