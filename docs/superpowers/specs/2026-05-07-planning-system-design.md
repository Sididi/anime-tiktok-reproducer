# Planning System — Design

**Date:** 2026-05-07
**Status:** Brainstormed, awaiting plan

## Context

The slot reservation system in `SchedulingService` is complete: it finds the next free slot per `(account, platform)` pool, applies a ±30 minute jitter, and persists the reservation on `Project.platform_schedules`. Today the only entry point is the Upload button in the Project Manager, which always reserves the next free slot automatically. There is no way to:

- See what is scheduled across accounts/platforms.
- Pick a specific slot at upload time.
- Insert an "urgent" upload that displaces existing reservations.

This spec covers all three additions, and the platform-side notifications required when a slot moves after the video has already been pushed to YouTube/Facebook/Instagram.

## Goals

1. **Planning view** — a forward-looking week calendar across all accounts/platforms, with filters and per-event actions (cancel, reschedule).
2. **Manual slot picker** — the user picks a TikTok-anchored slot at upload time when needed; auto remains the one-click default.
3. **Urgent upload mode** — takes the nearest slot regardless of reservations, cascades existing reservations forward, and propagates the new times to YT/FB/IG via their APIs.

## Non-goals (out of scope for v1)

- Month/day calendar views (week only).
- Showing already-uploaded videos in the Planning (forward-looking only).
- Drag-and-drop reschedule inside the Planning calendar — replaced by an explicit "Reschedule" action that opens the same picker.
- Notifications when a project is cascaded by someone else.
- iCal export, Google Calendar sync.
- Multi-account/role permission model.

## High-level architecture

```
LibraryHeader
 ├─ "Projects" button       → ProjectManagerModal (existing)
 └─ "Planning" button (new) → PlanningModal (new)

ProjectManagerModal
 └─ ProjectRow
     └─ UploadSplitButton (replaces inline Upload button)
        ├─ default click → Upload now (auto)               → existing flow
        ├─ ▾ → "Schedule for specific slot…" → SlotPickerPopover
        ├─ ▾ → "Upload urgently"             → UrgentCascadeModal
        └─ both new modes call new SchedulingService methods
           BEFORE the existing copyright/duration/enqueue flow

PlanningModal
 ├─ AccountSelectorDropdown (reused from project-manager)
 ├─ PlatformCheckboxes (new, multi-select with select-all)
 └─ PlanningCalendar (ScheduleX week view, FR locale, Europe/Paris tz)
     └─ EventPopover (click) → Reschedule (opens SlotPickerPopover)
                            → Cancel slot / Cancel all
```

Backend additions live in:

- `backend/app/services/scheduling_service.py` — extended with new methods.
- `backend/app/services/platform_reschedule_service.py` — **new**, calls YT/FB/server-IG APIs to update or cancel a scheduled publish.
- `backend/app/services/reschedule_retry_service.py` — **new**, async loop that retries failed platform notifications with exponential backoff.
- `backend/app/api/routes/scheduling.py` — **new**, public REST surface.
- `server/app/api/internal.py` — extended with `PATCH /jobs/{project_id}/slot` and `DELETE /jobs/{project_id}` for the Instagram side.

The model `Project` gains one new field: `reschedule_pending: dict[str, dict] = {}`.

## Frontend design

### LibraryHeader change

`frontend/src/components/library/LibraryHeader.tsx` adds a third button between "Projects" and the purge icon, using `CalendarDays` from lucide-react. Clicking it opens `PlanningModal`.

### Planning modal

**File layout (new directory):**

```
frontend/src/components/planning/
├── PlanningModal.tsx
├── PlanningHeader.tsx
├── PlatformCheckboxes.tsx
├── PlanningCalendar.tsx
├── EventPopover.tsx
├── platformColors.ts
└── types.ts
```

**Behaviour:**

- Modal opens at `max-w-7xl h-[88vh]` to give the calendar room.
- Header reuses `AccountSelectorDropdown` ("All Projects" by default) and adds a `PlatformCheckboxes` group: `[All] [YT] [FB] [IG] [TT]`.
- Filters persist in localStorage (`atr.planning.account_id`, `atr.planning.platforms`).
- Calendar is a `@schedule-x/react` week view, locale `fr-FR`, Europe/Paris timezone, week starts Monday.
- Events are coloured by platform via custom calendars in ScheduleX:
  - YouTube: `hsl(268, 76%, 58%)` (violet)
  - Facebook: `hsl(220, 76%, 50%)` (blue)
  - Instagram: `hsl(35, 91%, 55%)` (orange)
  - TikTok: `hsl(330, 81%, 60%)` (pink)
