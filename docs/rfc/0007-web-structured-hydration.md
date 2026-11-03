# RFC-0007: Structured hydration and scoped ownership

Status: implemented experimental M6.

SSR and static HTML accept immutable typed record/list state and scoped context providers. Hydration additionally claims conditional and keyed/unkeyed list ranges before attaching behavior. Keyed rows preserve native identity, focus and local component state across movement; removed rows dispose listeners, subscriptions and nested owners. Duplicate int64/string keys fail before DOM mutation. Unkeyed list changes recreate rows. Projected slots retain caller data and receiver lifetime. Providers are fresh per render, shadow lexically and never share request state.

The hydrated handle exposes update(patch), snapshot(), context(name), onDestroy(callback) and idempotent destroy(). Exact typed patches validate before assignment; records/lists are immutable snapshots. Int64 values are decimal strings on the wire and BigInt in JavaScript. Payloads are bounded to 1 MiB, nesting 32 and lists 10,000. Context handles expose checked setState/snapshot and declared actions. navigate() performs checked local full-document navigation. Existing scalar component props remain scalar; browser load/mutation resources do not run during SSR or hydration.

Initial mismatches attach no behavior and preserve server DOM. Failed later structural construction preserves the prior range and disposes partial owners; source-state rollback is not promised. Default client compiler output is unchanged. See tests/test_web_m6_completion.py and earlier conditional/hydration suites.

Stabilization hardening: root teardown attempts every registered cleanup and owner even when a host cleanup throws. It removes the root registry entry in all cases, then reports collected failures as AggregateError. The root stays disposed, repeated destroy() is harmless, and a new hydration of unchanged markup can attach a fresh owner.
