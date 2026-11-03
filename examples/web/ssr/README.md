# Experimental request-time HTML

From the repository root:

```sh
python -m nexo web serve examples/web/ssr/app.nexo --port 4174
```

Open the printed loopback URL. Load `/members/123/` directly and follow Next member.
The server renders any valid int64 member URL on demand; no routes JSON or browser
JavaScript is needed. A value whose Next member calculation overflows returns the
explicit error page with HTTP 500. Unknown typed routes use the not-found page.

The source is compiled once on startup; restart to reload. This loopback reference
runner is for local development. To integrate with a synchronous WSGI host, create
`SSRApplication.from_file("examples/web/ssr/app.nexo")` from `nexo.web_ssr` as the
host's application callable. Supply authentication, request limits, host checks,
TLS and lifecycle management separately. No production hosting is supplied here.

Only scalar pages/layouts/components are supported: no actions/events, async data,
context or hydration. Deploy at an origin root and use portable ASCII route slugs.
See `docs/rfc/0004-web-request-time-ssr.md` for the complete interface and bounds.
