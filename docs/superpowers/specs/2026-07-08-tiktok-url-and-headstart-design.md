# TikTok URL fix + 10-minute head-start & edit-lock — Design

**Date:** 2026-07-08
**Status:** Approved in conversation (Parts A, B, C all confirmed by Sid; Part C's constructed URL verified against a live post).

Three related refinements to the TikTok auto-upload flow (merged 2026-07-05). Independent enough to build in one plan; grouped because they all concern the TikTok publish path.

## Part A — TikTok 10-minute internal head-start

**Goal:** TikTok publishes ~10 minutes *before* its user-facing slot, invisibly. TikTok is the priority platform and its processing takes ~10 min; posting early guarantees it is never *later* than the other platforms, at the cost of being potentially slightly earlier.

**Mechanism — one constant, one place (VPS server only):**
- Add `TIKTOK_LEAD_MINUTES = 10` in `server/app/services/reminder_scheduler.py`.
- In `_platform_due_time(job, platform)`, when `platform == "tiktok"`, return `stored_time - timedelta(minutes=TIKTOK_LEAD_MINUTES)`. This is the single point where the head-start is applied — it shifts the *due comparison*, nothing else.
- Everything else keeps the true user-facing time: backend slot reservations, the `platform_scheduled_at` the backend sends to the VPS, the value stored in `jobs.json`, the Discord embed, the planning UI. The head-start is never stored, so it is invisible.
- Instagram/YouTube/Facebook are unaffected (`_platform_due_time` only shifts tiktok).
- Reschedules need no special handling: the backend keeps sending the true time; the VPS subtracts the lead on its own.
- Since the embed footer shows `slot_time` (not the per-platform tiktok time), nothing leaks into Discord.

**Edge:** if the user-facing tiktok time is already within 10 min of now (e.g. a near-immediate manual post), `stored − 10min` is in the past → fires on the next tick. This is the intended "never late" behavior.

## Part B — 10-minute edit-lock (keyed on TikTok time)

**Goal:** Once a project's TikTok posting has internally begun, forbid changing the project's timing. "Begun" = `now ≥ (tiktok user-facing slot) − 10min` — exactly the head-start boundary, so the lock window *is* the internal dispatch moment.

**Backend (authoritative) — `backend/app/services/scheduling_service.py`:**
- Add `TIKTOK_EDIT_LOCK_MINUTES = 10` and a helper `_tiktok_timing_locked(project, *, now=None) -> bool`: true iff the project has a tiktok schedule and `now ≥ tiktok.scheduled_at − TIKTOK_EDIT_LOCK_MINUTES`. Projects with no tiktok schedule → never locked (fall back to existing status-based locks).
- Guard the two "change existing timing" service methods — `reschedule_platform` and `reschedule_anchor` — at their top: if `_tiktok_timing_locked(project)` raise `ValueError("timing_locked")`. (Initial `reserve_anchor`/`reserve_manual` are not guarded: a fresh reservation has no existing near-now tiktok time, and minimum-lead is already enforced by `_earliest_allowed_publish_time`.)
- Route `backend/app/api/routes/scheduling.py`: in `patch_platform` and `patch_anchor`, map `ValueError("timing_locked")` to HTTP **423 Locked** with body `"timing_locked"` (distinct from the existing 422/409 mappings).

**Events flag (so the UI doesn't recompute time):**
- `PlanningEvent` (in `scheduling.py`) gains `timing_locked: bool`.
- `_build_planning_event` computes it once per project from the project's tiktok schedule via the same `_tiktok_timing_locked(project)` and stamps it on every event of that project.

**Frontend — `frontend/src/`:**
- `types/index.ts`: add `timing_locked: boolean` to the planning-event type.
- `EventPopover.tsx` / `PlanningModal.tsx`: disable the reschedule/anchor controls when `timing_locked` (in addition to the existing `status !== "scheduled"` lock), with reason text "La publication TikTok a commencé" (matches the existing French UI). Backend remains the enforcement authority — the flag is a UX affordance; a stale tab hitting the endpoint still gets 423.

**Scope note:** the lock is whole-project, keyed on tiktok time (per Sid). Other platforms keep only their existing status-based lock. Cancellation (`delete_platform`/`delete_all`) is *not* locked — only timing *changes* are.

**Cross-service coupling:** `TIKTOK_LEAD_MINUTES` (server) and `TIKTOK_EDIT_LOCK_MINUTES` (backend) are the same conceptual window and must both stay 10. They live in separate services (no shared module), so this is documented, not enforced in code.

## Part C — Real TikTok video URL (not the channel URL)

**Root cause (verified against a live post 2026-07-08):** PFM's `GET /v1/social-post-results` returns, at success, `platform_data.url = "https://www.tiktok.com/@<username>"` (channel URL) and never updates it to the video permalink — it was still the channel URL 20+ min after publish. But `platform_data.id` embeds the TikTok video id:

```
platform_data.id  = "v_pub_url~v2-1.7659653399897655318"
platform_data.url = "https://www.tiktok.com/@animespm2002"
→ real permalink   = "https://www.tiktok.com/@animespm2002/video/7659653399897655318"  (confirmed opens the posted video)
```

**Fix — `server/app/services/post_for_me_publisher.py`:**
- Add a pure helper `_derive_tiktok_video_url(platform_data: dict) -> str | None`:
  - If `platform_data["url"]` already matches `/video/\d+` → return it unchanged (future-proof if PFM ever fixes their url).
  - Else parse the username from the channel url (`@([A-Za-z0-9_.]+)`) and the video id from the trailing segment of `platform_data["id"]` (split on the last `.`; accept only an all-digit segment of length 18–19, the TikTok item-id shape).
  - If both parse, return `https://www.tiktok.com/@{username}/video/{video_id}`; otherwise return `None`.
- At the single success call site, replace `url = platform_data.get("url")` with `url = _derive_tiktok_video_url(platform_data) or platform_data.get("url")`. On any parse failure it falls back to today's channel URL — never a broken link, since a mis-shaped id fails the strict digit/length check.
- Log the raw `platform_data` and the derived url at INFO on success, so format drift is visible on the first posts.

**No new polling:** the existing poll-until-first-result loop is unchanged and already returns on the first successful result. Part C only transforms the id we already have on that result. The "keep polling after success to wait for the permalink" approach is explicitly rejected — PFM's result url never becomes the permalink, so it would poll forever.

## Testing

- **Part A:** `test_reminder_scheduler.py` — a tiktok job due at exactly `now + 9min` fires (because `−10min` makes it due), while `now + 11min` does not; instagram at `now + 9min` does *not* fire (no lead). Assert the stored `platform_scheduled_at` is untouched.
- **Part B backend:** `test_scheduling_service.py` — `reschedule_platform`/`reschedule_anchor` raise `timing_locked` inside the window and succeed outside it; project without tiktok is never timing-locked. `test_scheduling_routes.py` — patch endpoints return 423 when locked. `PlanningEvent.timing_locked` true inside window / false outside.
- **Part B frontend:** wire-up verified against the existing status-lock pattern; controls disabled + reason shown when `timing_locked`. (No new e2e required; mirror the existing lock's rendering.)
- **Part C:** `test_post_for_me_publisher.py` — `_derive_tiktok_video_url` for: the real sample (→ constructed permalink), an already-`/video/` url (→ unchanged), a non-digit / wrong-length id suffix (→ None → channel fallback), a missing/blank url (→ None). One end-to-end publish test asserting the returned `TikTokPublishResult.url` is the constructed permalink.

## Out of scope / follow-ups
- The pre-existing PFM "create-post 2xx without id" double-post window (tracked separately).
- The account-feed (`/v1/social-account-feeds`, needs `feeds` permission) is the fallback if PFM's `platform_data.id` format ever changes; not built now.
