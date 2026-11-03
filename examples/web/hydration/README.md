# Experimental fixed-tree hydration

```sh
python -m nexo web serve examples/web/hydration/app.nexo --hydrate --port 4174
```

Load `/counter/7/`. The heading is already in the server HTML; the external module
validates the tree, attaches Increment and Label handlers, and keeps existing DOM
nodes. Native navigation loads a fresh server document. With JavaScript disabled,
the page remains readable and navigation works, while action controls are inert.

This example exercises fixed scalar trees. Conditional regions are also supported;
see the sibling conditional-hydration example and RFC-0006. Lists, context, async
data, server loaders and navigation actions remain unsupported. Pre-hydration input edits remain
in the control until the next explicit input/change assignment. Mismatched markup
stays in place with no attached behavior; no automatic client remount is attempted.

To integrate with a synchronous WSGI host, create
`SSRApplication.from_file("app.nexo", hydrate=True)` from `nexo.web_ssr`. Bundles and
route props are public. The loopback runner is for local development; production
hosting/authentication/TLS/limits remain host responsibilities. See RFC-0005 for
state transfer, mismatch and lifecycle guarantees.
