"""nexo.stream on the clouds' services (D168), in both phases, against
tests/cloud_emulator.py: SQS and Kinesis (signed with SigV4), Service Bus
(with an Azure AD token) and Pub/Sub. A producer sends, a consumer handles
each message once and deletes it (or saves its position, Kinesis), and a
message that keeps failing goes to the dead letters."""
import json
import os
import signal
import subprocess
import time
import urllib.request

import pytest

import cloud_emulator as emu
from phases import ROOT, commands, run


@pytest.fixture(scope="module")
def cloud():
    server = emu.start()
    yield server, emu.environment(server)
    server.shutdown()


def create(server, what: dict) -> None:
    url = f"http://127.0.0.1:{server.server_address[1]}/__streams"
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(what).encode(), method="POST")).read()


def held(server) -> dict:
    url = f"http://127.0.0.1:{server.server_address[1]}/__streams"
    return json.loads(urllib.request.urlopen(url).read())


def consume(phase, folder, env, done, timeout=60) -> str:
    """Run consumer.nexo until done() holds; its output."""
    full = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", **env)
    proc = subprocess.Popen([*phase[1], "consumer.nexo"], cwd=folder, env=full, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        end = time.time() + timeout
        while not done() and time.time() < end:
            time.sleep(0.2)
        time.sleep(0.5)
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    return out.decode()


QUEUE_PRODUCER = """q = {source}
customer = "ana"
q.send({{"id": 1}}, key: customer)
q.send({{"id": 2}}, at: time(2026, 10, 26, 9, 0, zone: "UTC"))
q.send("bad")
"""

QUEUE_CONSUMER = """when {source} as msg:
    if msg.text() == "bad":
        fail("not an order")
    print(msg.json()["id"], msg.partition, msg.key, msg.time.year)
"""


@pytest.mark.parametrize("kind,option,dead", [("sqs", 'region: "eu-west-1"', "{q}-dead"),
                                              ("servicebus", 'namespace: "shop"', "{q}.dead")])
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_queue_of_a_cloud(cloud, phase, kind, option, dead, tmp_path):
    server, env = cloud
    q = f"orders-{phase[0]}"
    dead = dead.format(q=q)
    where = {"sqs": [q, dead]} if kind == "sqs" else {"servicebus": {"shop": [q, dead]}}
    create(server, where)
    source = f'{kind}("{q}", {option})'
    (tmp_path / "producer.nexo").write_text(QUEUE_PRODUCER.format(source=source))
    (tmp_path / "consumer.nexo").write_text(QUEUE_CONSUMER.format(source=source))
    assert run(phase[1], ["producer.nexo"], tmp_path, env=env).returncode == 0
    box = "sqs" if kind == "sqs" else "servicebus"
    name = q if kind == "sqs" else f"shop/{q}"
    out = consume(phase, tmp_path, env, lambda: not held(server)[box][name])
    lines = out.splitlines()
    assert lines[0] == f'Listening to {kind}("{q}") (Ctrl-C to stop)'
    assert lines[1:3] == [f"1 0 ana {time.localtime().tm_year}", "2 0 none 2026"]
    assert lines[3].endswith(f"fails after 4 tries and goes to {dead}: Unhandled error: not an order. "
                             "Handle it with try and else e.")
    dead_name = dead if kind == "sqs" else f"shop/{dead}"
    assert held(server)[box][dead_name] == ["bad"]


KINESIS_PRODUCER = """clicks = kinesis("{name}", region: "eu-west-1")
for who in ["ana", "bo", "ana", "cy", "bo"]:
    clicks.send({{"user": who}}, key: who)
"""

KINESIS_CONSUMER = """when kinesis("{name}", region: "eu-west-1") as msg:
    print(msg.partition, msg.key, msg.json()["user"])
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_shards_of_a_kinesis_stream(cloud, phase, tmp_path):
    server, env = cloud
    name = f"clicks-{phase[0]}"
    create(server, {"kinesis": {name: 2}})
    (tmp_path / "producer.nexo").write_text(KINESIS_PRODUCER.format(name=name))
    (tmp_path / "consumer.nexo").write_text(KINESIS_CONSUMER.format(name=name))
    assert run(phase[1], ["producer.nexo"], tmp_path, env=env).returncode == 0
    shards = held(server)["kinesis"][name]
    assert all(shards), "the keys of the test must fall on both shards"
    folder = tmp_path / ".nexo" / "streams" / "kinesis"

    def saved():
        return all((folder / f"{name}.{p}.consumer.offset").exists() for p in range(2))

    out = consume(phase, tmp_path, env, saved)
    # each shard in order, the shards in turn (one task per shard, D167)
    lines = out.splitlines()[1:]
    by_shard = [[line for line in lines if line.startswith(f"{p} ")] for p in range(2)]
    assert lines == by_shard[0] + by_shard[1]
    assert sorted(line.split()[2] for line in lines) == ["ana", "ana", "bo", "bo", "cy"]
    for p, shard in enumerate(shards):
        assert [line.split()[2] for line in by_shard[p]] == [json.loads(d)["user"] for d in shard]
    # a second run starts after the saved positions: nothing again
    again = consume(phase, tmp_path, env, lambda: True, timeout=3)
    assert again.splitlines()[1:] == []


PUBSUB_CONSUMER = """when pubsub("{name}", project: "shop-project") as msg:
    print(msg.text(), msg.key)
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_pubsub_topic(cloud, phase, tmp_path):
    server, env = cloud
    name = f"notices-{phase[0]}"
    create(server, {"pubsub": {"shop-project": [name]}})
    (tmp_path / "producer.nexo").write_text(
        f'k = "n1"\npubsub("{name}", project: "shop-project").send("shipped", key: k)\n')
    (tmp_path / "consumer.nexo").write_text(PUBSUB_CONSUMER.format(name=name))
    sub = f"projects/shop-project/subscriptions/{name}.consumer"
    state = {"sent": False}

    def done():
        # the subscription is made when the consumer starts; then a send
        subs = held(server)["pubsub"]
        if sub in subs and not state["sent"]:
            assert run(phase[1], ["producer.nexo"], tmp_path, env=env).returncode == 0
            state["sent"] = True
            return False
        return state["sent"] and subs.get(sub) == []

    out = consume(phase, tmp_path, env, done)
    assert out.splitlines()[1:] == ["shipped n1"]


MISSING = """try:
    sqs("nowhere", region: "eu-west-1").send("x")
else e:
    print(e.kind, e.message)
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_queue_that_does_not_exist(cloud, phase, tmp_path):
    (tmp_path / "app.nexo").write_text(MISSING)
    out = run(phase[1], ["app.nexo"], tmp_path, env=cloud[1]).stdout.decode()
    assert out == ('HTTPError sqs("nowhere"): the cloud answers 400: The specified queue does not exist.\n')