- Each event renders the project title and the account avatar inside the block (custom `timeGridEvent` component).
- Times shown are clean slots (`14:00`, `18:00`); the ±30 minute jitter is never displayed to the user.
- Past slots (slot < now) are not loaded.
- Clicking an event opens `EventPopover`, anchored to the event, with:
  - Project title + project_id, library type
  - Account avatar + name + platform badge
  - Slot time (clean), Drive folder link
  - Buttons: `Reschedule this slot`, `Reschedule whole project`, `Cancel slot`, `Cancel all`
- `Reschedule this slot` opens a single-platform variant of `SlotPickerPopover` showing only that platform's free slots (no anchor logic, no override section). On confirm it issues `PATCH /projects/{id}/platforms/{platform}`.
- `Reschedule whole project` opens the full TT-anchored picker pre-filled with the project's current TT slot. On confirm it issues `PATCH /projects/{id}/anchor`. This button is **disabled when the project has no TikTok reservation** (re-anchor requires TT, per the manual-scheduling rule).
- `Cancel slot` clears the platform's reservation on the project and notifies the platform.
- `Cancel all` clears every reservation on the project and notifies every relevant platform.

**Pool-aware account filtering:**

When the user selects account `A`, the events shown are those that share a pool with `A` for each platform. For each platform `p` we compute `account.pool_key_for(p)` for `A`, then keep events whose `scheduled_account_id` resolves to the same pool key. The existing `_collect_reserved_slots_for_pool` logic in `scheduling_service.py:42-59` already encodes this rule and is reused server-side via the `GET /events?account_id=...` endpoint.

**State:** local `useState`/`useMemo`. No global store. Re-fetch on modal open. SSE deferred to a later iteration.

### Upload split button

Replaces the inline `Upload` button at `frontend/src/components/project-manager/ProjectRow.tsx:74-93` with a `UploadSplitButton` component.

Visual contract:

- Left half: `↑ Upload` — same green button as today, single click triggers the existing auto flow with no extra interaction. This preserves the 80% case at zero added cost.
- Right half: `▾` — opens a menu with three rows:
  1. **Upload now (next free slot)** — green, sub-text shows the auto-resolved next TT slot computed at menu open.
  2. **Schedule for specific slot…** — blue, opens `SlotPickerPopover`.
  3. **Upload urgently (push others)** — red, separated by a divider, opens `UrgentCascadeModal`.

Disabled states:

- Option 2 disabled if the account has no TikTok configuration; tooltip explains that manual scheduling requires a TikTok-enabled account.
- Option 3 disabled when no compatible account is selectable for the project.

### Slot picker popover

`SlotPickerPopover` has two variants driven by a `mode` prop:

- `mode="anchor"` (default) — TT-anchored, used for "Schedule for specific slot…" in the upload flow and for "Reschedule whole project" in the Planning. Renders the layout below in full.
- `mode="single-platform"` — used for "Reschedule this slot" in the Planning. Renders only the mini calendar + slot chips for the target platform; hides the "Other platforms (auto)" preview and the override section. Submit calls a single-platform PATCH instead of `reserve-anchor`.

In `anchor` mode the popover renders:

1. Mini month calendar with prev/next chevrons.
2. Day cells are dimmed when no TT slot is available that day (account has no TT slots, or all are taken/past).
3. Below the calendar: TT slot chips for the selected day (`12:00`, `14:00`, `18:00`, `21:00`) with three states: selected (blue border), available (clickable), taken (strikethrough), too-close (greyed out, < `now + 30min`).
4. Below: live preview of "Other platforms (auto)" — `YT 14:00 · FB 14:00 · IG 14:00`. Recomputed on every TT change via `POST /api/scheduling/resolve-anchor`.
5. Collapsible `▸ Override per-platform` (advanced toggle): when expanded, shows one dropdown per other platform listing its next 20 free slots ≥ TT anchor. Changes in overrides re-call `resolve-anchor`.
6. Footer: `Cancel`, `Schedule` (disabled until a TT slot is selected and `conflicts` is empty for the chosen overrides).

Validation:

- If `resolve-anchor` returns conflicts (a platform cannot find a slot ≤ 90 days), the affected platform is shown in red inline; the user is offered to override.

### Urgent cascade modal

Triggered from option 3 of the split button.

1. Frontend calls `POST /api/scheduling/projects/{id}/cascade-preview`.
2. Modal renders the response:
   - For each platform, target slot of the urgent video and a tree of displaced projects: `↳ "Naruto 217" 14:00 → 18:00`.
   - A summary: `N project(s) will be shifted`.
   - Backend-returned `blockers` (running uploads, FB 29-day horizon, etc.) are surfaced as red errors; if non-empty, `Confirm` is disabled and a `Schedule manually instead` button switches to the picker.
