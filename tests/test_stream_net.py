"""nexo.stream on brokers on the network (D169), in both phases, against
brokers written for the tests from each protocol's specification:
tests/kafka_broker.py (Kafka, and Event Hubs through its Kafka endpoint
with a token of tests/cloud_emulator.py), tests/amqp_broker.py (RabbitMQ)
and tests/ws_server.py (a WebSocket server). A producer sends, a consumer
handles each message once and saves where it is (Kafka) or acknowledges
it (RabbitMQ), and a message that keeps failing goes to the dead letters."""
import os
import signal
import subprocess
import threading
import time

import pytest

import amqp_broker
import cloud_emulator as emu
import kafka_broker
import ws_server
from nexo.stdlib.stream import partition_of
from phases import ROOT, commands, run

PHASES = commands()


def consume(phase, folder, env, done, timeout=60) -> str:
    """Run consumer.nexo until done() holds, or until `timeout` seconds
    after it started listening; its output."""
    full = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **env)
    proc = subprocess.Popen([*phase[1], "consumer.nexo"], cwd=folder, env=full, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    lines: list = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, b"")), daemon=True)
    reader.start()
    try:
        # a compiled consumer is compiled first, which takes longer on a busy machine
        start = time.time()
        while not lines and proc.poll() is None and time.time() < start + 120:
            time.sleep(0.1)
        end = time.time() + timeout
        while not done() and time.time() < end:
            time.sleep(0.2)
        time.sleep(0.5)
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
        reader.join(timeout=30)
    return b"".join(lines).decode()


FAILS = "fails after 4 tries and goes to {dead}: Unhandled error: no. Handle it with try and else e."

LOG_PRODUCER = """k = {source}
for who in ["ana", "bo", "cy", "ana"]:
    k.send({{"user": who}}, key: who)
k.send("bad")
"""

LOG_CONSUMER = """when {source} as msg:
    if msg.text() == "bad":
        fail("no")
    print(msg.partition, msg.id, msg.key, msg.json()["user"])
"""


def expected_log(lines, count):
    """What the consumer of LOG_PRODUCER prints: by partition, each in
    order (D167), the failure where its partition's turn comes."""
    by = {p: [] for p in range(count)}
    for who in ["ana", "bo", "cy", "ana"]:
        p = partition_of(who, "", count)
        by[p].append(f"{p} {len(by[p])} {who} {who}")
    return [line for p in range(count) for line in by[p]]


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_kafka_with_a_password(phase, tmp_path):
    broker = kafka_broker.start({"orders": 2, "orders.dead": 1}, users={"shop": "pw"})
    env = {"KAFKA_PASS": "pw"}
    source = f'kafka("orders", servers: "shop@localhost:{broker.port}", key: secret("KAFKA_PASS"))'
    (tmp_path / "producer.nexo").write_text(LOG_PRODUCER.format(source=source))
    (tmp_path / "consumer.nexo").write_text(LOG_CONSUMER.format(source=source))
    assert run(phase[1], ["producer.nexo"], tmp_path, env=env).returncode == 0
    held = [[(o, k, v) for o, _, k, v in part] for part in broker.topics["orders"]]
    assert sum(len(p) for p in held) == 5
    for p, part in enumerate(held):
        assert [o for o, _, _ in part] == list(range(len(part)))
        for _, k, v in part:
            assert partition_of(k and k.decode(), v.decode(), 2) == p

    def committed():
        return sum(broker.offsets.get(("consumer", "orders", p), 0) for p in range(2)) == 5

    out = consume(phase, tmp_path, env, committed)
    lines = out.splitlines()
    assert lines[0] == 'Listening to kafka("orders") (Ctrl-C to stop)'
    printed = [line for line in lines[1:] if "fails after" not in line]
    assert printed == expected_log(lines, 2)
    failed = [line for line in lines if "fails after" in line]
    assert len(failed) == 1 and failed[0].endswith(FAILS.format(dead="orders.dead"))
    assert [v for _, _, _, v in broker.topics["orders.dead"][0]] == [b"bad"]
    # a consumer of the same group starts where the last one stopped
    again = consume(phase, tmp_path, env, lambda: False, timeout=2)
    assert again.splitlines() == ['Listening to kafka("orders") (Ctrl-C to stop)']


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_kafka_refuses_a_wrong_password(phase, tmp_path):
    broker = kafka_broker.start({"orders": 1}, users={"shop": "pw"})
    (tmp_path / "p.nexo").write_text(
        f'k = kafka("orders", servers: "shop@localhost:{broker.port}", key: secret("KAFKA_PASS"))\nk.send("x")\n')
    r = run(phase[1], ["p.nexo"], tmp_path, env={"KAFKA_PASS": "wrong"})
    assert r.returncode == 1
    assert 'kafka("orders"): ' in r.stdout.decode()
    assert broker.topics["orders"] == [[]]


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_event_hubs_through_the_kafka_endpoint(phase, tmp_path):
    cloud = emu.start()
    try:
        broker = kafka_broker.start({"clicks": 1, "clicks.dead": 1}, tokens=[emu.AZURE_TOKEN])
        env = dict(emu.environment(cloud), NEXO_EVENTHUB_SERVERS=f"localhost:{broker.port}")
        source = 'eventhub("clicks", namespace: "shop")'
        (tmp_path / "producer.nexo").write_text(LOG_PRODUCER.format(source=source))
        (tmp_path / "consumer.nexo").write_text(LOG_CONSUMER.format(source=source))
        assert run(phase[1], ["producer.nexo"], tmp_path, env=env).returncode == 0
        out = consume(phase, tmp_path, env, lambda: broker.offsets.get(("consumer", "clicks", 0)) == 5)
    finally:
        cloud.shutdown()
    assert out.splitlines() == ['Listening to eventhub("clicks") (Ctrl-C to stop)', *expected_log(None, 1),
                                "clicks: message 4 " + FAILS.format(dead="clicks.dead")]


