# Nexo Web website content

The public overview is deliberately short. Keep syntax details, command options
and deployment limits in the relevant guide instead of expanding the overview.

| Canonical template | Generated page | Purpose |
| --- | --- | --- |
| `page.tmpl` | `docs/web.html` | First component and next steps |
| `guide.tmpl` | `docs/web-guide.html` | Components, routing and scoped CSS |
| `api.tmpl` | `docs/web-api.html` | Typed data example and browser API reference |
| `development.tmpl` | `docs/web-development.html` | Local tools and diagnostics |
| `deployment.tmpl` | `docs/web-deployment.html` | Releases, security and supported limits |

From the repository root, run `python docs/web/build.py`, then
`python docs/web/build.py --check`. The builder also checks and refreshes the
existing demos. Examples come from `examples/web/`; keep historical validation
reports and release measurements separate from introductory copy.

Use descriptive feature names in public prose. Preserve existing demo URLs and
reference anchors when linking to historical material. Keep the experimental
status and runtime, security and hosting limitations explicit in the guides.
