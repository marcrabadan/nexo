# Changelog

## 0.6.0 — 2026-11-05

- Add Apply to source with revision checks, preservation of runtime logic, and editor undo.
- Generate responsive tablet/mobile CSS from per-viewport geometry edits.
- Extract reusable components with editable text properties and a component library.
- Add multi-selection, alignment, equal spacing, grouping and group movement.
- Add compiler-backed live preview with input, state, navigation, rebuilds and cleanup.

- Import the active Nexo web source into the builder, with readiness handling, undo and partial-import notices.

- Automatically open a single Web Builder beside `.nexo` files referencing `nexo.web`; add `nexo.builder.autoOpen` opt-out.

- Add eight edge/corner resize handles with zoom, snapping and undo support.

- Add desktop/tablet/mobile canvas previews, zoom and optional 8px drag snapping.
- Add duplicate, layer ordering, keyboard nudging and editing shortcuts.
- Highlight selected layers and improve keyboard focus visibility.

## 0.5.0

Add the visual Nexo Web Builder with live source generation and a Design
System Manager for light/dark palettes, shared typography, padding and radii.
Designs and reusable design systems can be saved, loaded and exported.

The `nexo.lang` setting is gone: Nexo's messages are in English only
(D125).

## 0.4.1

Add the Nexo logo to the extension listing and Extensions view.

## 0.4.0

Phase 4 of Nexo: highlighting of `ai`, `agent` and `server` blocks, routes,
`fake ... returns` and the functions of the library; completion of the
library's names and members.

## 0.3.0

First version, with phase 3 of Nexo: highlighting, diagnostics with their
fixes, formatting, go to definition, hover, outline, completion and the
commands to run a file and its tests.
