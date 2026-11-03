# RFC-0011: Owned typed subscriptions

Status: implemented experimental M6.

SSRApplication.from_file(..., hydrate=True, channels={localPath: EventChannel(schema)}) enables connectEvents(handle, localPath, {schema, onValue, onError, maxRetries}). subscriptions=True can enable an externally hosted same-origin SSE endpoint instead. The generated module exports the adapter; default onValue applies a checked root-state patch. Invalid JSON, shape, type, oversized payload or event envelope closes the connection before reporting a generic error. Duplicate/stale sequence IDs within an epoch are ignored. Retry errors are bounded (default 3, maximum 10). Root destruction and explicit close remove handlers and close EventSource.

EventChannel validates full public snapshots, encodes int64 as decimal strings and bounds each frame to 64 KiB. A ring buffer holds 1–128 frames (default 32). IDs carry a random epoch and int64 sequence. Retained IDs replay subsequent frames; a new epoch or expired history resumes from the latest full snapshot. This is snapshot synchronization, not durable delivery or a command log. Heartbeats and client disconnect cleanup bound lifetime. The loopback channel reference host caps concurrent requests at eight.

SSE is unidirectional. No WebSocket API, arbitrary network capability, authentication, message broker, distributed durability or production hosting is claimed. Streams are explicitly public; authorization belongs in an independently reviewed host. See real browser updates/teardown and replay tests in tests/test_web_m6_completion.py.