3. `Confirm urgent upload` calls `POST /api/scheduling/projects/{id}/cascade-apply`. On success, the existing copyright/duration/enqueue flow takes over.

### Hook into existing upload flow

`startUploadWithChecks` in `frontend/src/components/project-manager/ProjectManagerModal.tsx:541` is extended with `mode: "auto" | "scheduled" | "urgent"` and an optional `anchorPayload`:

- `auto` — flow unchanged.
- `scheduled` — calls `reserveAnchor` first; if it succeeds, the existing copyright/FB/YT/enqueue flow runs untouched (the scheduler reuses the now-persisted `platform_schedules` via `_try_reuse_platform_reservation`).
- `urgent` — calls `cascadeApply` first; otherwise identical to `scheduled`.

Errors from `reserveAnchor`/`cascadeApply` abort the flow and surface in the existing session error banner.

### TypeScript types

```ts
type Platform = "youtube" | "facebook" | "instagram" | "tiktok";

interface PlanningEvent {
  project_id: string;
  anime_title: string;
  account_id: string;
  account_avatar_url: string;
  account_name: string;
  platform: Platform;
  slot: string;            // ISO, displayed clean time
  scheduled_at: string;    // ISO with jitter, hidden from UI
  drive_folder_url: string | null;
  status: "scheduled" | "running" | "complete";
}

interface FreeSlot {
  slot: string;                    // ISO
  available: boolean;
  taken_by_project_id?: string;
}

interface ResolveAnchorResult {
  resolved: Record<Platform, { slot: string; scheduled_at: string; available: boolean }>;
  conflicts: Array<{ platform: Platform; reason: string }>;
}

interface CascadePreview {
  per_platform: Array<{
    platform: Platform;
    target_slot: string;
    target_scheduled_at: string;
    displaced: Array<{
      project_id: string;
      anime_title: string;
      from_slot: string;
      to_slot: string;
      requires_platform_notification: boolean;
    }>;
  }>;
  blockers: Array<{ platform: Platform; reason: string }>;
}
```

### Library: ScheduleX

`@schedule-x/react` and `@schedule-x/calendar` (MIT, MIT). Configured with `views: [createViewWeek()]`, `locale: "fr-FR"`, `firstDayOfWeek: 1`, `timezone: "Europe/Paris"`. The custom `timeGridEvent` component renders the avatar + title.

CSS variables for the four platforms are added to `frontend/src/index.css` and applied through ScheduleX's `calendars: { [colorName]: { lightColors, darkColors } }` configuration so the theme matches the existing dark UI.

## Backend design

### Scheduling service extensions

All new methods live in the existing `SchedulingService` class so the lock and helper utilities are shared. Mutating methods take `_reservation_lock` exactly as the existing reservation methods do.

```python
class SchedulingService:
    @classmethod
    def find_free_slots_after(
        cls, account_id: str, platform: str, after: datetime, limit: int
    ) -> list[FreeSlot]: ...

    @classmethod
    def resolve_anchor(
        cls,
        account_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
    ) -> ResolveAnchorResult: ...

    @classmethod
    def reserve_anchor(
        cls,
        project_id: str,
        account_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
    ) -> dict[str, PlatformSchedule]: ...

    @classmethod
    def reschedule_platform(
        cls, project_id: str, platform: str, new_slot: datetime
    ) -> PlatformSchedule: ...

    @classmethod
    def reschedule_anchor(
        cls,
        project_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
    ) -> dict[str, PlatformSchedule]: ...

    @classmethod
    def cancel_platform_slot(cls, project_id: str, platform: str) -> None: ...

    @classmethod
    def cancel_all_slots(cls, project_id: str) -> None: ...

    @classmethod
    def compute_cascade(cls, project_id: str, account_id: str) -> CascadeResult: ...

    @classmethod
    def apply_cascade(cls, project_id: str, account_id: str) -> CascadeResult: ...
```

`compute_cascade` is a pure simulation; `apply_cascade` performs the simulation and persists every affected `Project` atomically under `_reservation_lock`, including the new reservation for the urgent project itself. It returns the same `CascadeResult` shape so the route can iterate over `displaced` and call `PlatformRescheduleService.notify` for each `(displaced_project, platform)` pair *outside* the lock. The urgent project itself does not need a notification — its `publishAt`/`scheduled_publish_time` is set when its upload runs immediately afterward.

The cascade algorithm, per platform:

