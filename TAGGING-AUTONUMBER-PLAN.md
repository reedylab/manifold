# Tagging, Auto-Numbering, Additive Activation — Plan

## Goal

Shift manifold from "ingest-driven" to "curation-driven":
- Rule-based tagging becomes the source of truth for category, channel number, and active state.
- M3U source becomes a catalog of available channels; tags decide what's exposed to Jellyfin.
- Manual overrides still win, so the existing UI toggles don't regress.

## Build Order

1. Externalize tagging rules
2. Per-tag number ranges → auto-numbering on ingest
3. Rebuild `/channels/renumber` on the rule engine
4. `activation_mode` tri-state + additive activation pass
5. Stale-delete guard (active-only)
6. Channelarr-side tagging

Each phase is independently shippable.

---

## Phase 1 — Externalize Tagging Rules

**Today:** `_compute_tags()` in `manifold/services/m3u_ingest.py:53-105` hardcodes keyword lists for sports, news, movies, kids, ppv, vod, plus group-title passthrough and event regex.

**Change:**
- Move keyword lists + tag priority order into `manifold_settings` under keys like:
  - `tag_rules.sports.keywords` → `["espn", "nfl", "nba", "mlb", ...]`
  - `tag_rules.news.keywords` → `["cnn", "fox news", "msnbc", ...]`
  - `tag_rules.priority` → `["event", "sports", "news", "movies", "kids", "live"]`
- Keep event-detection regex in code (too structural for settings).
- Keep group-title passthrough behavior.
- Add a `primary_tag` computed field: first tag in `tags` that appears in `tag_rules.priority`. This is what auto-numbering and activation use. Stored on the row for performance and UI clarity.

**Schema:**
- `manifests.primary_tag` — nullable VARCHAR, indexed. Computed during ingest, overwritten each run.

**Files:**
- `manifold/services/m3u_ingest.py` — rewrite `_compute_tags()` to read from settings, compute `primary_tag`.
- `manifold/models/manifest.py` — add `primary_tag` column.
- Alembic migration for `primary_tag`.
- `manifold/config.py` — add defaults for tag rules (seeded into settings on first run if missing).
- `manifold/web/routers/` — add simple GET/PUT endpoints for tag rules so they can be edited without touching the DB directly. UI can come later; API first.

**Open question:** YAML file vs. DB settings? DB is consistent with existing patterns (`manifold_settings` KV). Default to DB, seed from a shipped YAML on first run for convenience.

---

## Phase 2 — Per-Tag Number Ranges + Auto-Numbering

**Today:** `Manifest.channel_number` (`manifold/models/manifest.py:63`) is a nullable int, purely manual. Orphan numbers when channels disappear.

**Behavior:**
- Per-tag number ranges stored in settings:
  - `number_ranges.sports` → `{"start": 1000, "end": 1999}`
  - `number_ranges.news` → `{"start": 2000, "end": 2099}`
  - etc.
- On ingest, after tags are computed, run auto-number pass for each channel:
  1. If `channel_number` is already set and in-range for `primary_tag` → leave alone (sticky).
  2. If `channel_number` is set but out-of-range → reassign to next free in correct range.
  3. If `channel_number` is null → assign lowest free number in the range for `primary_tag`.
- "Free" = not used by any other active or inactive channel in that range.
- Do **not** reclaim numbers from deleted channels. Gaps are fine; stability matters more.
- Channels with no `primary_tag` or whose primary_tag has no configured range: leave `channel_number` null.

**Files:**
- `manifold/services/m3u_ingest.py` — add `_auto_number()` call after `_compute_tags()`.
- New helper `manifold/services/autonumber.py` — encapsulates range lookup + free-slot finder so Phase 3 can reuse it.

**Edge cases:**
- Range exhausted → log warning, leave channel unnumbered. UI should surface this.
- Primary tag changes between ingests (e.g., a channel gets retagged from "sports" to "news") → auto-number will relocate it to the new range on next ingest. That's desired.
- Manual override: if a user explicitly set a number via the existing UI, it stays sticky as long as it's in-range for the primary tag. If the tag changes, the manual number gets moved. This is a trade-off — simplest behavior. If it bites, revisit with a `manual_number` flag.

---

## Phase 3 — Rebuild `/channels/renumber` on the Rule Engine

**Today:** `POST /channels/renumber` (`manifold/web/routers/channels.py:53-73`) takes `start` and assigns sequentially from that value.

**Change:**
- New behavior: runs the same auto-number pass from Phase 2 across all active channels, wiping existing numbers first (full rebuild).
- Accept optional `scope` param: `all` (default) or a list of tags to limit the rebuild to.
- Old `start`-based behavior either removed or kept under a different endpoint name. Default to removing — if you miss it, add back.

**Files:**
- `manifold/web/routers/channels.py` — rewrite handler.
- Reuse `manifold/services/autonumber.py` from Phase 2.

---

## Phase 4 — Activation Mode Tri-State + Additive Activation

**Today:** `Manifest.active` (`manifold/models/manifest.py:68`) is a Boolean. Ingest sets it based on source presence. UI toggles flip it directly.

