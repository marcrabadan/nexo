# RFC-0006: M6e — experimental conditional hydration

Status: implemented experimental fifth M6 slice. Extends RFC-0005's opt-in
hydration with scalar `if`/`elif`/`else` regions. List hydration, context, server
data loaders and arbitrary serialized state remain excluded.

## Interface and server markers

```sh
python -m nexo web serve examples/web/conditional-hydration/app.nexo --hydrate --port 4174
```

The WSGI factory's `hydrate=True` flag and the generated module's hydrate/destroy
interface remain those of RFC-0005. No new Nexo syntax is introduced. Ordinary
SSR/static builds retain their existing condition rendering and emit no new markers.

In hydration mode, each conditional emits compiler-owned comment boundaries:
`<!--nexo-if:line:choice-->` and `<!--/nexo-if:line-->`. Choice is the first true
branch index, the else index, or -1 when no branch matches. Comments add no element
wrapper and do not alter element-based CSS selectors. Source positions are scoped
by the surrounding ownership roots; nested same-parent regions have distinct
source lines. The module build identity covers the emitted claim/render protocol.
Deployments must preserve these comments; stripping them causes a closed mismatch.

The server evaluates the same typed scalar condition expressions as before. The
client recomputes the initial choice from the transferred scalar route props and
initial source state. It verifies exact start/end markers, selected index and the
complete branch subtree before any listeners/bindings are installed. Empty regions
still have both markers. Missing, altered or extra markers/content fail NW6005,
dispose planned ownership and leave the owned SSR tree intact.

## Reactive branch ownership

A claimed branch has a separate ownership/session lifetime. Its binding dependencies
link to the containing component's scalar state. If state changes while the chosen
index stays the same, the existing branch/component instances remain mounted and
ordinary bindings update in place. Nested local component state is retained. If
the selected index changes, that region constructs a fresh branch in a detached
DocumentFragment using native element/text/attribute operations, with no innerHTML,
raw HTML or global DOM interception.

Live transitions first verify that both anchors are intact and ordered and that
each top-level node between them belongs to the active branch ownership graph.
Damaged/moved anchors or an extended range containing unrelated siblings fail
without disposing the old branch or deleting those siblings. This is an ownership
guard, not a general reconciliation or repair API for externally edited DOM.

New branch initializers and bindings must succeed before replacing the old branch.
Only then are old owners, listeners and subscriptions disposed and nodes between
the two markers replaced. Markers and all ancestor/unrelated DOM remain in place.
Newly active components get fresh initial local state from current props. An empty
selection removes only the previous branch's content, retaining the marker pair.
A failed new-branch construction disposes its partial detached ownership and keeps
the old DOM and behavior. It does not roll back the source action's state changes,
replay the action or automatically retry an unchanged cached choice. Existing
client action/error and partial-assignment semantics continue to apply.

Removed branch nodes no longer respond to their old handlers or parent-state
updates, even if a host retains a detached node reference. Repeated switching must
not accumulate listeners or subscriptions. Root destroy disposes the current branch
hierarchy and preserves the current DOM, including markers, as in RFC-0005. There
is no implicit state migration or automatic rehydration of updated client markup.

## Nested regions and projected slots

Each claimed branch has an explicit cursor limit at its end marker. Nested claims
cannot consume a parent's boundary or following siblings. A region with unchanged
choice preserves its nested ownership; changing an inner region leaves outer
siblings intact, while changing an outer region disposes all active descendants.

Projected slot functions receive the active hydration/creation session explicitly.
Their lexical values and source owner still belong to the caller, while their
listeners/subscriptions belong to the receiving branch lifetime. Consequently a
conditional slot can be shown, hidden and recreated without placing its owners
on a long-lived root session or attempting to claim nonexistent DOM. Caller scope
wrappers and existing CSS isolation remain unchanged. Layouts still obey the
language's original requirement for one unconditional routing slot.

## Guarantees inherited from M6d

Initial successful hydration retains existing elements/nonempty text, focus and
live input/checkbox properties. Empty text placeholders can be inserted only after
verification. Pre-hydration edits are not automatically adopted into state and
clicks are not replayed. New branches use current source state/props; removed
controls are not preserved. Focus outside a changed region stays untouched; focus
inside removed content follows native browser behavior, without invented migration.

Typed scalar props, int64 BigInt precision, safe text/attribute updates, assignment
and WebEvent actions, full-document navigation, same-origin external module CSP,
compile-once request isolation and no per-request I/O remain unchanged. Without
JavaScript, the server's selected branch remains readable and native links work.
No source action is executed on the server.

## Validation and limits

`tests/test_web_conditional_hydration.py` covers initial identity/no-mutation claims,
unchanged-choice state retention, reactive child props, conditionally projected
slots, direct nested sibling ranges, elif/else and empty transitions, unrelated
input/focus, detached-node disposal, repeated switches, root teardown, malformed
markers, failed branch construction and no-JavaScript SSR. Existing fixed-tree,
SSR/static/SSG/frontend/tooling/composition suites remain regressions.

This is conditional structural hydration only. Lists/keyed reconciliation, context,
resource requests, server data, navigation actions, arbitrary server-state transfer,
streaming, production serving and cross-engine qualification remain future work.
Ordinary client codegen/runtime and measured M5 optimized artifacts are unchanged.
