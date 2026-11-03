# Experimental conditional hydration

```sh
python -m nexo web serve examples/web/conditional-hydration/app.nexo --hydrate --port 4174
```

The initial panel is server-rendered and claimed in place. Increment local, then
increment the parent: the same panel retains its local count, and its conditional
slot begins showing the projected parent value. Toggle the panel off/on: the old
panel is disposed and the new one starts with fresh local state. Parent state and
unrelated DOM survive both transitions.

Only conditional scalar hydration is added here. Lists, context, async server data
and arbitrary state transfer remain unsupported. Keep compiler comment boundaries
in deployed HTML. Mismatches attach no behavior; failed new-branch construction
keeps the old branch without rolling back the source action. With JavaScript
disabled, the initial server branch and native links remain available.

The loopback runner is a local development tool. Use
`SSRApplication.from_file("app.nexo", hydrate=True)` for an external synchronous
WSGI host, with production/authentication/TLS/limits supplied separately. See
RFC-0006 for ownership, slot and mismatch details.
