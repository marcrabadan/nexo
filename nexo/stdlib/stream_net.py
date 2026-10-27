"""nexo.stream on brokers on the network (section 19, D169): Kafka
(`kafka("orders", servers: "host:9092")`, kafka.py), Azure Event Hubs
through its Kafka endpoint (`eventhub("clicks", namespace: "shop")`),
RabbitMQ (`rabbit("orders", url: "amqps://user@host/vhost")`, amqp.py) and
WebSocket servers (`websocket("wss://host/path")`, websocket.py)."""
from __future__ import annotations

import os

from ..types import failure
from .kafka import Client, KafkaError


def broker_failure(call, stream, reason: str):
    return failure("NetworkError", call.span, key="NetworkError.broker", stream=stream.repr(), reason=reason)


def backend(stream):
    if stream.kind == "kafka":
        return KAFKA
    if stream.kind == "eventhub":
        return EVENTHUB
    if stream.kind == "rabbit":
        from .amqp import RABBIT
        return RABBIT
    from .websocket import WEBSOCKET
    return WEBSOCKET


def servers_of(text: str) -> tuple[str | None, list]:
    """(user, [(host, port)]) of `user@host:9092,host2:9092`."""
    user = None
    out = []
    for part in text.split(","):
        part = part.strip()
        if "@" in part:
            user, part = part.rsplit("@", 1)
        host, _, port = part.rpartition(":")
        if not host:
            host, port = port, "9092"
        out.append((host.strip("[]"), int(port)))
    return user, out


class Kafka:
    """A Kafka cluster: its partitions are the stream's, each read from
    the offset the consumer group committed (from the first message for a
    new group); a message's id is its offset."""
    log = True

    def __init__(self):
        self.clients: dict = {}

    def connect(self, call, stream):
        user, servers = servers_of(stream.where)
        sasl = None
        if stream.secret is not None:
            from .crypto import text_or_secret
            password = text_or_secret(call, "key", stream.secret)
            sasl = ("PLAIN", f"\0{user or ''}\0{password}".encode())
        return Client(servers, sasl)

    def client(self, call, stream) -> Client:
        if stream.ident() not in self.clients:
            self.clients[stream.ident()] = self.connect(call, stream)
        return self.clients[stream.ident()]

    def host(self, stream) -> str:
        return servers_of(stream.where)[1][0][0]

    def run(self, call, stream, what):
        try:
            return what(self.client(call, stream))
        except KafkaError as exc:
            if exc.code is None:
                # a connection that is lost is made again next time
                self.clients.pop(stream.ident(), None)
            raise broker_failure(call, stream, exc.reason)

    def partitions(self, call, stream) -> int:
        return self.run(call, stream, lambda c: c.metadata(stream.name))

    def start(self, call, stream, partition):
        from .stream import group

        def first(c):
            at = c.committed(group(), stream.name, [partition]).get(partition, -1)
            return (at if at >= 0 else c.earliest(stream.name, partition)) - 1
        return self.run(call, stream, first)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import Message
        records = self.run(call, stream, lambda c: c.fetch(stream.name, partition, position + 1, 100))
        return [Message(stream, offset, ts, value.decode("utf-8", "replace"),
                        None if key is None else key.decode("utf-8", "replace"), partition, offset - 1)
                for offset, ts, key, value in records]

    def done(self, call, stream, partition, messages, position) -> None:
        from .stream import group, kept
        at = kept(stream, partition, position)
        self.run(call, stream, lambda c: c.commit(group(), stream.name, {partition: int(at) + 1}))

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import Message, partition_of
        topic = name or stream.name

        def produce(c):
            count = c.counts.get(topic) or c.metadata(topic)
            p = partition_of(key, data, count)
            k = None if key is None else key.encode()
            return p, c.produce(topic, p, [(k, data.encode(), ms)])
        p, offset = self.run(call, stream, produce)
        return Message(stream, offset, ms, data, key, p)

    def dead(self, call, message) -> str:
        target = message.stream.name + ".dead"
        self.send(call, message.stream, message.data, message.ms, message.key, target)
        return target


class EventHub(Kafka):
    """Azure Event Hubs through its Kafka endpoint (port 9093, TLS), signed
    in with a token of Azure AD (OAUTHBEARER); NEXO_EVENTHUB_SERVERS points
    it elsewhere (an emulator)."""

    def address(self, stream) -> str:
        return os.environ.get("NEXO_EVENTHUB_SERVERS") or f"{stream.where}.servicebus.windows.net:9093"

    def host(self, stream) -> str:
        return servers_of(self.address(stream))[1][0][0]

    def connect(self, call, stream):
        from .stream_cloud import azure_token
        token = azure_token(call, stream, f"https://{stream.where}.servicebus.windows.net/.default")
        return Client(servers_of(self.address(stream))[1], ("OAUTHBEARER", f"n,,\x01auth=Bearer {token}\x01\x01".encode()))


KAFKA, EVENTHUB = Kafka(), EventHub()
