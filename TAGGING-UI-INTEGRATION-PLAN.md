# Tagging / Numbering / Activation — UI Integration Plan

Follow-up to `TAGGING-AUTONUMBER-PLAN.md`. Phases 1–5 landed the backend:
`/api/tag-rules`, `/api/number-ranges`, `/api/activation-rules` (GET/PUT),
`primary_tag` + `activation_mode` on the channel model, reset-to-auto
endpoint. Nothing in the UI consumes any of this yet.

This plan wires the backend into the existing sidebar-nav + single-page UI
(`templates/ui.html`, `static/app.js`, `static/style.css`). It's split into
six phases that are independently mergeable so each one is testable on its
own.

## Build Order

1. Channels table: new columns (`primary_tag`, `activation_mode`) + filter
2. Channel edit modal: activation mode control + primary_tag display
3. Renumber modal: strip `start`, add tag-scope selector + range preview
4. New **Settings → Tagging** subtab: Tag Rules editor
5. Settings → Tagging: Number Ranges editor
6. Settings → Tagging: Activation Rules editor + ingest summary banner

Each phase is small enough to finish + smoke test in one session.

---

## Phase A — Channels Table: New Columns + Filter

**Backend source:** `GET /api/channels` now returns `primary_tag` and
`activation_mode` on every row (landed in Phase 4 — see
`manifold/services/channel_manager.py:61-77`).

**Table header** (`ui.html:113-126`):
Insert two columns between `Tags` and `Active`:
- `Primary` — primary_tag pill
- `Mode` — activation_mode icon (auto / force_on / force_off)

**Row render** (`app.js` `renderChannelTable()` around line 265):
- Primary: colored pill, one color per tag (sports=orange, news=blue,
  movies=purple, kids=green, live=gray, event=red, uncategorized=neutral).
  CSS class `.tag-pill.tag-<name>`.
- Mode: single glyph + tooltip:
  - `auto` → 🤖 "Rule-driven (resets with tag rules)"
  - `force_on` → 🔒✓ "Manually on (immune to rules)"
  - `force_off` → 🔒✗ "Manually off (immune to rules)"
  - Use inline SVG or unicode — consistent with existing icon style (no new asset dependency).

**Filter** (`app.js:198-206` — existing tag filter):
- Rename the existing "Tag" filter to "Tags (any)" — keeps current behavior of filtering by `tags[]` array membership.
- Add a second `#channel-primary-tag-filter` dropdown: "Primary". Populated from `[...new Set(channels.map(c => c.primary_tag).filter(Boolean))]`. Filtering is an exact match on `primary_tag`.
- Add a third `#channel-mode-filter` dropdown: "Mode" with values `all / auto / force_on / force_off`.

**No backend changes for this phase.**

**Smoke test:**
- Load `/channels` UI, confirm new columns populate.
- Filter by `primary_tag=sports` narrows the list correctly.
- Filter by `mode=force_on` matches all manually-activated channels.

---

## Phase B — Channel Edit Modal: Activation Mode Control

**Modal source:** `ui.html:381-429` (`#edit-modal`), handler in
`app.js:434-526`.

**New fields:**
- Read-only info row: `Primary tag: <pill>` — no edit, it's computed by the rule engine.
- Activation Mode tri-state (radio group or button group):
  - **Auto** — "Let tag rules decide"
  - **Always On** — "Keep active regardless of rules"
  - **Always Off** — "Keep inactive regardless of rules"
- Remove the existing `Active` checkbox from the modal. It's now redundant —
  mode + ingest decides `active`. Direct `active` toggling still happens via
  the row-level toggle in the table (which writes `force_on`/`force_off`
  via the toggle endpoint — already landed in Phase 4). The edit modal is
  for explicit intent, which is what `activation_mode` expresses.

