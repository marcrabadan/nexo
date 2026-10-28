# Shield integration with database, messaging and AI connectors

The merged main branch adds PostgreSQL, MySQL, MongoDB, Elasticsearch, cloud
streams, network brokers, AI streaming/budgets/providers and MCP. Their ordinary
execution features are retained. This review addresses their Shield authority
boundaries; it is not a wire-protocol or cryptographic audit of every connector.

## Fail-closed boundaries

| Surface | Shield behavior | Reason |
| --- | --- | --- |
| PostgreSQL / MySQL | Reject before transport or password lookup | Direct sockets and reusable connection authority are not brokered/sealed |
| MongoDB / Elasticsearch | Reject before transport or password lookup | Database/session side effects lack the required context confinement |
| Remote Kafka / RabbitMQ / WebSocket | Reject remote construction | Persistent transport, state and consumer lifecycle are unsupported |
| SQS / Kinesis / Service Bus / Event Hubs / Pub/Sub | Reject remote source options | Ambient cloud credentials and persistent state are not delegated by memory mode |
| MCP | Reject construction before configuration discovery or process launch | External processes and tool identities do not have sealed Shield grants |
| Azure / Bedrock AI | Reject resolution before credential lookup | Ambient authentication paths are not constrained by current env grants |
| AI spend budgets | Reject under Shield | User-global usage state is not an authorized application budget store |
| AI configuration discovery / usage telemetry | Disabled under Shield | User/project files must not become implicit read/write authority |
| Checked HTTP AI providers | Retained with inline configuration and explicit secret handles | Existing HTTP/secret checks and broker path apply |
| Local in-memory streams | Retained, including partition/key metadata | Registration/publisher/deferred contexts intersect and payload+key retention is budgeted |

`security_connector` is fatal and cannot be recovered with language `try`.
An allow-net rule does not override these unsupported guarantees. Ordinary
execution outside Shield retains the connectors and implicit AI configuration /
usage behavior from main. SQLite remains governed by its existing file checks.

To integrate an unsupported connector into Shield later, add brokered/mediated
IO, explicit credential/state grants, sealed connection and deferred contexts,
resource/lifecycle budgets and adversarial tests before removing the guard.
Calling checked HTTP APIs explicitly remains available; this does not grant the
raw connector an exception.

## Merge decisions

- Preserve the broker branch while retaining main's ordinary HTTP streaming.
  In the Linux online profile, streaming uses the bounded broker response buffer;
  it does not open a payload socket or promise incremental broker streaming.
- Preserve immutable seals and confirmation-before-conversion for native tools;
  retain ordinary MCP functionality and new AI provider response formats.
- Preserve main's partition scheduling and backend implementations outside
  Shield, while retaining message/registration/query snapshots in memory mode.
- Include serialized keys in both individual and cumulative memory-stream byte
  budgets; they must not bypass retention limits by being outside the payload.
- Avoid implicit AI config reads and usage writes while Shield is enforcing.

Tests in `tests/test_security_connectors.py` use synthetic credentials and local
listeners to verify rejection before connections, secret lookup or process
creation, inability to catch the rejection, unchanged user-global usage, and
stream key budget accounting. Ordinary connector suites verify that the guarded
paths still work outside Shield. Full isolation still requires a compatible host.
