"""nexo.stream (section 19, D117) outside tests: programs talk through the
local broker of .nexo/streams, whichever phase runs each of them."""
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
    assert err.decode().startswith('Escuchando kafka("notices")')
