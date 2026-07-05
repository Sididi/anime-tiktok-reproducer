# Manual Custom-Time Upload & Slot Switching — Design

**Date:** 2026-07-05
**Status:** Approved pending user review

## Overview

Two additions to the slot-based upload scheduling system:

1. **Manual custom time** — the user can schedule an upload at an arbitrary exact
   datetime, entirely outside the slot system: it neither blocks slots nor is
   blocked by them, but is displayed and treated normally everywhere else
   (planning, upload pipeline, platform publish times, VPS reminders).
2. **Slot switching** — in the slot pickers, slots taken by another project are
   shown in a distinct (amber) style and are clickable. Clicking opens a
   confirmation popup previewing the displacement; the user chooses between a
   **chain cascade** or a **next-free-slot** fallback for the displaced
   project(s). Displacement is propagated to every platform's API for
   already-uploaded videos.

## Decisions made (with user)

| Question | Decision |
|---|---|
| Manual scope across platforms | One exact datetime applied to all platforms in scope |
| Manual UX entry point | Inside the existing SlotPickerPopover (custom-time field below chips) |
| Jitter on manual times | None — publish exactly at typed time |
| Collision with slot system | Fully allowed; non-blocking ±1 h proximity warning in the picker |
| Displacement rule | User picks per switch, in the confirmation popup: chain cascade OR next-free-slot |
| Switch surfaces | All three: anchor TikTok chips, per-platform override rows, single-platform reschedule picker |
| Displacement span | Only the stolen platform's schedule moves; the occupant's other platforms stay |
| YouTube quota safety | No hard cap; popup shows a quota warning when >10 already-uploaded YT videos would move |

## Verified platform API constraints (2026-07)

- **YouTube**: `videos.update` ≈ 50 quota units; default budget 10,000/day
  (shared with uploads at ~100 units each). Worst-case 60-video chain =
  3,000 units. Displacement notify only fires for already-uploaded videos.
- **Facebook**: page-level limit 4,800 calls/engaged-user/24 h — non-binding.
- **Instagram / TikTok**: reschedules are PATCHes to our own VPS server
  (`/api/internal/jobs/{id}/slot`) — no external quota.
- Chain length is bounded by contiguous occupied slots; the chain stops at the
  first free slot.

## Feature 1 — Manual custom time

### Data model

- `PlatformSchedule` (backend/app/models/project.py) gains `manual: bool = False`.
- A manual entry stores the exact user-chosen time in both `slot` and
  `scheduled_at` (no jitter).

### Pool exclusion (the core mechanism)

- `SchedulingService._collect_pool_reservations()` skips `manual=True` entries.
- The occupancy maps built inside `compute_cascade` (urgent flow) and the new
  `compute_switch` also skip manual entries.
- Consequences, all intentional:
  - manual entries never appear as "taken" in free-slots listings;
  - the slot system can later book the same time (allowed by design);
  - cascades and switches never displace a manual upload;
  - a slot datetime "held" by a manual upload is bookable normally.
- Everything else reads `platform_schedules` unchanged and keeps working:
  planning events, upload pipeline reuse path, YT/FB publish-at, VPS reminder
  sync, cancellation, reschedule_pending retry loop.

### Service & API

- `SchedulingService.reserve_manual(project_id, account_id, at: datetime,
  platforms: list[str])`:
  - validates `at ≥ now + 30 min` (MIN_LEAD) — 422 `slot_too_close` otherwise;
  - no slot-config check, no pool check;
  - writes `PlatformSchedule(slot=at, scheduled_at=at, manual=True)` for each
    platform, sets `scheduled_account_id`, recomputes aggregates, saves;
  - idempotent/overwriting: calling again replaces the manual entries — this is
    also the edit path.
- Route: `POST /scheduling/projects/{id}/reserve-manual`
  `{account_id, at, platforms?}` (platforms defaults to the account's
  configured platforms via `_platforms_to_reserve`).
- `PlanningEvent` gains `manual: bool`.

### Frontend

- **SlotPickerPopover**: new "Heure personnalisée" row under the slot chips —
  `HH:MM` input for the selected calendar day + activation toggle.
  - When active: chips deselect; anchor-mode auto-resolve preview and override
    rows hide (one time for all platforms); submit label becomes
    "Programmer (manuel)" and calls `reserve-manual`.
  - Non-blocking warning when the typed time is within ±1 h of an existing
    reservation that day (computed from already-fetched day slots).
  - Days struck as "full" become selectable while custom time is active
    (past days stay blocked).
- **Planning**: manual events get an "M" badge + dashed border
  (PlanningCalendar) and a "Programmation manuelle" line (EventPopover).
  Rescheduling a manual event reopens the picker in custom-time mode
  pre-filled.

## Feature 2 — Slot switching

### Service

