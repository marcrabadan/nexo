# RFC-0003: M6b — experimental routed static generation

Status: implemented experimental second M6 slice. Extends RFC-0002's scalar
renderer with finite public pages, nested layouts, metadata and real HTML links.
This RFC does not authorize request-time SSR, hydration or async server data.

## Build interface and enumeration

```sh
python -m nexo web build examples/web/ssg/app.nexo --static \
    --routes examples/web/ssg/routes.json -o dist
python -m nexo web preview dist
```

`--static` selects standalone component HTML (RFC-0002) or routed static generation
when the program has public pages. `--routes` requires `--static` and public pages.
No new Nexo language syntax is introduced. The Python frontend loads and validates
ordinary `page`, `layout`, component imports and scalar declarations first.

All public pages with a literal path and no parameters are generated automatically.
A routed site must have a public root page at `/`. Dynamic pages use a UTF-8 JSON
array of concrete local paths, for example `["/members/7", "/members/42"]`.
Every dynamic page must match at least one generated path. Unknown paths, missing
parameter coverage, invalid parameter conversions, duplicate concrete paths and
case-insensitive output collisions fail `NW6003` before any output is replaced.
Static segment specificity, strict int64 parsing and lowercase boolean conversion
match the existing client router. String parameters are portable slugs in this
slice. A more specific static route may shadow a dynamic path; it does not count
as coverage for the dynamic page.

Paths normalize to directory URLs with trailing slashes: `/members/7/` maps to
`members/7/index.html`, and `/` maps to `index.html`. Enumeration accepts an
optional trailing slash. Percent escapes are decoded once before normalization;
aliases such as `/members/%37` and `/members/7` count as duplicates. The first
slice accepts ASCII letters, digits, underscores and hyphens in decoded segments,
with at most 128 characters per segment and 8192 characters per input URL. Dot
segments, encoded separators, double-encoded escapes, query/fragment URLs,
Windows device names and reserved `/_nexo/` paths fail. Unicode, punctuation and
space-bearing route segments require a future extension. Enumeration is limited
to 1 MiB and 1000 paths; automatic pages count toward the output limit.

## Rendering and navigation

Each document is rendered independently with fresh initial scalar state and its
full outer-to-inner layout chain. Component calls, default props, lexical slots
and conditionals reuse the static renderer. Slot outlets and caller ownership
roots match client DOM creation; scoped CSS cannot leak across projected content.
Layouts have no persistent state across HTML navigations. `title` and `description`
are evaluated from typed page parameters and HTML escaped. There is no state
serialization, server evaluation of source functions or secrets access.

`a(..., href: local_path)` becomes a normal directory URL, without a fragment.
Every rendered link must point to a generated page, including links in layouts
and fallback content. A link to an unenumerated dynamic URL fails `NW6003`.
These links support native browser navigation/history with JavaScript disabled.
The client-rendered build retains its fragment URLs and existing behavior.

Public `not_found = true` pages produce `404.html`, with their optional layouts.
The local preview serves that document with HTTP 404 for unknown resources.
Production hosting must explicitly configure its 404/error document and status;
the compiler does not deploy or configure a public server. `error = true` pages
are rejected: build errors fail the entire build instead of emitting a fallback.

All pages reference shared `/_nexo/app.css`. This slice targets deployment at the
origin root. Subdirectory/base-path hosting, arbitrary resource URLs, external
links, route queries and fragments are outside its contract. A static host must
serve directory `index.html` files and CSS with the correct content types.

## Artifact ownership and preview

Routed output includes `nexo-manifest.json`, schema version 1, format
`nexo-web-static`, with the generated URL-to-file map, an input fingerprint and
per-file size/checksum/integrity metadata. This is a distinct artifact format
from the existing optimized M5 release; no optimizer or SRI-loading claim is made.
A dedicated helper verifies static ownership while reusing M5's staged writes.
The M5 optimizer fingerprint and measured release assets remain unchanged.

Compilation, enumeration, rendering and link checks complete before staged output
replacement. Removed routes disappear on rebuild. Modified artifacts, unrelated
files/directories and symlinks prevent replacement. A final rename failure restores
the previous directory. Switching between routed static, standalone static, client
and optimized release artifacts respects the previous manifest's owned files.
Manifest hashes detect local changes; they are not signatures or proof of origin.

The artifact-only loopback preview supports nested directory URLs, trailing-slash
redirects, content types and fallback 404 status. Both static modes disable scripts
and connections through CSP. It also previews standalone M6a HTML/CSS builds.
Source files and unlisted assets are never served.

## Limits and validation

Actions/events, requests/mutations, context, structured state and loops remain
unsupported. All loaded declarations and branches are checked for these features.
`--release`, `--sourcemap` and `--observe` remain incompatible with `--static`.
No Node runtime, browser or network access is needed to build. Browser tests use
Chromium only; no cross-engine or production-readiness claim follows.

`tests/test_web_ssg.py` covers typed enumeration, static precedence, layouts,
metadata escaping, links, path rejection, deterministic relocation, manifests,
build-mode switching, output preservation and rollback, preview status and actual
browser navigation with JavaScript disabled. A browser comparison also checks
nested layout DOM and projected-content CSS against a normal client build.
Request-time SSR, hydration, async server data, PWA, WASM interop and subscriptions
remain future RFC work.

RFC-0004 now provides bounded scalar request-time WSGI rendering; earlier
future-SSR statements here describe this RFC's original milestone boundary.
Hydration and async server data remain future work.
