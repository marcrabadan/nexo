"""nexo.stream on the clouds' own services (section 19, D168): `sqs` and
`kinesis` on AWS (signed with SigV4), `servicebus` on Azure (with a token
of Azure AD) and `pubsub` on Google Cloud, each through its HTTP API, with
the credentials and addresses nexo.infra reads (infra_cloud.py).

A queue (SQS, Service Bus, Pub/Sub) is read with a lease and each message
is deleted (acknowledged) once it is handled; a log (Kinesis) is read from
the position the consumer group saved in .nexo/streams, like the local
broker. A message keeps the time it was sent with (`send(at:)`) and its
key in the attributes of the service, where it has them."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ..errors import NexoError
from ..types import failure
from .infra_cloud import error_text, need_env, sigv4

TIME, KEY = "nexo-time", "nexo-key"


def request(call, stream, method: str, url: str, headers: dict, body: bytes | None = None):
    """(status, headers, body); a network failure is NetworkError."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except (urllib.error.URLError, OSError) as e:
        from .http import network_reason
        raise failure("NetworkError", call.span, url=url, reason=network_reason(e))


def refused(call, stream, status: int, body: bytes):
    return failure("HTTPError", call.span, key="HTTPError.stream", stream=stream.repr(), status=status,
                   reason=error_text(body))


