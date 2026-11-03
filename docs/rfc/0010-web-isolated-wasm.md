# RFC-0010: Worker-isolated WASM interoperability

Status: implemented experimental M6.

SSRApplication.from_file(..., hydrate=True, wasm=True) explicitly enables loadWasm(handle, localURL, {sha256, functions, timeoutMs}). The generated external module exports the adapter. Supply named signatures with i32/i64/f32/f64 scalar arguments/results or void result. Binary fetches require same-origin paths, application/wasm, a verified SHA256 and at most 1 MiB. Modules with imports are rejected.

Compilation and execution occur in an external fingerprinted module Worker. No memory, DOM, imported host functions or WASI capability is exposed. Arguments/results are checked; i64 is BigInt, floats finite. Pending calls are capped at 32, arguments at 16. Initialization/fetch and each call have bounded deadlines (default 5 seconds, maximum 30); expiration terminates the worker and rejects pending work. Root teardown or explicit adapter.destroy() aborts fetches and terminates work. Only this opt-in adds worker-src self and wasm-unsafe-eval CSP permissions. CSP and integrity remain enforced. Browser tests include actual numeric execution, integrity refusal, import refusal, an infinite loop timeout and teardown.
