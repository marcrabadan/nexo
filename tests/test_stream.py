"""nexo.stream (section 19, D117) outside tests: programs talk through the
local broker of .nexo/streams, whichever phase runs each of them."""
import json
import os
import signal
import subprocess
import time

import pytest

from phases import ROOT, commands

PRODUCER = 'kafka("notices").send({"user": 1, "text": "Order shipped"})\nkafka("notices").send("plain")\n'
CONSUMER = 'when kafka("notices") as msg:\n    print(msg.id, msg.text())\n'
OUT = '1 {"user":1,"text":"Order shipped"}\n2 plain\n3 {"user":1,"text":"Order shipped"}\n4 plain\n'


def start(prefix, args, cwd):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.Popen([*prefix, *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            start_new_session=True)


def wait_for(path, text, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text(encoding="utf-8").strip() == text:
            return
        time.sleep(0.1)
    raise AssertionError(f"{path} never reached {text}")


PAIRS = [(p, c) for p in commands() for c in commands()]


@pytest.mark.parametrize("producer,consumer", PAIRS, ids=lambda c: c[0])
def test_a_consumer_reads_what_producers_send(producer, consumer, tmp_path):
    (tmp_path / "producer.nexo").write_text(PRODUCER, encoding="utf-8")
    (tmp_path / "consumer.nexo").write_text(CONSUMER, encoding="utf-8")
    assert subprocess.run([*producer[1], "producer.nexo"], cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                          timeout=120).returncode == 0
    proc = start(consumer[1], ["consumer.nexo"], tmp_path)
    try:
        offset = tmp_path / ".nexo" / "streams" / "kafka" / "notices.consumer.offset"
        wait_for(offset, "2")
        assert subprocess.run([*producer[1], "producer.nexo"], cwd=tmp_path,
                              env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=120).returncode == 0
        wait_for(offset, "4")
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    assert out.decode() == OUT
    assert err.decode().startswith('Listening to kafka("notices")')


WINDOWED = """when kafka("clicks") as msg:
    print("seen", msg.id)

fn show(n):
    print("window", n)

kafka("clicks")
  |> window 1h
  |> count
  |> each show
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_stop_keeps_the_messages_of_an_open_window(phase, tmp_path):
    # the offset stays before a window that has not run yet, so a consumer
    # stopped (or crashed) before the window closes reads it again
    (tmp_path / "producer.nexo").write_text('kafka("clicks").send("home")\n', encoding="utf-8")
    (tmp_path / "consumer.nexo").write_text(WINDOWED, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    assert subprocess.run([*phase[1], "producer.nexo"], cwd=tmp_path, env=env, timeout=120).returncode == 0
    offset = tmp_path / ".nexo" / "streams" / "kafka" / "clicks.consumer.offset"
    for round_ in range(2):
        out = tmp_path / f"out{round_}.txt"
        with out.open("w") as f:
            proc = subprocess.Popen([*phase[1], "consumer.nexo"], cwd=tmp_path, stdout=f,
                                    stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            try:
                wait_for(out, "seen 1")
                wait_for(offset, "0")
            finally:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=30)
        # the window never ran, and the message is read again each time
        assert out.read_text() == "seen 1\n"


def test_producers_in_several_processes_get_unique_ids(tmp_path):
    # the log is locked while a producer counts its lines and appends, so
    # programs sending at once (in either phase) never share an id
    (tmp_path / "producer.nexo").write_text('for i in range(1, 41):\n    kafka("clicks").send(i)\n', encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    prefixes = [c[1] for c in commands()] * 3
    procs = [subprocess.Popen([*p, "producer.nexo"], cwd=tmp_path, env=env) for p in prefixes]
    for p in procs:
        assert p.wait(timeout=300) == 0
    lines = (tmp_path / ".nexo" / "streams" / "kafka" / "clicks.log").read_text().splitlines()
    ids = [json.loads(line)["id"] for line in lines]
    assert sorted(ids) == list(range(1, 40 * len(prefixes) + 1))
