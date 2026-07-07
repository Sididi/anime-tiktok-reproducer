# TikTok URL fix + 10-min head-start & edit-lock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the real TikTok video permalink in Discord (not the channel URL), publish TikTok 10 minutes before its user-facing slot, and lock a project's timing once that head-start has begun.

**Architecture:** Three independent changes. (C) The VPS publisher derives the `/video/<id>` permalink from the video id PFM embeds in `platform_data.id`. (A) The VPS scheduler subtracts a 10-min lead from the tiktok due-time comparison only. (B) The backend rejects reschedules once `now ≥ tiktok_time − 10min` (HTTP 423) and exposes a `timing_locked` flag the planning UI uses to disable its controls.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest (server: `asyncio_mode=auto`); React/TypeScript frontend.

**Spec:** `docs/superpowers/specs/2026-07-08-tiktok-url-and-headstart-design.md`

## Global Constraints

- The 10-minute value is the same conceptual window in two services and must both be 10: `TIKTOK_LEAD_MINUTES = 10` in `server/app/services/reminder_scheduler.py`, `TIKTOK_EDIT_LOCK_MINUTES = 10` in `backend/app/services/scheduling_service.py`. They live in separate services (no shared module).
- The head-start is applied ONLY at the VPS due-time comparison; it is never stored. Backend reservations, `platform_scheduled_at` sent to the VPS, `jobs.json`, the Discord embed, and the planning UI all keep the true user-facing time.
- The edit-lock is whole-project, keyed on the project's TikTok `scheduled_at`. Projects with no tiktok schedule are never timing-locked (existing status-based locks still apply). Cancellation is never timing-locked — only timing *changes* (reschedule) are.
- Part C must never ship a broken link: only construct a `/video/` URL when the id's trailing segment is all-digits and 18–19 long AND a username parsed; otherwise fall back to PFM's channel URL (today's behavior).
- Server tests: `cd server && uv run pytest` (no async markers needed). Backend tests: `pixi run -e dev test` from repo root (plain `pixi run test` lacks deps). Known pre-existing backend failure `test_scheduling_routes.py::test_list_events_returns_filtered_events` is unrelated — ignore it.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

### Task 1: Part C — derive the real TikTok video URL (VPS publisher)

**Files:**
- Modify: `server/app/services/post_for_me_publisher.py`
- Test: `server/tests/test_post_for_me_publisher.py`

**Interfaces:**
- Produces: `_derive_tiktok_video_url(platform_data: dict) -> str | None`.
- Consumes: the success branch's `platform_data` (already read at `post_for_me_publisher.py:322`).

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_post_for_me_publisher.py`:

```python
from app.services.post_for_me_publisher import _derive_tiktok_video_url


def test_derive_url_constructs_permalink_from_embedded_id():
    pd = {
        "id": "v_pub_url~v2-1.7659653399897655318",
        "url": "https://www.tiktok.com/@animespm2002",
    }
    assert _derive_tiktok_video_url(pd) == (
        "https://www.tiktok.com/@animespm2002/video/7659653399897655318"
    )


def test_derive_url_passes_through_existing_video_url():
    pd = {"id": "anything", "url": "https://www.tiktok.com/@a/video/12345"}
    assert _derive_tiktok_video_url(pd) == "https://www.tiktok.com/@a/video/12345"


def test_derive_url_none_when_id_not_video_id():
    # trailing segment is not an 18-19 digit id
    pd = {"id": "v_pub_url~v2-1.abc", "url": "https://www.tiktok.com/@a"}
    assert _derive_tiktok_video_url(pd) is None


def test_derive_url_none_when_id_wrong_length():
    pd = {"id": "v_pub_url~v2-1.123", "url": "https://www.tiktok.com/@a"}
    assert _derive_tiktok_video_url(pd) is None


def test_derive_url_none_when_url_missing_username():
    pd = {"id": "v_pub_url~v2-1.7659653399897655318", "url": ""}
    assert _derive_tiktok_video_url(pd) is None
```

Also add an end-to-end publish test (reuse this file's `fake`/`_publish` fixtures) asserting the channel-URL-plus-embedded-id result yields the constructed permalink:

```python
async def test_publish_returns_constructed_video_url(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {
              "id": "v_pub_url~v2-1.7659653399897655318",
              "url": "https://www.tiktok.com/@animespm2002",
          },
          "error": None}],
    ]
    result = await _publish(fake, tmp_path)
    assert result.success is True
    assert result.url == (
        "https://www.tiktok.com/@animespm2002/video/7659653399897655318"
    )
    assert result.publish_state.url == result.url
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && uv run pytest tests/test_post_for_me_publisher.py -v`
Expected: the new tests FAIL (`ImportError: cannot import name '_derive_tiktok_video_url'`).

- [ ] **Step 3: Implement**

In `server/app/services/post_for_me_publisher.py`, add `import re` near the top imports if not present, and add the helper next to the other module-level helpers (e.g. after `_result_error_detail`):

```python
_TIKTOK_VIDEO_URL_RE = re.compile(r"/video/\d+")
_TIKTOK_USERNAME_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)")


