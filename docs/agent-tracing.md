# Opt-in agent tracing (D188)

With `NEXO_AGENT_TRACE` unset or empty, no trace exporter runs. Set
`NEXO_AGENT_TRACE=stderr` explicitly to write one OTLP JSON envelope per line to
standard error. Other values fail before agent dispatch; there is no implicit
collector discovery or network exporter.

```sh
NEXO_AGENT_TRACE=stderr nexo main.nexo 2>agent-traces.jsonl
```

The mapping is the `ExportTraceServiceRequest` / resource / scope / span model
from [opentelemetry-proto v1.7.0](https://github.com/open-telemetry/opentelemetry-proto/tree/v1.7.0/opentelemetry/proto),
using [OTLP JSON encoding](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding).
IDs are nonzero lowercase hexadecimal, nanosecond timestamps are decimal
strings and enums are numeric. The closed [Nexo profile schema](schemas/agent-trace-v1.json)
permits only the fields emitted by this implementation. This is a subset of
OTLP, not a claim to implement every GenAI semantic convention or exporter.

Spans have static names `nexo.agent.run`, `nexo.agent.provider` and
`nexo.agent.tool`, kind INTERNAL and status OK/ERROR for runtime call completion.
A tool result that reports an error to the model may still be a successfully
completed dispatch. Synchronous child spans inherit parent and trace IDs;
a task without a current trace starts a separate trace. The resource is the
constant service name `nexo`, and instrumentation scope is `nexo.agent`, version
`1`. No program attributes or remote parent headers are accepted.

The exporter never receives prompts, model outputs, tool arguments/results,
URLs, model/tool/agent/run/file identities, exception messages, secrets or
arbitrary attributes. Errors export status alone. IDs and timing still reveal
execution structure; enable collection deliberately and protect the destination.
Write failures fail the invocation. Tracing does not add permission authority,
persist approval or resume a failed run. Export on a process crash is not
promised. Standard error can also contain ordinary diagnostics; collectors
should select OTLP envelopes rather than treat every line as trace data.