**Schema:**
- Add `activation_mode` enum column: `auto | force_on | force_off`. Default `auto`.
- `active` Boolean stays as the effective state. `activation_mode` governs how ingest is allowed to touch it.

**Settings:**
- `activation_rules.tags_auto_on` → `["sports", "news", "live"]` — tags that flip `auto` channels active.
- Everything not matching stays inactive.

**Ingest pass (additive):**
- For each channel with `activation_mode == 'auto'`:
  - If **any** tag in the channel's `tags` array is in `activation_rules.tags_auto_on` → `active = True`
  - Else → `active = False`
- For `force_on` / `force_off` rows → leave `active` alone.

> **Revision (post-implementation):** Originally this pass matched on
> `primary_tag` only. Switched to any-tag match so users can activate
> based on secondary tags (e.g. `tags_auto_on = ["espn"]` activates every
> ESPN channel regardless of whether its primary is `sports` or `event`).
> Numbering is still `primary_tag`-driven — a channel can only have one
> number, so priority order continues to resolve that.

**UI interaction:**
- Existing toggle button flips to `force_on` or `force_off` depending on direction (records user intent).
- Add a "reset to auto" action that sets `activation_mode = 'auto'` so the channel rejoins the rule-driven pool.

**Uniqueness constraint:**
- `uq_manifests_title_active` (`manifest.py:80-83`) — partial index on active==True. Additive mode will make a lot more channels inactive; this constraint keeps working but watch for edge cases where a channel flips active and collides with an existing active-same-title row. Current behavior already handles this; no change needed.

**Files:**
- `manifold/models/manifest.py` — add `activation_mode` column + enum.
- Alembic migration.
- `manifold/services/m3u_ingest.py` — add activation pass after auto-number.
- `manifold/services/channel_manager.py` — toggle endpoint writes `activation_mode`, not just `active`.
- `manifold/web/routers/channels.py` — add "reset to auto" endpoint.

---

## Phase 5 — Stale-Delete Guard (Active-Only)

**Today:** `STALE_GRACE_HOURS = 12` in `manifold/services/m3u_ingest.py:181`. Any channel disappearing from source gets deactivated, then deleted 12h later.

**Problem in additive world:** inactive channels that flicker in/out of the source will churn rows — delete, reimport as new row, lose any `force_on`/`force_off` state the user set.

**Change:**
- Stale-delete only fires for channels that were `active == True` and `activation_mode == 'auto'` at disappearance time.
- `force_on` / `force_off` channels: never auto-delete, even if absent from source. They represent user intent and should persist until user removes them.
- Inactive `auto` channels: delete immediately on disappearance (they were never exposed, no grace needed). Alternative: keep them in the catalog with a `last_seen` timestamp and prune on a longer TTL (e.g., 30d). Flagging as open question.

**Files:**
- `manifold/services/m3u_ingest.py` — rework stale-check branches around lines 181-202.

**Open question:** Inactive catalog retention — delete-on-disappear vs. long TTL. Long TTL preserves user ability to browse everything the source has ever offered, which is nice but could bloat the DB. Default to delete-on-disappear; add TTL later if useful.

---

## Phase 6 — Channelarr-Side Tagging

**Context:** Channelarr is a separate repo that creates channels (resolver-driven, not M3U). Tagging there is a separate concern but should share the same vocabulary as manifold so categories align when both feeds merge downstream.

**Scope:**
- Mirror the tag rule schema from Phase 1 — same keyword-list structure, same priority order.
- Apply on channel creation / edit in channelarr.
- No number ranges in channelarr (manifold owns numbering on the combined output).

**Open question:** Should tag rules be shared via a common config source (e.g., both apps read from the same YAML on the host) or maintained independently? Shared is less duplication but couples the two apps. Independent-with-same-schema is more flexible. Recommend independent for now; revisit if drift becomes annoying.

**This phase is scoped separately — not touched until Phases 1-5 land.**

---

## Settings Shape (Reference)

```yaml
tag_rules:
  priority: [event, sports, news, movies, kids, live]
  sports:
    keywords: [espn, nfl, nba, mlb, nhl, ufc, fight]
  news:
    keywords: [cnn, fox news, msnbc, bbc, sky news]
  movies:
    keywords: [hbo, showtime, cinemax, movie]
  kids:
    keywords: [disney, nickelodeon, cartoon, pbs kids]
  ppv:
    keywords: [ppv, pay-per-view]

number_ranges:
  event:  {start: 500,  end: 599}
  sports: {start: 1000, end: 1999}
  news:   {start: 2000, end: 2099}
  movies: {start: 3000, end: 3999}
  kids:   {start: 4000, end: 4099}
  live:   {start: 5000, end: 5999}

activation_rules:
  tags_auto_on: [event, sports, news, live]
```

---

## Deferred / Out of Scope

- LLM-assisted tagging for unmatched channels (explicitly deferred per user — no infra).
- Jellyfin category/tag injection during rebind (separate follow-up; the rebind path already reads `channel_number` so Phase 2 improves it for free, but injecting tags as Jellyfin categories is its own feature).
- UI for editing tag rules / number ranges (API endpoints first; UI later).