def _derive_tiktok_video_url(platform_data: dict[str, Any]) -> str | None:
    """Build the public /video/<id> permalink from PFM's result payload.

    PFM returns platform_data.url as the channel URL and never updates it to
    the video permalink, but embeds the TikTok video id in platform_data.id
    (e.g. "v_pub_url~v2-1.7659653399897655318"). Combine that id with the
    username parsed from the channel URL. Returns None when either cannot be
    parsed with confidence, so the caller falls back to the channel URL.
    """
    url = str(platform_data.get("url") or "")
    if _TIKTOK_VIDEO_URL_RE.search(url):
        return url  # PFM already gave us a permalink
    username_match = _TIKTOK_USERNAME_RE.search(url)
    if not username_match:
        return None
    trailing = str(platform_data.get("id") or "").rsplit(".", 1)[-1]
    if not (trailing.isdigit() and 18 <= len(trailing) <= 19):
        return None
    return f"https://www.tiktok.com/@{username_match.group(1)}/video/{trailing}"
```

Then change the success branch. Replace `post_for_me_publisher.py:322-323`:

```python
                if result.get("success"):
                    platform_data = result.get("platform_data") or {}
                    url = platform_data.get("url")
```

with:

```python
                if result.get("success"):
                    platform_data = result.get("platform_data") or {}
                    url = _derive_tiktok_video_url(platform_data) or platform_data.get("url")