1. Anchor = first slot in `account.slots_for(platform)` at `slot >= now + 30min` for the urgent video.
2. If that slot is free in the pool: stop, urgent gets it.
3. If taken: enqueue `(occupant_project, current_slot, next_slot)`. The occupant moves to the next configured slot ≥ its current. Repeat the same check on `next_slot`.
4. Continue until a free slot is reached or the lookahead window is exhausted (90 days, or 29 days for FB).
5. If exhausted: return a `blockers` entry for that platform.

The cascade is contained to one pool per platform, computed independently for each platform of the urgent project.

### Platform notification service

`backend/app/services/platform_reschedule_service.py` (new):

```python
class PlatformRescheduleService:
    @classmethod
    def notify(
        cls, project: Project, platform: str, new_scheduled_at: datetime
    ) -> NotificationResult: ...

    @classmethod
    def cancel(cls, project: Project, platform: str) -> NotificationResult: ...
```

- **YouTube** — `videos.update(part="status", body={"status": {"privacyStatus": "private", "publishAt": iso}})` for reschedule; `publishAt=None` + `privacyStatus="private"` for cancel. Uses the existing `AccountService.get_youtube_credentials`.
- **Facebook** — Graph API `POST /{video_id}` with `scheduled_publish_time=epoch` for reschedule; `published=false` + remove `scheduled_publish_time` for cancel. Uses `AccountService.get_meta_credentials`.
- **Instagram** — `PATCH /api/internal/jobs/{project_id}/slot` (new endpoint on `/server/`) with `slot_time` and `platform_scheduled_at["instagram"]` for reschedule; `DELETE /api/internal/jobs/{project_id}` for cancel.
- **TikTok** — no-op.

The `video_id` is parsed from `Project.upload_last_result.platforms[platform].url`. If absent (no upload yet), the notification is skipped — the local `platform_schedules` change is enough since no upload has been pushed to the platform.

`NotificationResult = {"status": "ok"|"pending_retry"|"skipped", "error": str | None}`.

On failure (HTTP error, OAuth refresh failure, etc.), the service returns `pending_retry` and the caller flags `Project.reschedule_pending[platform] = {target_scheduled_at, retries: 0, last_error, last_attempt_at}`.

### Retry service

`backend/app/services/reschedule_retry_service.py` (new) — an async loop started from `main.py`:

- Polls every 60 seconds.
- For each `Project` with non-empty `reschedule_pending`, retries each entry whose `last_attempt_at + backoff` ≤ now.
- Exponential backoff steps: 1m, 2m, 5m, 15m, 1h.
- On success: removes the entry.
- After 5 failed attempts: keeps the entry, logs `critical`, posts a Discord alert via `discord_service.py`.

### REST API

New router `backend/app/api/routes/scheduling.py`, prefix `/api/scheduling`.

```
GET    /events
GET    /free-slots
POST   /resolve-anchor
POST   /projects/{project_id}/reserve-anchor
PATCH  /projects/{project_id}/platforms/{platform}
PATCH  /projects/{project_id}/anchor
DELETE /projects/{project_id}/platforms/{platform}
DELETE /projects/{project_id}/all
POST   /projects/{project_id}/cascade-preview
POST   /projects/{project_id}/cascade-apply
GET    /reschedule-pending
```

Request/response shapes match the TypeScript types above. Error codes:

- `409 pool_busy` — a project in the targeted pool is `running`/`queued`.
- `409 job_running` — the project itself has a running upload job.
- `409 tiktok_slot_taken` — picker without urgent mode.
- `422 slot_too_close` — slot < `now + 30min`.
- `422 invalid_slot` — slot not in the configured slot list for that day/platform.
- `422 tiktok_required` — manual scheduling requested without TT config.
- `422 pool_full` — cascade would exceed 90 days.
- `422 facebook_horizon_exceeded` — cascade would exceed FB's 29-day window.

Cancellation and reschedule endpoints atomically update local state under `_reservation_lock`, then trigger platform notifications outside the lock. The HTTP response includes `notification_status` per affected `(project, platform)` so the UI can surface "applied locally; platform notification pending retry" when relevant.

### Project model change

```python
class Project(BaseModel):
    ...
    reschedule_pending: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # key = platform, value = {
    #     "target_scheduled_at": datetime,
    #     "retries": int,
    #     "last_error": str,
    #     "last_attempt_at": datetime,
    # }
```

### Server-side (`/server/`)

`server/app/api/internal.py`:

```python
class UpdateSlotRequest(BaseModel):
    slot_time: datetime
    platform_scheduled_at: dict[str, datetime] | None = None

@router.patch("/jobs/{project_id}/slot")
async def update_job_slot(...): ...

@router.delete("/jobs/{project_id}")
async def delete_job(...): ...
```

