# TikTok publishing via Post for Me native scheduling

**Date:** 2026-07-10
**Status:** Approved
**Supersedes:** the 10-minute head-start mechanism from `2026-07-08-tiktok-url-and-headstart-design.md` (committed in `abb7bde`, never deployed).

## Problem

TikTok posts land 4.6–24.2 minutes late (median +11.5 min) relative to their
user-facing slot. Measured on the VPS across the 12 Post-for-Me-era publishes
(2026-07-05 → 2026-07-10):

1. **The 10-min head start never ran in production.** It was committed 07-08
   (`abb7bde`) but the deployed Docker image was built 07-04. Every publish
   fired at slot time.
2. **PFM processing is highly variable**: post creation → live on TikTok takes
   2.6–9.9 min (median 7.7). A fixed lead can never cancel a ×4-spread delay.
3. **The scheduler loop is fully sequential**: one 30 s tick awaits each
   platform of each job in turn. Same-slot jobs queue behind each other's
   Instagram (2–4 min each) and TikTok (up to 10 min each) publishes.
   Observed: second TikTok of a shared slot started 15 min after its due time.
4. Drive download + PFM media upload add only ~15–60 s (not the bottleneck,
   but they sit in the critical window today).

## Solution overview

Stop trying to time an immediate publish. Use PFM's native scheduling:
`POST /v1/social-posts` accepts `scheduled_at` (ISO 8601; omitted = post
instantly — today's behaviour). PFM then fires server-side at the exact slot,
the same trust model as YouTube `publishAt` / Facebook scheduled videos.

Split the TikTok pipeline into three independently-due phases and make the
scheduler dispatch concurrent.

## Server changes

### 1. `post_for_me_publisher.py` — split the monolith

Refactor `publish_to_tiktok` into three functions sharing the persisted
`TikTokPublishState` (which gains stage `post_scheduled`):

- **`stage_media`** — Drive download → `create-upload-url` → PUT →
  persist `media_url`, stage `media_uploaded`. (Reuses `_upload_media`.)
- **`create_scheduled_post`** — `POST /social-posts` with
  `scheduled_at = <tiktok scheduled time, UTC ISO>`; persist `post_id`,
  stage `post_scheduled`. **Late-job rule:** if `sched − now < 60 s` at call
  time, omit `scheduled_at` (instant publish, today's behaviour).
- **`poll_post_result`** — existing 15 s poll of `social-post-results` until
  a result exists; stages `published`/`failed` unchanged.

Double-post guard unchanged: a live `post_id` is polled, never re-created;
a new post is only created after a definitive `failed` result, reusing
`media_url`.

### 2. `reminder_scheduler.py` — per-phase due times

New constant `TIKTOK_SCHEDULE_LEAD_MINUTES = 15` (replaces
`TIKTOK_LEAD_MINUTES = 10`; must equal `TIKTOK_EDIT_LOCK_MINUTES` in
`backend/app/services/scheduling_service.py`).

With `sched = platform_scheduled_at["tiktok"] or slot_time`:

| Phase | Due | Retry |
| --- | --- | --- |
| stage_media | immediately on job arrival | every tick, own attempts counter; failure ping if still failing at `sched − 15 min` |
| create post | `sched − 15 min` | every tick, up to 5 attempts (existing counter) |
| poll result | `sched` | resumable; existing attempts/timeout semantics |

`completed_at`, Discord embed re-render, and failure pings keep today's
semantics (based on the poll-result outcome).

### 3. Concurrent dispatch

`dispatch_due_actions` spawns `asyncio.create_task` per due (job, platform)
action instead of awaiting inline, guarded by an **in-memory in-flight set**
keyed on `(project_id, platform)` so a tick never double-dispatches.

Invariant change: today `status == "uploading"` at tick start means "crashed
mid-publish, re-dispatch". Under concurrency the in-flight set is the
liveness signal; `uploading` **and not in-flight** (i.e. after a process
restart, when the set is empty) is the crash-recovery case. Instagram gains
the same concurrency for free.

### 4. Cancellation / reschedule

- Before `sched − 15 min`: no PFM post exists; nothing to do. Staged media
  simply sits unused in PFM storage if the job is deleted.
- After `sched − 15 min`: the backend edit-lock (now 15 min) already forbids
  slot changes and caption edits. Job **deletion** inside the window
  additionally issues `DELETE /v1/social-posts/{post_id}` when a
  `post_scheduled` state exists.

## Backend changes

- `TIKTOK_EDIT_LOCK_MINUTES: 10 → 15` in
  `backend/app/services/scheduling_service.py` (caption freezes when the
  scheduled post is created). Update the cross-file comment to point at
  `TIKTOK_SCHEDULE_LEAD_MINUTES`.

## Out of scope (YAGNI)

- PFM webhooks (`social.post.result.created`) — post-slot polling is fine and
  already resumable.
- Adaptive/measured timing.
- Any Instagram pipeline change beyond gaining concurrent dispatch.

## Deployment

The VPS image is stale (built 07-04; server code from 07-08 never shipped).
After merge, rebuild/redeploy per `server/DEPLOYMENT.md`. Expected outcome:
TikTok goes live at `slot + PFM fire→live tail` with media pre-staged
(observed floor ~2.6 min), instead of today's `slot + 4.6…24 min`, and
same-slot jobs no longer serialize.

## Testing

- Unit tests per phase: due-time gating for the three phases, late-job
  instant-publish rule, media-staging retry, create-post retry.
- Concurrency: in-flight guard (no double dispatch within/across ticks),
  crash-recovery re-dispatch after restart, two same-slot jobs dispatch in
  the same tick.
- Cancellation: delete-in-window issues the PFM DELETE; delete before the
  window does not.
- Existing publisher tests adapted to the split functions; backend
  edit-lock tests updated for 15 min.