def compact(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def backend(stream):
    return {"sqs": SQS, "kinesis": KINESIS, "servicebus": SERVICEBUS, "pubsub": PUBSUB}[stream.kind]


# ---------------------------------------------------------------- AWS

class Aws:
    service = ""
    prefix = ""
    log = False

    def endpoint(self, stream) -> str:
        return (os.environ.get("AWS_ENDPOINT_URL") or f"https://{self.service}.{stream.where}.amazonaws.com").rstrip("/")

    def host(self, stream) -> str:
        return urllib.parse.urlsplit(self.endpoint(stream)).hostname or ""

    def call(self, call, stream, op: str, payload: dict) -> dict:
        url = self.endpoint(stream) + "/"
        body = compact(payload).encode()
        headers = {"Content-Type": "application/x-amz-json-1.0" if self.service == "sqs" else "application/x-amz-json-1.1",
                   "X-Amz-Target": f"{self.prefix}.{op}"}
        signed = sigv4("POST", url, headers, body, self.service, stream.where)
        status, _, data = request(call, stream, "POST", url, signed, body)
        if status >= 400:
            raise refused(call, stream, status, data)
        return json.loads(data.decode() or "{}")


class Sqs(Aws):
    """Amazon SQS through its JSON protocol: one partition; a message is
    deleted once it is handled."""
    service, prefix = "sqs", "AmazonSQS"

    def __init__(self):
        self.urls: dict = {}

    def url(self, call, stream, name: str | None = None) -> str:
        name = name or stream.name
        if (stream.where, name) not in self.urls:
            self.urls[(stream.where, name)] = self.call(call, stream, "GetQueueUrl", {"QueueName": name})["QueueUrl"]
        return self.urls[(stream.where, name)]

    def partitions(self, call, stream) -> int:
        self.url(call, stream)
        return 1

    def start(self, call, stream, partition):
        return None

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import Message
        attrs = {TIME: {"DataType": "Number", "StringValue": str(ms)}}
        if key is not None:
            attrs[KEY] = {"DataType": "String", "StringValue": key}
        payload = {"QueueUrl": self.url(call, stream, name), "MessageBody": data, "MessageAttributes": attrs}
        if (name or stream.name).endswith(".fifo"):
            # a FIFO queue keeps the order of each group: the key
            payload["MessageGroupId"] = key or "nexo"
            payload["MessageDeduplicationId"] = hashlib.sha256(f"{ms}:{data}".encode()).hexdigest()
        out = self.call(call, stream, "SendMessage", payload)
        return Message(stream, out.get("MessageId"), ms, data, key)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import Message
        url = self.url(call, stream)
        out = self.call(call, stream, "ReceiveMessage", {
            "QueueUrl": url, "MaxNumberOfMessages": 10, "WaitTimeSeconds": 1, "VisibilityTimeout": 60,
            "MessageAttributeNames": ["All"], "AttributeNames": ["SentTimestamp"]})
        messages = []
        for m in out.get("Messages") or []:
            attrs = m.get("MessageAttributes") or {}
            ms = attrs.get(TIME, {}).get("StringValue") or (m.get("Attributes") or {}).get("SentTimestamp") or 0
            key = attrs.get(KEY, {}).get("StringValue")
            messages.append(Message(stream, m["MessageId"], int(ms), m.get("Body", ""), key, 0, None,
                                    (url, m["ReceiptHandle"])))
        return messages

    def done(self, call, stream, partition, messages, position) -> None:
        for m in messages:
            url, receipt = m.receipt
            self.call(call, stream, "DeleteMessage", {"QueueUrl": url, "ReceiptHandle": receipt})

    def dead(self, call, message) -> None:
        # SQS names cannot have a dot: the dead letters go to <name>-dead
        name = message.stream.name
        dead = name[:-5] + "-dead.fifo" if name.endswith(".fifo") else name + "-dead"
        self.send(call, message.stream, message.data, message.ms, message.key, dead)
        return dead


class Kinesis(Aws):
    """Amazon Kinesis Data Streams: its shards are the partitions; the
    consumer group saves the sequence number it reached in each one."""
    service, prefix = "kinesis", "Kinesis_20131202"
    log = True

    def __init__(self):
        self.known: dict = {}

    def shards(self, call, stream) -> list:
        if stream.ident() not in self.known:
            out = self.call(call, stream, "ListShards", {"StreamName": stream.name})
            self.known[stream.ident()] = [s["ShardId"] for s in out.get("Shards") or []]
        return self.known[stream.ident()]

    def partitions(self, call, stream) -> int:
        return len(self.shards(call, stream))

    def path(self, stream, partition: int):
        from .stream import ROOT, group
        return ROOT / "kinesis" / f"{stream.name}.{partition}.{group()}.offset"

    def start(self, call, stream, partition):
        try:
            return self.path(stream, partition).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import Message
        # the shard is chosen by the partition key: the key, or the data
        part = key if key is not None else hashlib.sha256(data.encode()).hexdigest()[:32]
        out = self.call(call, stream, "PutRecord", {"StreamName": name or stream.name, "PartitionKey": part,
                                                    "Data": base64.b64encode(data.encode()).decode()})
        return Message(stream, out.get("SequenceNumber"), ms, data, key)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import Message
        shard = self.shards(call, stream)[partition]
        start = {"StreamName": stream.name, "ShardId": shard}
        if position is None:
            start["ShardIteratorType"] = "TRIM_HORIZON"
        else:
            start.update(ShardIteratorType="AFTER_SEQUENCE_NUMBER", StartingSequenceNumber=position)
        it = self.call(call, stream, "GetShardIterator", start)["ShardIterator"]
        out = self.call(call, stream, "GetRecords", {"ShardIterator": it, "Limit": 100})
        messages, before = [], position
        for r in out.get("Records") or []:
            data = base64.b64decode(r.get("Data", "")).decode("utf-8", "replace")
            ms = int(float(r.get("ApproximateArrivalTimestamp", 0)) * 1000)
            key = r.get("PartitionKey")
            messages.append(Message(stream, r["SequenceNumber"], ms, data, key, partition, before))
            before = r["SequenceNumber"]
        return messages

    def done(self, call, stream, partition, messages, position) -> None:
        from .stream import kept
        position = kept(stream, partition, position)
        if position is not None:
            path = self.path(stream, partition)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{position}\n", encoding="utf-8")

    def dead(self, call, message) -> None:
        self.send(call, message.stream, message.data, message.ms, message.key, message.stream.name + ".dead")
        return message.stream.name + ".dead"


# -------------------------------------------------------------- Azure

def azure_url(url: str) -> str:
    """A data-plane address, at NEXO_AZURE_ENDPOINT when it is set (an
    emulator): https://HOST/path becomes ENDPOINT/HOST/path."""
    endpoint = (os.environ.get("NEXO_AZURE_ENDPOINT") or "").rstrip("/")
    if endpoint and url.startswith("https://"):
        return f"{endpoint}/{url[8:]}"
    return url


class ServiceBus:
    """Azure Service Bus queues through REST: a message is read with a
    lock (peek-lock) and completed once it is handled."""
    log = False

    def __init__(self):
        self.tokens: dict = {}

    def base(self, stream, name: str | None = None) -> str:
        return azure_url(f"https://{stream.where}.servicebus.windows.net/{name or stream.name}")

    def host(self, stream) -> str:
        return urllib.parse.urlsplit(self.base(stream)).hostname or ""

    def token(self, call, stream) -> str:
        if "t" not in self.tokens:
            tenant, client, secret = need_env("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
            host = (os.environ.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com").rstrip("/")
            url = f"{host}/{tenant}/oauth2/v2.0/token"
            form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client,
                                           "client_secret": secret,
                                           "scope": "https://servicebus.azure.net/.default"}).encode()
            status, _, data = request(call, stream, "POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, form)
            if status >= 400:
                raise refused(call, stream, status, data)
            self.tokens["t"] = json.loads(data.decode())["access_token"]
        return self.tokens["t"]

    def partitions(self, call, stream) -> int:
        return 1

    def start(self, call, stream, partition):
        return None

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import Message
        headers = {"Authorization": "Bearer " + self.token(call, stream),
                   "Content-Type": "text/plain; charset=utf-8", TIME: str(ms)}
        if key is not None:
            headers[KEY] = compact(key)
        status, _, body = request(call, stream, "POST", self.base(stream, name) + "/messages", headers, data.encode())
        if status >= 400:
            raise refused(call, stream, status, body)
        return Message(stream, None, ms, data, key)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import Message
        messages = []
        for _ in range(10):
            status, headers, body = request(call, stream, "POST", self.base(stream) + "/messages/head?timeout=1",
                                            {"Authorization": "Bearer " + self.token(call, stream)}, b"")
            if status == 204:
                break
            if status >= 400:
                raise refused(call, stream, status, body)
            props = json.loads(headers.get("brokerproperties") or "{}")
            ms = int(headers.get(TIME) or 0)
            key = json.loads(headers[KEY]) if KEY in headers else None
            receipt = f"{self.base(stream)}/messages/{props.get('MessageId')}/{props.get('LockToken')}"
            messages.append(Message(stream, props.get("SequenceNumber"), ms, body.decode("utf-8", "replace"), key, 0,
                                    None, receipt))
        return messages

    def done(self, call, stream, partition, messages, position) -> None:
        for m in messages:
            status, _, body = request(call, stream, "DELETE", m.receipt,
                                      {"Authorization": "Bearer " + self.token(call, stream)})
            if status >= 400:
                raise refused(call, stream, status, body)

    def dead(self, call, message) -> None:
        self.send(call, message.stream, message.data, message.ms, message.key, message.stream.name + ".dead")
        return message.stream.name + ".dead"


# --------------------------------------------------------- Google Cloud

class PubSub:
    """Google Cloud Pub/Sub through REST: a topic, read through a
    subscription of the consumer group (`<topic>.<group>`, made when it is
    missing); a message is acknowledged once it is handled."""
    log = False

    def base(self) -> str:
        emulator = os.environ.get("PUBSUB_EMULATOR_HOST")
        if emulator:
            return f"http://{emulator}/v1"
        endpoint = (os.environ.get("NEXO_GCP_ENDPOINT") or "").rstrip("/")
        return f"{endpoint}/pubsub/v1" if endpoint else "https://pubsub.googleapis.com/v1"

    def host(self, stream) -> str:
        return urllib.parse.urlsplit(self.base()).hostname or ""

    def send_json(self, call, stream, method: str, path: str, payload) -> tuple:
        headers = {"Content-Type": "application/json"}
        if not os.environ.get("PUBSUB_EMULATOR_HOST"):
            token, = need_env("GOOGLE_OAUTH_ACCESS_TOKEN")
            headers["Authorization"] = "Bearer " + token
        return request(call, stream, method, f"{self.base()}/{path}", headers,
                       None if payload is None else compact(payload).encode())

    def json(self, call, stream, method, path, payload) -> dict:
        status, _, data = self.send_json(call, stream, method, path, payload)
        if status >= 400:
            raise refused(call, stream, status, data)
        return json.loads(data.decode() or "{}")

    def subscription(self, stream) -> str:
        from .stream import group
        return f"projects/{stream.where}/subscriptions/{stream.name}.{group()}"

    def partitions(self, call, stream) -> int:
        topic = f"projects/{stream.where}/topics/{stream.name}"
        status, _, data = self.send_json(call, stream, "PUT", self.subscription(stream),
                                         {"topic": topic, "ackDeadlineSeconds": 60})
        if status >= 400 and status != 409:
            raise refused(call, stream, status, data)
        return 1

    def start(self, call, stream, partition):
        return None

    def send(self, call, stream, data: str, ms: int, key, name: str | None = None):
        from .stream import Message
        attrs = {TIME: str(ms)}
        if key is not None:
            attrs[KEY] = key
        topic = f"projects/{stream.where}/topics/{name or stream.name}"
        out = self.json(call, stream, "POST", topic + ":publish",
                        {"messages": [{"data": base64.b64encode(data.encode()).decode(), "attributes": attrs}]})
        return Message(stream, (out.get("messageIds") or [None])[0], ms, data, key)

    def poll(self, call, stream, partition, position) -> list:
        from .stream import Message
        out = self.json(call, stream, "POST", self.subscription(stream) + ":pull", {"maxMessages": 100})
        messages = []
        for r in out.get("receivedMessages") or []:
            m = r.get("message") or {}
            attrs = m.get("attributes") or {}
            data = base64.b64decode(m.get("data", "")).decode("utf-8", "replace")
            messages.append(Message(stream, m.get("messageId"), int(attrs.get(TIME) or 0), data, attrs.get(KEY), 0,
                                    None, r.get("ackId")))
        return messages

    def done(self, call, stream, partition, messages, position) -> None:
        if messages:
            self.json(call, stream, "POST", self.subscription(stream) + ":acknowledge",
                      {"ackIds": [m.receipt for m in messages]})

    def dead(self, call, message) -> None:
        self.send(call, message.stream, message.data, message.ms, message.key, message.stream.name + ".dead")
        return message.stream.name + ".dead"


SQS, KINESIS, SERVICEBUS, PUBSUB = Sqs(), Kinesis(), ServiceBus(), PubSub()