The existing `reminder_scheduler` already drives off `Job.slot_time`/`Job.platform_scheduled_at`, so updating those fields is sufficient — the next scheduler tick recomputes the due time without restart.

## Edge cases & validation

1. Slot < `now + 30min` — `422 slot_too_close`.
2. Manual schedule on a non-TT account — `422 tiktok_required`.
3. Picker submits a TT slot already taken — `409 tiktok_slot_taken` (urgent mode bypasses).
4. Override slot not in the platform's configured list for that day — `422 invalid_slot`.
5. Cascade exceeds 90 days for any platform — `422 pool_full` with offending platform.
6. Cascade exceeds 29 days for Facebook — `422 facebook_horizon_exceeded`.
7. Pool has a `running`/`queued` upload job — `409 pool_busy` with blocker list.
8. Single-platform reschedule on a project whose job is `running` — `409 job_running`.
9. Notification fails 5 times in a row — log `critical`, Discord alert, leave `reschedule_pending` set for manual ops.

## Tests

**Frontend (Playwright)** — `frontend/e2e/planning.spec.ts`:

- Render `PlanningModal` with mocked events; verify per-platform colours and positions.
- Click event → popover shows correct fields.
- Reschedule from popover opens the picker pre-filled with the current TT slot.
- Cancel slot → confirmation modal → mocked API → event disappears.
- Filter by platform: unchecking YT hides all YT events.
- Filter by account: only that account's events plus pool-shared platforms remain.

**Frontend (Playwright)** — `frontend/e2e/upload-split-button.spec.ts`:

- Auto path unchanged: a single click triggers the existing copyright check.
- Manual mode: open menu, pick a slot, mock `reserveAnchor`, observe upload flow continues.
- Urgent mode: open menu, mock `cascade-preview`, confirm, mock `cascade-apply`, observe upload flow continues.
- Urgent mode with blockers: confirm button disabled.

**Backend (pytest)** — `backend/tests/test_scheduling_service.py`:

- `find_free_slots_after` returns N slots in order, excludes taken ones.
- `resolve_anchor` resolves YT/FB/IG to first slot ≥ TT, available.
- `resolve_anchor` with taken YT falls back to next free slot.
- `reserve_anchor` writes `platform_schedules`; second call is idempotent via `_try_reuse_platform_reservation`.
- `cancel_platform_slot` removes the platform; `_recompute_aggregates` updates `scheduled_at`.
- `compute_cascade` simple (1 displaced), chained (3 displaced), 90-day overflow, 29-day FB overflow.
- `apply_cascade` mutates atomically; new jitter applied; `running` project in pool returns 409.
- `reschedule_platform` frees old slot, reserves new one, triggers notification.

**Backend (pytest)** — `backend/tests/test_platform_reschedule_service.py`:

- Mock `googleapiclient` — `reschedule_youtube` calls `videos().update()` with the correct body.
- Mock `httpx` — `reschedule_facebook` issues the right Graph API POST.
- Mock `httpx` — `reschedule_instagram` issues a PATCH to `/api/internal/jobs/{id}/slot`.
- Cancel paths: YT sets `privacyStatus=private` and clears `publishAt`; FB sets `published=false`; IG triggers DELETE.

**Backend (pytest)** — `backend/tests/test_reschedule_retry_service.py`:

- Loop polls, retries with backoff, succeeds on attempt 3 → entry removed.
- 5 failures → Discord alert called, entry retained.

**Server (pytest)** — `server/tests/test_internal_jobs_slot.py`:

- `PATCH /api/internal/jobs/{id}/slot` updates `Job.slot_time`.
- `DELETE /api/internal/jobs/{id}` removes the job.
- Reminder scheduler picks up the new `slot_time` on the next tick.

## Operational concerns

- Feature flag env var `ATR_SCHEDULING_V2_ENABLED=true|false`. When false, the split button collapses to the legacy single-button behaviour and the Planning button is hidden. The auto upload path is untouched either way.
- Structured log lines for cascade events: `[cascade] project=X account=Y displaced=N platforms=[...] notifications_pending=M`.
- Retry failures beyond the 5th attempt post a Discord alert.
- A small per-day counter of cascades applied is logged for visibility.

## Migration

- `Project.reschedule_pending` defaults to `{}`. Pydantic accepts existing JSON files without modification.
- No schema migration required.
- ScheduleX, `@schedule-x/calendar`, and `@schedule-x/theme-default` are added to `frontend/package.json`.

## Open questions

None remaining at design time. Implementation will surface concrete details about ScheduleX styling and any Graph API error shapes that need handling.
