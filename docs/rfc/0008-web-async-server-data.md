# RFC-0008: Async server data

Status: implemented experimental M6.

SSRApplication.from_file(path, hydrate=True, loader=async_callback, loader_timeout=10) invokes a trusted Python callback once per rendered page. The frozen RenderRequest provides path, page, page_id and read-only typed params. Return a partial dictionary of root-page state overrides. Fields and nested values must exactly match source types. The renderer copies values and emits escaped typed wire data for hydration. No callback values become executable JavaScript.

response() and WSGI run the async loader synchronously in a request-local event loop; aresponse() is the async embedding interface. Concurrent callers have isolated state. Loader timeouts (maximum 120 seconds), invalid values and exceptions produce generic 500 responses without backend diagnostics. HEAD runs the loader for consistent headers. CLI serve --loader trusted.module:async_function is explicit trusted backend configuration, not source-language module execution.

Request cookies, identity, bodies and credentials are not implicitly passed. Applications needing authenticated data must provide separately authorized middleware and hosting. No streaming SSR, arbitrary source async functions, browser-resource replay or production listener is introduced. See loader tests in tests/test_web_m6_completion.py.
