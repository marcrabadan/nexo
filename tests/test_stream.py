"""nexo.stream (section 19, D117) outside tests: programs talk through the
local broker of .nexo/streams, whichever phase runs each of them."""
import json
import os
import signal
import subprocess
import time

import pytest

from pathlib import Path
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


PARTITIONED_PRODUCER = """jobs = kafka("jobs", partitions: 3)
for i in range(1, 10):
    who = ["f", "a", "b"][i % 3]
    jobs.send({"n": i}, key: who)
"""

PARTITIONED_CONSUMER = """when kafka("jobs") as msg:
    answer = http.get("{base}/{msg.partition}/{msg.id}")?
    print(msg.partition, msg.key, msg.id, msg.json()["n"], answer.text)
"""


class Slow:
    """A server that answers after a moment and counts the requests it
    holds at once."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.lock = threading.Lock()
        self.now = self.most = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with outer.lock:
                    outer.now += 1
                    outer.most = max(outer.most, outer.now)
                time.sleep(0.3)
                with outer.lock:
                    outer.now -= 1
                data = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_partitions_are_handled_in_parallel_each_in_order(phase, tmp_path):
    slow = Slow()
    (tmp_path / "producer.nexo").write_text(PARTITIONED_PRODUCER, encoding="utf-8")
    (tmp_path / "consumer.nexo").write_text(PARTITIONED_CONSUMER.replace("{base}", slow.base), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    assert subprocess.run([*phase[1], "producer.nexo"], cwd=tmp_path, env=env, timeout=120).returncode == 0
    streams = tmp_path / ".nexo" / "streams" / "kafka"
    assert (streams / "jobs.partitions").read_text() == "3\n"
    proc = start(phase[1], ["consumer.nexo"], tmp_path)
    try:
        for p in range(3):
            wait_for(streams / f"jobs.{p}.consumer.offset", "3")
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        slow.server.shutdown()
    # each partition in order; what the tasks print comes in partition order
    assert out.decode() == ("0 f 1 3 ok\n0 f 2 6 ok\n0 f 3 9 ok\n"
                            "1 a 1 1 ok\n1 a 2 4 ok\n1 a 3 7 ok\n"
                            "2 b 1 2 ok\n2 b 2 5 ok\n2 b 3 8 ok\n")
    # phase 0 runs the tasks one after the other (D52); phase 1 at once
    # The native scheduler is capped by available_parallelism, including cgroup
    # CPU quotas. Three partitions do not imply three available worker threads.
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    quota = Path("/sys/fs/cgroup/cpu.max")
    if quota.exists():
        amount, period = quota.read_text().split()
        if amount != "max":
            cpus = min(cpus, max(1, int(amount) // int(period)))
    assert slow.most == (1 if phase[0] == "interpreter" else min(3, cpus))