```

And extend the existing success log (a few lines below) to include the raw payload so format drift is visible — change the `logger.info("PFM TikTok publish succeeded ...")` call to add `platform_data=%s`:

```python
                    logger.info(
                        "PFM TikTok publish succeeded post_id=%s url=%s "
                        "platform_data=%s elapsed=%.1fs",
                        post_id, url, platform_data, time.monotonic() - started,
                    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server && uv run pytest tests/test_post_for_me_publisher.py -v`
Expected: PASS (new + all pre-existing, including the happy-path test whose `platform_data.url` already contains `/video/` and so passes through unchanged).

- [ ] **Step 5: Commit**

```bash
git add server/app/services/post_for_me_publisher.py server/tests/test_post_for_me_publisher.py
git commit -m "fix(server): derive real TikTok video permalink from PFM platform_data.id"
```

---

### Task 2: Part A — TikTok 10-minute head-start (VPS scheduler)

**Files:**
- Modify: `server/app/services/reminder_scheduler.py`
- Test: `server/tests/test_reminder_scheduler.py`

**Interfaces:**
- Produces: module constant `TIKTOK_LEAD_MINUTES = 10`; `_platform_due_time(job, platform)` returns the tiktok due time minus the lead.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_reminder_scheduler.py` (reuse this file's existing job factory — Task-4 work added `_tiktok_job(...)`/`_make_job(...)`; use whichever the file defines to build a job, then set `platform_scheduled_at`):

```python
from datetime import UTC, datetime, timedelta

from app.services.reminder_scheduler import (
    TIKTOK_LEAD_MINUTES,
    _platform_due_time,
)


def test_tiktok_due_time_has_10min_lead():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job("p1")            # adapt to this file's factory
    job.platform_scheduled_at = {"tiktok": slot}
    assert TIKTOK_LEAD_MINUTES == 10
    assert _platform_due_time(job, "tiktok") == slot - timedelta(minutes=10)


def test_instagram_due_time_has_no_lead():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job("p1")
    job.platform_scheduled_at = {"instagram": slot}
    assert _platform_due_time(job, "instagram") == slot


def test_tiktok_lead_does_not_mutate_stored_time():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job("p1")
    job.platform_scheduled_at = {"tiktok": slot}
    _platform_due_time(job, "tiktok")
    assert job.platform_scheduled_at["tiktok"] == slot  # unchanged
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && uv run pytest tests/test_reminder_scheduler.py -v`
Expected: new tests FAIL (`ImportError: cannot import name 'TIKTOK_LEAD_MINUTES'`).

- [ ] **Step 3: Implement**

In `server/app/services/reminder_scheduler.py`: ensure `timedelta` is imported (change `from datetime import UTC, datetime` to `from datetime import UTC, datetime, timedelta`). Add the constant next to the other module constants (near `_IG_MAX_ATTEMPTS`):

```python
TIKTOK_LEAD_MINUTES = 10
```

Replace `_platform_due_time` (currently `reminder_scheduler.py:75-77`):

```python
def _platform_due_time(job: Job, platform: str) -> datetime:
    """Due time for a platform. TikTok fires TIKTOK_LEAD_MINUTES early so its
    ~10-min processing finishes around the user-facing slot; the stored time is
    never mutated, so the head-start stays invisible to the backend and UI."""
    due_time = job.platform_scheduled_at.get(platform) or job.slot_time
    due = _normalize_utc(due_time)
    if platform == "tiktok":
        due -= timedelta(minutes=TIKTOK_LEAD_MINUTES)
    return due
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server && uv run pytest tests/test_reminder_scheduler.py -v`
Expected: PASS (new + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add server/app/services/reminder_scheduler.py server/tests/test_reminder_scheduler.py
git commit -m "feat(server): publish TikTok 10 minutes before its user-facing slot"
```

---

### Task 3: Part B backend — edit-lock guard + `timing_locked` events flag

**Files:**
- Modify: `backend/app/services/scheduling_service.py`, `backend/app/api/routes/scheduling.py`
- Test: `backend/tests/test_scheduling_service.py`, `backend/tests/test_scheduling_routes.py`

**Interfaces:**
- Produces: `SchedulingService.tiktok_timing_locked(project, *, now: datetime | None = None) -> bool`; `reschedule_platform`/`reschedule_anchor` raise `ValueError("timing_locked")` when locked; `PlanningEvent.timing_locked: bool`.

- [ ] **Step 1: Write the failing unit tests (service)**

Append to `backend/tests/test_scheduling_service.py` (reuse the file's `_save_scheduled_project(pid, account_id, platform, slot_dt, ...)` helper and `_setup_single_account`):

```python
def test_tiktok_timing_locked_inside_window(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    tiktok_at = now + timedelta(minutes=5)  # lock window opened at now-5min
    project = _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is True


def test_tiktok_timing_not_locked_outside_window(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    tiktok_at = now + timedelta(minutes=15)  # lock opens at now+5min
    project = _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is False


def test_project_without_tiktok_never_timing_locked(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    project = _save_scheduled_project("p1", acc, "youtube", now)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is False


def test_reschedule_platform_rejects_when_timing_locked(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    tiktok_at = now + timedelta(minutes=3)  # inside the 10-min window
    _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    with pytest.raises(ValueError, match="timing_locked"):
        SchedulingService.reschedule_platform("p1", "tiktok", tiktok_at)


def test_reschedule_anchor_rejects_when_timing_locked(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    tiktok_at = now + timedelta(minutes=3)
    _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    with pytest.raises(ValueError, match="timing_locked"):
        SchedulingService.reschedule_anchor("p1", tiktok_at)
```

(If `pytest` / `timezone` are not already imported at the top of the file, add them. `_setup_single_account` and `_save_scheduled_project` already exist in this file.)

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run -e dev test -- tests/test_scheduling_service.py -k "timing_locked or timing_not_locked or without_tiktok" -v`
Expected: FAIL (`AttributeError: ... 'tiktok_timing_locked'` and the reschedule guards don't raise).

- [ ] **Step 3: Implement the service**

In `backend/app/services/scheduling_service.py`, add the constant near the top of the class (with the other class-level config constants) and the helper (place it next to `_normalize_utc_datetime`):

```python
    TIKTOK_EDIT_LOCK_MINUTES = 10

    @classmethod
    def tiktok_timing_locked(
        cls, project, *, now: datetime | None = None
    ) -> bool:
        """True once a project's TikTok posting has internally begun — i.e.
        now >= tiktok.scheduled_at - TIKTOK_EDIT_LOCK_MINUTES. Projects without
        a tiktok schedule are never timing-locked."""
        sched = (project.platform_schedules or {}).get("tiktok")
        if sched is None:
            return False
        current = now or datetime.now(timezone.utc)
        lock_at = cls._normalize_utc_datetime(sched.scheduled_at) - timedelta(
            minutes=cls.TIKTOK_EDIT_LOCK_MINUTES
        )
        return current >= lock_at
```

In `reschedule_platform`, right after the `if not account_id: raise ...` check (before freeing the old slot), add:

```python
            if cls.tiktok_timing_locked(project):
                raise ValueError("timing_locked")
```

In `reschedule_anchor`, right after the `if not account_id: raise ...` check (before `project.platform_schedules = {}`), add the same three lines.

- [ ] **Step 4: Write the failing route + events-flag tests**

Append to `backend/tests/test_scheduling_routes.py` (reuse the `client` fixture + `_NOW`/`_FixedDateTime`; `_NOW = 2026-05-07 12:00 UTC`):

```python
def _save_project_with_tiktok(pid, scheduled_at):
    project = Project(
        id=pid, anime_name="Show", scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": PlatformSchedule(slot=scheduled_at, scheduled_at=scheduled_at),
        },
    )
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


def test_patch_platform_locked_returns_423(client):
    # tiktok at _NOW + 5min → lock window opened at _NOW - 5min → locked now
    locked_at = datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc)
    _save_project_with_tiktok("plock", locked_at)
    r = client.patch(
        "/api/scheduling/projects/plock/platforms/tiktok",
        json={"new_slot": datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc).isoformat()},
    )
    assert r.status_code == 423
    assert "timing_locked" in r.text


def test_patch_anchor_locked_returns_423(client):
    locked_at = datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc)
    _save_project_with_tiktok("plock2", locked_at)
    r = client.patch(
        "/api/scheduling/projects/plock2/anchor",
        json={"tiktok_slot": datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc).isoformat()},
    )
    assert r.status_code == 423


def test_events_include_timing_locked_flag(client):
    _save_project_with_tiktok("plocked", datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc))
    _save_project_with_tiktok("pfree", datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc))
    r = client.get("/api/scheduling/events", params={"range_start": _NOW.isoformat()})
    assert r.status_code == 200
    events = {e["project_id"]: e for e in r.json()["events"]}
    assert events["plocked"]["timing_locked"] is True
    assert events["pfree"]["timing_locked"] is False
```

- [ ] **Step 5: Run to verify the route/events tests fail**

Run: `pixi run -e dev test -- tests/test_scheduling_routes.py -k "locked or timing" -v`
Expected: FAIL (`timing_locked` field missing → KeyError/validation; 422 instead of 423).

- [ ] **Step 6: Implement the route + events flag**

In `backend/app/api/routes/scheduling.py`:

Add the field to `PlanningEvent` (after `manual: bool = False`):

```python
    timing_locked: bool = False
```

In `_build_planning_event`, add `timing_locked` to the `PlanningEvent(...)` construction (it already has `project` in scope):

```python
        timing_locked=SchedulingService.tiktok_timing_locked(project),
```

(Confirm `SchedulingService` is imported at the top of the file — it is used elsewhere in the module. If the import is function-local only, add `from ...services.scheduling_service import SchedulingService` at module top.)

In `patch_platform`, change the `except ValueError` block to map the lock to 423:

```python
    except ValueError as exc:
        if str(exc) == "timing_locked":
            raise HTTPException(423, "timing_locked")
        raise HTTPException(422, str(exc))
```

In `patch_anchor`, add the same mapping as the first check inside its `except ValueError`:

```python
    except ValueError as exc:
        msg = str(exc)
        if msg == "timing_locked":
            raise HTTPException(423, "timing_locked")
        if "slot_state_changed" in msg or "pool_busy" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(422, msg)
```

- [ ] **Step 7: Run all Part B backend tests**

Run: `pixi run -e dev test -- tests/test_scheduling_service.py tests/test_scheduling_routes.py -v`
Expected: PASS (the pre-existing `test_list_events_returns_filtered_events` failure is unrelated and may still fail — verify it fails identically on `main` if unsure).

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/app/api/routes/scheduling.py backend/tests/test_scheduling_service.py backend/tests/test_scheduling_routes.py
git commit -m "feat(backend): lock project timing 10min before TikTok posts (423) + timing_locked flag"
```

---

### Task 4: Part B frontend — disable reschedule when `timing_locked`

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/components/planning/EventPopover.tsx`, `frontend/src/components/planning/PlanningModal.tsx`

**Interfaces:**
- Consumes: `PlanningEvent.timing_locked` from Task 3.

- [ ] **Step 1: Add the type field**

In `frontend/src/types/index.ts`, add to the `PlanningEvent` interface (after `manual: boolean;`):

```ts
  timing_locked: boolean;
```

- [ ] **Step 2: Disable the per-platform "Déplacer" button when timing-locked**

In `frontend/src/components/planning/EventPopover.tsx`, inside the `ordered.map((m) => { ... })` block, extend the lock. Replace:

```tsx
            const locked = m.status !== "scheduled";
            const lockedReason =
              m.status === "running"
                ? "Upload en cours — action impossible"
                : "Déjà publié — action impossible";
```

with:

```tsx
            const statusLocked = m.status !== "scheduled";
            const timingLocked = m.timing_locked;
            // Timing lock forbids MOVING a slot, not cancelling it.
            const rescheduleLocked = statusLocked || timingLocked;
            const cancelLocked = statusLocked;
            const rescheduleLockedReason = statusLocked
              ? (m.status === "running"
                  ? "Upload en cours — action impossible"
                  : "Déjà publié — action impossible")
              : "La publication TikTok a commencé — horaire verrouillé";
            const cancelLockedReason =
              m.status === "running"
                ? "Upload en cours — action impossible"
                : "Déjà publié — action impossible";
```

Then update the two buttons in that block: the reschedule ("Déplacer") button uses `disabled={rescheduleLocked}` and `title={rescheduleLocked ? rescheduleLockedReason : \`Déplacer le créneau ${PLATFORM_LABELS[m.platform]}\`}`; the cancel ("Annuler") button uses `disabled={cancelLocked}` and `title={cancelLocked ? cancelLockedReason : \`Annuler le créneau ${PLATFORM_LABELS[m.platform]}\`}`. (These replace the current `disabled={locked}` / `title={locked ? lockedReason : ...}` on each button respectively.)

- [ ] **Step 3: Disable the whole-project "Replanifier projet" button when timing-locked**

Still in `EventPopover.tsx`, compute a project-level flag near `const first = members[0];`:

```tsx
  const projectTimingLocked = first.timing_locked;
```

Then change the "Replanifier projet" button to combine it with the incoming prop:

```tsx
            disabled={rescheduleProjectDisabled || projectTimingLocked}
            title={
              projectTimingLocked
                ? "La publication TikTok a commencé — horaire verrouillé"
                : rescheduleProjectDisabledReason ??
                  "Replanifier le projet entier (toutes plateformes)"
            }
```

- [ ] **Step 4: Verify types/build**

Run: `cd frontend && npx tsc --noEmit`
Expected: no type errors. (If the project defines a typecheck/lint script, e.g. `npm run typecheck` or `npm run build`, run that instead/as well.)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/components/planning/EventPopover.tsx frontend/src/components/planning/PlanningModal.tsx
git commit -m "feat(frontend): disable reschedule controls once TikTok timing is locked"
```

Note: `PlanningModal.tsx` is listed because `timing_locked` flows through the `members` it already passes to `EventPopover`; no logic change is required there unless `npx tsc --noEmit` flags a construction of `PlanningEvent` objects that now needs the field. If it does, add `timing_locked` there.

---

### Task 5: Full verification

- [ ] **Step 1: Server suite**

Run: `cd server && uv run pytest`
Expected: PASS.

- [ ] **Step 2: Backend suite**

Run: `pixi run -e dev test`
Expected: PASS except the known-unrelated `test_scheduling_routes.py::test_list_events_returns_filtered_events`.

- [ ] **Step 3: Frontend typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Confirm tree is clean**

Run: `git status --short`
Expected: clean.