- `SchedulingService.compute_switch(project_id, account_id, platform, slot)`
  → `SwitchResult` containing:
  - `cascade` plan: chain walk starting at the stolen slot through contiguous
    occupied slots (reuses the urgent-cascade walk logic, generalized to start
    at an arbitrary slot); list of `DisplacedItem(from_slot, to_slot, …)`.
  - `next_free` plan: single move — occupant jumps to the first genuinely free
    configured slot after its current one.
  - `blockers`: `pool_busy` (occupant's pool has a running/queued upload job),
    `pool_full` (no landing slot within lookahead), `facebook_horizon_exceeded`
    (a move would exceed FB's 29-day scheduling window). A blocker can apply to
    one plan only (e.g. cascade blocked, next_free fine).
  - `uploaded_count`: how many displaced projects already have an uploaded
    video on this platform (drives the YT quota warning).
- `SchedulingService.apply_switch(project_id, account_id, platform, slot,
  mode: "cascade" | "next_free")`, under `_reservation_lock`:
  1. recompute the plan; **verify the slot's occupant is unchanged** since the
     preview — otherwise raise `slot_state_changed` (HTTP 409, UI re-previews);
  2. abort if the chosen plan has blockers;
  3. move displaced projects farthest-first (no transient double-booking);
  4. write the stolen slot onto the switching project (jittered
     `scheduled_at`, as for any slot reservation);
  5. save everything, return the applied plan.
- Route layer notifies each displaced project via the existing
  `PlatformRescheduleService.notify` (YT `publishAt`, FB
  `scheduled_publish_time`, IG/TT via VPS PATCH); failures land in the existing
  `reschedule_pending` retry queue. Notification statuses are returned in the
  response.

### API

- `POST /scheduling/projects/{id}/switch-preview`
  `{account_id, platform, slot}` → both plans + blockers + uploaded_count.
- `POST /scheduling/projects/{id}/switch-apply`
  `{account_id, platform, slot, mode}` → applied plan + notification statuses.
  Used by the single-platform reschedule picker. Also serves "reschedule self
  onto a taken slot": the switching project's old entry on that platform is
  freed as part of the same lock.
- **Anchor flow atomicity**: `resolve-anchor` / `reserve-anchor` gain optional
  `steals: {platform: mode}`. `reserve_anchor` displaces the stolen platforms'
  chains first, then runs normal anchor resolution against the freed slots —
  one lock, no window where the steal succeeded but the anchor failed. If any
  part fails, nothing is persisted (compute-then-write, same as apply_switch).
  `resolve_anchor` with steals returns the displacement plan(s) so the popup
  can show them before the final "Schedule".

### Frontend

- **SlotChips** — three states:
  - free: current style;
  - impossible (`slot < now + 30 min`): struck-through, disabled — also fixes
    the latent bug where today's past slots are selectable and fail at submit;
  - taken by a project: **amber** border/text, clickable, tooltip
    "Occupé par «{title}» — cliquer pour échanger". (Free-slots API already
    returns `taken_by_project_id`; add the occupant title to the response.)
- **SlotSwitchConfirmModal** (new, styled like UrgentCascadeModal): occupant
  name, the two plans side by side (cascade: full move list + count + YT quota
  warning when `uploaded_count > 10` on youtube; next-free: the single landing
  slot), buttons **"Cascader (N vidéos)"**, **"Slot libre suivant (1 vidéo)"**,
  "Annuler". A plan with a blocker is shown disabled with the reason.
- Wiring per surface:
  - **single-platform picker**: confirm → `switch-apply` directly;
  - **anchor TT chips & override rows**: confirm stores the steal locally (chip
    selected + amber swap badge); the steal executes on final "Schedule" via
    `reserve-anchor.steals`. The resolve preview reflects pending steals.
- Post-switch toast summarizing moved projects; `pending_retry` statuses shown
  as "sera resynchronisé automatiquement".

## Edge cases

1. Stale preview → 409 `slot_state_changed`, modal re-fetches preview.
2. Occupant mid-upload → `pool_busy` blocker (existing guard reused).
3. Clicking a slot the project itself holds → not amber, no-op.
4. FB 29-day horizon blocks only the offending plan.
5. Urgent cascade also skips manual entries (same map exclusion).
6. Manual time < now+30 min → UI-disabled + API 422.
7. Only the stolen platform's entry moves; `_recompute_aggregates` keeps the
   project-level `scheduled_at` coherent.
8. VPS server needs no changes: manual and switched TT/IG times flow through
   the existing `_patch_server_slot`.

## Testing

- **test_scheduling_service.py**: manual exclusion from pool /
  free-slots / cascade / switch maps; `reserve_manual` validation + overwrite;
  `compute_switch` chain & next-free correctness, blockers, manual skipping;
  `apply_switch` atomicity, occupant re-verification, farthest-first ordering;
  `reserve_anchor` with steals (atomic success and all-or-nothing failure).
- **test_scheduling_routes.py**: new routes' status codes (409/422 mapping),
  notification dispatch per displaced project (mocked), response shapes.
- **test_platform_reschedule_service.py**: already covers YT/FB/IG/TT notify
  paths — extend only if response handling changes.
- **Frontend e2e** (upload-split-button.spec.ts / planning.spec.ts): custom
  time flow; amber chip → modal → both confirm modes; manual badge in
  planning; steal-within-anchor flow.

## Out of scope

- Displacing manual uploads (impossible by design).
- Re-anchoring all platforms of a displaced project.
- Hard cap on chain length (user chooses next-free instead).
- Any VPS server changes.