**Save handler** (`app.js` edit-save around line 497):
- PUT `/channels/{id}` with `{activation_mode, channel_number, title_override, tags, title}` — `active` is dropped from the payload; the server derives it from mode (via the update_channel path already landed).
- Actually: the backend currently computes `active` based on the `active` field if present — should be updated to derive from `activation_mode` when that's the input. See **Backend tweak** below.

**Backend tweak** (small, for this phase):
- In `ChannelManagerService.update_channel` (`manifold/services/channel_manager.py`), when only `activation_mode` is provided (no `active`), also set `active` accordingly:
  - `force_on` → `active=True`
  - `force_off` → `active=False`
  - `auto` → leave `active` alone (next ingest's additive pass will set it)
- This keeps the UI simple (user picks mode, server figures out the rest).

**Smoke test:**
- Open edit modal for a force_off channel, switch to force_on, save. Row becomes active immediately.
- Switch to auto, save. Row stays on whatever `active` was, but next ingest (or the next rule evaluation) will adjust it.

---

## Phase C — Renumber Modal: Rule-Driven

**Modal source:** `app.js:360-417`.

**Remove:** the `#renumber-start` input — `start` is ignored server-side now.

**Add:**
- **Scope selector** (radio group):
  - **All active channels** — POST `{}`
  - **Current filter** (existing, only shown if filters active) — POST `{ids: [...]}`
  - **By primary tag** (multi-select chips) — POST `{tags: [...]}`
- **Range preview** section (read-only): show each selected tag's number range from `/api/number-ranges`, e.g.
  ```
  sports → 1000-1999 (619 channels)
  news   → 2000-2099 (15 channels)
  ```
  Fetched at modal open via a single `GET /api/number-ranges` call plus a client-side count from the currently-loaded channels array.
- **Warning** when a scope would exceed its range (preview count > range size): red text, disable the Apply button.

**Apply button:**
- Collect scope, build payload, POST `/api/channels/renumber`.
- On success, re-load channels and close modal.

**Smoke test:**
- Open modal, pick "By tag: sports", confirm preview shows `1000-1999 (N channels)`.
- Apply, confirm table refreshes with sports channels renumbered.
- Pick a tag whose count exceeds its range → Apply button disables with warning.

---

## Phase D — Settings → Tagging Subtab: Tag Rules Editor

**Location:** new subtab under **Settings**, alongside General / Tasks /
Scheduler / Images / EPG / Stream / Integrations.

**Subnav entry** (`ui.html` around the settings subnav, ~line 50):
```html
<button class="subnav-item" data-subview="tagging">Tagging</button>
```

**Render condition** (`app.js:1215-1303` — the
`currentSettingsSub === "xxx"` chain):
```js
if (currentSettingsSub === "tagging") {
  // render tagging subtab
}
```

**Layout:** a single scrollable panel with three cards, in order:

1. **Tag Rules** (this phase — Phase D)
2. **Number Ranges** (Phase E)
3. **Activation Rules** (Phase F)

### Tag Rules card

**Data source:** `GET /api/tag-rules` returns:
```json
{
  "priority": ["event","sports","news","movies","kids","live"],
  "sports": {"keywords": ["espn", ...], "domain_keywords": ["espn"]},
  ...
}
```

**UI:**

- **Priority section**: a drag-to-reorder list (or numbered text input if drag
  is too much for one phase — a textarea with comma-separated tag names is
  acceptable for v1). This decides which tag wins when a channel matches
  multiple.
- **Per-tag editor** (one row per tag, collapsible card):
  - Tag name (read-only text for existing tags, editable "new tag" row at the bottom)
  - `keywords` — chip input (type word, enter adds a chip, click chip to remove). For v1, a simple `textarea` with newline-separated keywords is fine — faster to build, good enough.
  - `domain_keywords` — same pattern.
  - "Remove this tag" button.
- **Add new tag** — button at bottom, spawns a blank row.
- **Save** button at the bottom of the card: PUT `/api/tag-rules` with the full current state.
- **Reset to defaults** button — shows a confirm dialog, then PUT with the hardcoded default from `manifold/services/tag_rules.py`. (Backend tweak: add a `POST /api/tag-rules/reset-defaults` endpoint so the UI doesn't need to know the defaults — cleaner separation.)

**State & validation (client-side):**
- Warn if a tag in `priority` isn't defined in the rules (typo guard).
- Warn if a tag has zero keywords and zero domain_keywords (it'll never match).

**Smoke test:**
- Open Settings → Tagging, confirm defaults render.
- Add "espn2" to sports keywords, Save.
- Verify `GET /api/tag-rules` reflects the change.
- Trigger re-ingest via the Sources page, confirm a channel with "espn2" in its title gets tagged `sports`.

---

## Phase E — Settings → Tagging: Number Ranges Editor

**Data source:** `GET /api/number-ranges` — shape:
```json
{"sports": {"start": 1000, "end": 1999}, ...}
```

**UI (card in the Tagging subtab):**

- **Per-tag row**:
  - Tag name (read-only, dropdown of existing tags from tag_rules.priority, excluding "uncategorized")
  - Start (`<input type="number">`)
  - End (`<input type="number">`)
  - Slot count (read-only, `end - start + 1`)
  - "In use" (read-only, count of active channels with this `primary_tag`)
  - "Utilization" (read-only, `in_use / slot_count` as percent)
  - Remove button
- **Add tag** — dropdown of priority tags that don't yet have a range.
- **Overlap detection** (client-side): if any two ranges overlap, show a red warning strip. Ranges don't *have* to be disjoint for the backend to work, but overlapping is almost always a user mistake.
- **Save** button: PUT `/api/number-ranges`.
- **Reset to defaults** button — needs matching backend endpoint
  (`POST /api/number-ranges/reset-defaults`).

**Utilization display** is the key UX payoff — it makes it obvious when
you need to widen a range (the user's original complaint: "987 events
didn't fit in the 500-slot default range").

**Smoke test:**
- Open card, confirm default ranges render.
- Widen `event` to 500-2499, Save, re-ingest via Sources page.
- Confirm all events now get numbered.

---

## Phase F — Settings → Tagging: Activation Rules + Ingest Summary

### Activation Rules card

**Data source:** `GET /api/activation-rules` → `{tags_auto_on: [...]}`.

**Semantic note (backend revision):** activation matches on **any** tag in
the channel's `tags` array, not just `primary_tag`. So
`tags_auto_on = ["espn"]` activates every channel tagged `espn` regardless
of primary. Backend change required before this UI ships:

- In `manifold/services/activation.py` → `should_be_active()`:
  change `return bool(primary_tag) and primary_tag in tags_auto_on`
  to take the channel's full `tags` list and check
  `return any(t in tags_auto_on for t in (tags or []))`.
- In `manifold/services/m3u_ingest.py` ingest loop: pass `computed_tags`
  (the full list) to the activation check instead of `primary_tag`.

**UI:**
- Single section: "Tags that activate channels automatically"
- Chip/checkbox input. Sources for the suggestion list:
  1. All tags in `tag_rules.priority` (curated primary categories)
  2. All distinct tags across channels (`[...new Set(channels.flatMap(c => c.tags || []))]`) — surfaces secondary tags like `espn`, `ncaaf`, `ppv`, group-title passthroughs
- Pre-select items already in `tags_auto_on`.
- Free-text "add tag" input at the bottom for tags that don't exist yet.
- Save: PUT `/api/activation-rules`.
- **Helper text**: "Channels with **any** of the above tags will go active
  on next ingest, unless you've manually set them to Always On / Always
  Off via the channels table. Matches against the full tag list, not just
  the primary category — so you can activate by genre, network, sub-sport,
  etc."

**Smoke test:**
- Uncheck `movies`, Save, re-ingest. All auto-mode movies channels go inactive.
- Re-check `movies`, Save, re-ingest. They come back.

### Ingest Summary Banner

**Why:** the backend logs warnings like "Number range exhausted for tag
'event' (500-999)" but these only show up in `/api/logs/tail`. We want a
visible UI signal.

**Approach:**
- Extend the ingest endpoints (`POST /api/m3u-sources/ingest`,
  `POST /api/m3u-sources/{id}/ingest`) to return a `warnings` array in
  addition to the current `{ok, channels, ...}`:
  ```json
  {"ok": true, "channels": 4735, "warnings": [
    {"type": "range_exhausted", "tag": "event", "range": [500, 999], "unassigned": 987}
  ]}
  ```
- Backend change: track exhausted ranges in `AutoNumberer` (already does
  internally via `_exhausted_warned`) and surface them up through
  `ingest_source` → `ingest_all`. Count the unassigned channels too.
- Frontend change (`app.js` where ingest is triggered, probably in the
  Sources page handler): if `warnings.length > 0`, render a yellow banner
  at the top with the warning + a "Fix ranges" link that jumps to
  Settings → Tagging → Number Ranges.

**Smoke test:**
- Set `event` range to 500-509 (tiny), re-ingest.
- Confirm yellow banner appears with "Event range exhausted — 1477 channels unassigned".
- Click "Fix ranges" → navigates to Settings → Tagging → Number Ranges.

---

## Cross-Cutting: Reset-to-Defaults Endpoints

Phases D, E, F all want a "reset to defaults" button. Cleanest to add three
small endpoints rather than hardcoding defaults in JS:

- `POST /api/tag-rules/reset-defaults` — writes `DEFAULT_TAG_RULES`
- `POST /api/number-ranges/reset-defaults` — writes `DEFAULT_NUMBER_RANGES`
- `POST /api/activation-rules/reset-defaults` — writes `DEFAULT_ACTIVATION_RULES`

All three are trivial — they just call their respective `set_*()` helper
with the module's default constant.

**Files:** `manifold/web/routers/system.py` — add three endpoints below the
existing GET/PUT pairs.

---

## Cross-Cutting: CSS

New classes needed (add to `static/style.css`):

- `.tag-pill` — small rounded badge, 11px font, padding 2px 8px.
- `.tag-pill.tag-sports`, `.tag-pill.tag-news`, etc. — color variants. Use
  the existing color tokens if defined (`--accent`, `--warning`, etc.),
  otherwise add a small palette section at the top of style.css.
- `.mode-indicator` — wrapper for the activation_mode icon in the table.
  Tooltip via `title=` attribute or a `.tooltip` helper if one exists.
- `.warning-banner` — yellow background, dismissable, for the ingest
  summary.

Keep the palette small (one color per primary tag). Don't introduce a new
icon library — use inline SVG or unicode.

---

## Phased Smoke Test Summary

| Phase | Smoke test |
|-------|-----------|
| A | Table shows primary_tag + mode columns; all three filters narrow correctly |
| B | Edit modal sets activation_mode; server reflects in DB |
| C | Renumber modal with tag scope works; over-range scope disables Apply |
| D | Tag rules editor roundtrips; adding a keyword reclassifies channels on re-ingest |
| E | Number ranges editor shows utilization; widening a range assigns more channels on re-ingest |
| F | Activation rules checkbox list works; unassigned-channel warning banner appears |

---

## Deferred / Out of Scope

- Rich chip input for keywords — v1 uses textarea with newline-separated
  values. Upgrade to chips later if the textarea feels awkward.
- Drag-and-drop priority reordering — v1 uses a textarea or ordered input
  list. Upgrade to drag later.
- Bulk "reset to auto" action from the table selection — could be added
  after Phase A if you want it. Small addition: a button in the bulk
  action bar that POSTs to a new `/api/channels/bulk-reset-activation`
  endpoint.
- Color palette polish — first pass uses simple named colors per tag,
  revisit once the UI is in use.