QUEUE_PRODUCER = """q = {source}
who = "ana"
q.send({{"id": 1}}, key: who)
q.send("bad")
q.send({{"id": 2}})
"""

QUEUE_CONSUMER = """when {source} as msg:
    if msg.text() == "bad":
        fail("no")
    print(msg.json()["id"], msg.key, msg.partition, msg.time.year)
"""


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_rabbitmq(phase, tmp_path):
    broker = amqp_broker.start({"shop": "pw"})
    env = {"RABBIT_PASS": "pw"}
    source = f'rabbit("orders", url: "amqp://shop@localhost:{broker.port}/", key: secret("RABBIT_PASS"))'
    (tmp_path / "producer.nexo").write_text(QUEUE_PRODUCER.format(source=source))
    (tmp_path / "consumer.nexo").write_text(QUEUE_CONSUMER.format(source=source))
    assert run(phase[1], ["producer.nexo"], tmp_path, env=env).returncode == 0
    assert [body for _, body in broker.queues["orders"]] == [b'{"id":1}', b"bad", b'{"id":2}']
    out = consume(phase, tmp_path, env, lambda: not broker.queues["orders"] and broker.queues.get("orders.dead"))
    year = time.localtime().tm_year
    assert out.splitlines() == ['Listening to rabbit("orders") (Ctrl-C to stop)', f"1 ana 0 {year}",
                                "orders: message 2 " + FAILS.format(dead="orders.dead"), f"2 none 0 {year}"]
    assert [body for _, body in broker.queues["orders.dead"]] == [b"bad"]


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_websocket(phase, tmp_path):
    server = ws_server.start(["first message", "bad", '{"n": 3}'])
    url = f"ws://localhost:{server.port}/feed"
    (tmp_path / "consumer.nexo").write_text(f'''when websocket("{url}") as msg:
    if msg.text() == "bad":
        fail("no")
    print(msg.id, msg.text())
    websocket("{url}").send("got {{msg.id}}")
''')
    out = consume(phase, tmp_path, {}, lambda: len(server.received) == 2)
    dead = f"localhost_{server.port}_feed.dead"
    assert out.splitlines() == [f'Listening to websocket("{url}") (Ctrl-C to stop)', "1 first message",
                                f"{url}: message 2 " + FAILS.format(dead=dead), '3 {"n": 3}']
    assert server.received == ["got 1", "got 3"]
    # a WebSocket keeps nothing: its dead letters go to the local broker
    assert (tmp_path / ".nexo" / "streams" / "websocket" / f"{dead}.log").read_text().count('"bad"') == 1


@pytest.mark.parametrize("program", [
    'websocket("feed")\n',
    'kafka("orders", key: secret("P"))\n',
    'websocket("ws://localhost:1/x", partitions: 2)\n',
    'sqs("orders", key: secret("P"))\n',
])
def test_source_errors_are_the_same_in_both_phases(program, tmp_path):
    (tmp_path / "p.nexo").write_text(program)
    outs = []
    for phase in PHASES:
        r = run(phase[1], ["p.nexo"], tmp_path)
        assert r.returncode == 1
        outs.append(r.stdout.decode())
    assert len(set(outs)) == 1, outs


POLICY = """policy:
    allow net "localhost"
    deny net "127.0.0.1"
k = kafka("orders", servers: "localhost:1,127.0.0.1:{port}")
k.send("x")
print("sent")
"""


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_the_policy_checks_every_broker_a_client_reaches(phase, tmp_path):
    # the first server is allowed; the next one, which answers, is not
    broker = kafka_broker.start({"orders": 1})
    (tmp_path / "p.nexo").write_text(POLICY.replace("{port}", str(broker.port)))
    out = run(phase[1], ["p.nexo"], tmp_path).stdout.decode()
    assert "The policy denies connecting to 127.0.0.1" in out and "sent" not in out
    assert broker.topics["orders"] == [[]]
