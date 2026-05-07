# Planning System — Phase 1: Backend Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete backend that powers the Planning view, manual slot picker, and urgent cascade upload modes — including platform-side notifications when a slot moves.

**Architecture:** Extend the existing `SchedulingService` with new methods (find_free_slots_after, resolve_anchor, reserve_anchor, reschedule_platform/anchor, cancel_*, compute_cascade, apply_cascade) sharing the existing `_reservation_lock`. Add a `PlatformRescheduleService` that wraps YT/FB API calls and an HTTP client to `/server/` for Instagram. Add a `RescheduleRetryService` async loop for failed notifications. Expose everything through a new `/api/scheduling` router. Add two endpoints to the `/server/` internal API.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, googleapiclient (existing), httpx (existing), pytest. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-05-07-planning-system-design.md](../specs/2026-05-07-planning-system-design.md)

---

## File Structure

**New files:**
- `backend/app/services/platform_reschedule_service.py` — YT/FB/IG-server reschedule + cancel
- `backend/app/services/reschedule_retry_service.py` — async retry loop
- `backend/app/api/routes/scheduling.py` — new REST router
- `backend/tests/test_platform_reschedule_service.py`
- `backend/tests/test_reschedule_retry_service.py`
- `backend/tests/test_scheduling_v2_service.py` — new tests for the v2 methods (keep `test_scheduling_service.py` for legacy)
- `backend/tests/test_scheduling_routes.py` — new routes
- `server/tests/test_internal_jobs_slot.py`

**Modified files:**
- `backend/app/models/project.py` — add `reschedule_pending` field
- `backend/app/services/scheduling_service.py` — extend with v2 methods
- `backend/app/services/__init__.py` — export new services
- `backend/app/api/routes/__init__.py` — register `scheduling_router`
- `backend/app/main.py` — start retry service in lifespan
- `backend/app/config.py` — add `ATR_SCHEDULING_V2_ENABLED` flag
- `server/app/api/internal.py` — add `PATCH /jobs/{project_id}/slot` and `DELETE /jobs/{project_id}`

---

## Conventions

- All datetimes are UTC at the persistence/API boundary; timezone normalization uses the existing `SchedulingService._normalize_utc_datetime`.
- All mutations to `platform_schedules` happen under `SchedulingService._reservation_lock`.
- Tests use the same fixture pattern as [backend/tests/test_scheduling_service.py:79-83](backend/tests/test_scheduling_service.py#L79-L83) — a `_FixedDateTime` subclass + `monkeypatch.setattr("app.services.scheduling_service.datetime", _FixedDateTime)`.
- Run a single test: `pixi run -- pytest backend/tests/test_X.py::test_name -v`
- Run all backend tests: `pixi run test`

---

## Task 1: Add `reschedule_pending` field to Project model

**Files:**
- Modify: `backend/app/models/project.py`
- Test: `backend/tests/test_project_model_reschedule_pending.py` (new)

- [ ] **Step 1: Write the failing test**

`backend/tests/test_project_model_reschedule_pending.py`:

```python
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project


def test_reschedule_pending_defaults_to_empty_dict():
    project = Project(id="p1")
    assert project.reschedule_pending == {}


def test_reschedule_pending_round_trips_through_json():
    payload = {
        "youtube": {
            "target_scheduled_at": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            "retries": 2,
            "last_error": "503 Service Unavailable",
            "last_attempt_at": datetime(2026, 5, 7, 14, 5, tzinfo=timezone.utc),
        }
    }
    project = Project(id="p1", reschedule_pending=payload)
    dumped = project.model_dump(mode="json")
    assert "reschedule_pending" in dumped
    restored = Project.model_validate(dumped)
    assert restored.reschedule_pending["youtube"]["retries"] == 2
    assert restored.reschedule_pending["youtube"]["last_error"] == "503 Service Unavailable"


def test_legacy_project_json_without_field_loads():
    project = Project.model_validate({"id": "p1"})
    assert project.reschedule_pending == {}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pixi run -- pytest backend/tests/test_project_model_reschedule_pending.py -v
```

Expected: FAIL with `AttributeError: 'Project' object has no attribute 'reschedule_pending'` or pydantic validation error.

- [ ] **Step 3: Add the field to Project**

Edit [backend/app/models/project.py:76](backend/app/models/project.py#L76) — insert after `platform_schedules`:

```python
    platform_schedules: dict[str, PlatformSchedule] = Field(default_factory=dict)

    # Per-platform pending platform-side notifications. Set when a reschedule
    # could not be propagated to YT/FB/IG and is awaiting retry.
    # key = platform; value = {target_scheduled_at, retries, last_error, last_attempt_at}.
    reschedule_pending: dict[str, dict[str, Any]] = Field(default_factory=dict)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pixi run -- pytest backend/tests/test_project_model_reschedule_pending.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/project.py backend/tests/test_project_model_reschedule_pending.py
git commit -m "feat(model): add reschedule_pending to Project for platform-notification retries"
```

---

## Task 2: Add `find_free_slots_after` to SchedulingService

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_v2_service.py` (new, will grow across tasks)

- [ ] **Step 1: Create the test file shell**

`backend/tests/test_scheduling_v2_service.py`:

```python
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.models import Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


_NOW = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


@pytest.fixture
def isolated_scheduler(tmp_path: Path, monkeypatch):
    """Reset accounts cache + projects dir + freeze time."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        """\
accounts:
  acc_a:
    name: "Account A"
    language: "fr"
    device: "poco"
    slots: ["12:00", "14:00", "18:00"]
    youtube:
      refresh_token: "tok"
      channel_id: "ch_a"
    tiktok:
      slots: ["12:00", "14:00", "18:00", "21:00"]
  acc_b:
    name: "Account B"
    language: "fr"
    device: "poco"
    slots: ["14:00", "18:00"]
    youtube:
      refresh_token: "tok"
      channel_id: "ch_a"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    monkeypatch.setattr(
        "app.services.scheduling_service.datetime", _FixedDateTime
    )
    AccountService.invalidate()
    yield
    AccountService.invalidate()


def test_find_free_slots_after_returns_chronological_chips(isolated_scheduler):
    slots = SchedulingService.find_free_slots_after(
        account_id="acc_a",
        platform="tiktok",
        after=_NOW,
        limit=5,
    )
    assert len(slots) == 5
    assert all(s.available for s in slots)
    assert [s.slot.hour for s in slots[:4]] == [14, 18, 21, 12]


def test_find_free_slots_after_marks_taken_slots(isolated_scheduler):
    project = Project(id="p1", scheduled_account_id="acc_a")
    project.platform_schedules = {
        "tiktok": __import__("app").models.PlatformSchedule(
            slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            scheduled_at=datetime(2026, 5, 7, 14, 11, tzinfo=timezone.utc),
        )
    }
    ProjectService.save(project)

    slots = SchedulingService.find_free_slots_after(
        account_id="acc_a",
        platform="tiktok",
        after=_NOW,
        limit=5,
    )
    taken = [s for s in slots if not s.available]
    assert len(taken) == 1
    assert taken[0].slot == datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    assert taken[0].taken_by_project_id == "p1"
```

- [ ] **Step 2: Run the test — verify it fails**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: FAIL — `AttributeError: type object 'SchedulingService' has no attribute 'find_free_slots_after'`.

- [ ] **Step 3: Implement `find_free_slots_after` and the `FreeSlot` dataclass**

Edit `backend/app/services/scheduling_service.py`. Add at the top, after the existing imports:

```python
from dataclasses import dataclass


@dataclass
class FreeSlot:
    slot: datetime
    available: bool
    taken_by_project_id: str | None = None
```

Then inside `SchedulingService`, after `find_next_slot_for_platform`, add:

```python
    @classmethod
    def _collect_pool_reservations(
        cls, pool_key: str, platform: str
    ) -> dict[str, str]:
        """Return {slot_iso: project_id} for the given pool/platform."""
        account_pool_keys: dict[str, str] = {}
        for acc_id, acc in AccountService.all_accounts().items():
            account_pool_keys[acc_id] = (
                acc.pool_key_for(platform) or f"account:{acc_id}:{platform}"
            )

        reservations: dict[str, str] = {}
        for project in ProjectService.list_all():
            schedules = project.platform_schedules or {}
            sched = schedules.get(platform)
            if sched is None:
                continue
            owner_id = project.scheduled_account_id
            if not owner_id:
                continue
            if account_pool_keys.get(owner_id) == pool_key:
                slot_iso = cls._normalize_utc_datetime(sched.slot).isoformat()
                reservations[slot_iso] = project.id
        return reservations

    @classmethod
    def find_free_slots_after(
        cls,
        account_id: str,
        platform: str,
        after: datetime,
        limit: int,
    ) -> list[FreeSlot]:
        """Return up to `limit` slots ≥ `after` for (account, platform).

        Each FreeSlot tells whether the slot is currently free in the pool;
        if not, includes the project_id occupying it.
        """
        if limit <= 0:
            return []

        account = AccountService.get_account(account_id)
        if not account:
            raise ValueError(f"Account {account_id} not found")

        slot_strings = account.slots_for(platform)
        if not slot_strings:
            return []

        slot_times: list[tuple[int, int]] = []
        for slot_str in slot_strings:
            parts = slot_str.strip().split(":")
            slot_times.append((int(parts[0]), int(parts[1]) if len(parts) > 1 else 0))
        slot_times.sort()

        pool_key = cls._resolve_pool_key(account_id, platform)
        reservations = cls._collect_pool_reservations(pool_key, platform)

        after_utc = cls._normalize_utc_datetime(after)
        results: list[FreeSlot] = []

        current_date = after_utc.date()
        for _ in range(cls._MAX_LOOKAHEAD_DAYS + 1):
            for hour, minute in slot_times:
                slot_dt = datetime(
                    current_date.year, current_date.month, current_date.day,
                    hour, minute, 0,
                    tzinfo=timezone.utc,
                )
                if slot_dt < after_utc:
                    continue
                slot_iso = slot_dt.isoformat()
                taker = reservations.get(slot_iso)
                results.append(
                    FreeSlot(
                        slot=slot_dt,
                        available=taker is None,
                        taken_by_project_id=taker,
                    )
                )
                if len(results) >= limit:
                    return results
            current_date += timedelta(days=1)
        return results
```

Refactor `_collect_reserved_slots_for_pool` to delegate (one-line change):

```python
    @classmethod
    def _collect_reserved_slots_for_pool(cls, pool_key: str, platform: str) -> set[str]:
        return set(cls._collect_pool_reservations(pool_key, platform).keys())
```

- [ ] **Step 4: Run the tests — verify they pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
pixi run -- pytest backend/tests/test_scheduling_service.py -v   # legacy still green
```

Expected: 2 passed in v2 file, all legacy tests still green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_v2_service.py
git commit -m "feat(scheduling): add find_free_slots_after with pool-aware availability"
```

---

## Task 3: Implement `resolve_anchor`

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_v2_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_scheduling_v2_service.py`:

```python
def test_resolve_anchor_resolves_each_platform_to_first_free_slot(isolated_scheduler):
    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides=None,
    )
    yt = result.resolved["youtube"]
    assert yt.slot == datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    assert yt.available is True
    assert result.conflicts == []


def test_resolve_anchor_falls_back_to_next_slot_when_taken(isolated_scheduler):
    other = Project(id="other", scheduled_account_id="acc_a")
    other.platform_schedules = {
        "youtube": __import__("app").models.PlatformSchedule(
            slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            scheduled_at=datetime(2026, 5, 7, 14, 7, tzinfo=timezone.utc),
        )
    }
    ProjectService.save(other)

    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides=None,
    )
    yt = result.resolved["youtube"]
    assert yt.slot == datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    assert yt.available is True


def test_resolve_anchor_uses_overrides(isolated_scheduler):
    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides={"youtube": datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)},
    )
    yt = result.resolved["youtube"]
    assert yt.slot == datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)


def test_resolve_anchor_invalid_override_returns_conflict(isolated_scheduler):
    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides={"youtube": datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc)},
    )
    assert any(c.platform == "youtube" for c in result.conflicts)
```

- [ ] **Step 2: Run the tests — they fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 4 new failures, no `resolve_anchor` attribute.

- [ ] **Step 3: Implement `resolve_anchor`**

Add to `backend/app/services/scheduling_service.py`, after `find_free_slots_after`:

```python
@dataclass
class ResolvedSlot:
    slot: datetime
    scheduled_at: datetime
    available: bool


@dataclass
class AnchorConflict:
    platform: str
    reason: str


@dataclass
class ResolveAnchorResult:
    resolved: dict[str, ResolvedSlot]
    conflicts: list[AnchorConflict]
```

Inside `SchedulingService`:

```python
    _OTHER_PLATFORMS_FOR_ANCHOR: tuple[str, ...] = ("youtube", "facebook", "instagram")

    @classmethod
    def _is_slot_in_account_config(
        cls, account_id: str, platform: str, slot: datetime
    ) -> bool:
        account = AccountService.get_account(account_id)
        if not account:
            return False
        slot_strings = account.slots_for(platform)
        wanted = (slot.hour, slot.minute)
        for slot_str in slot_strings:
            parts = slot_str.strip().split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            if (hour, minute) == wanted:
                return True
        return False

    @classmethod
    def resolve_anchor(
        cls,
        account_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
    ) -> "ResolveAnchorResult":
        """Resolve which slot will be reserved per platform, given a TT anchor.

        Pure read-only — does not write anything.
        """
        anchor = cls._normalize_utc_datetime(tiktok_slot)
        overrides = overrides or {}
        now_utc = datetime.now(timezone.utc)

        resolved: dict[str, ResolvedSlot] = {}
        conflicts: list[AnchorConflict] = []

        # TikTok itself is the anchor: must match a configured slot, must be
        # free in TT pool.
        if not cls._is_slot_in_account_config(account_id, "tiktok", anchor):
            conflicts.append(AnchorConflict("tiktok", "slot_not_configured"))
        else:
            tt_pool_key = cls._resolve_pool_key(account_id, "tiktok")
            tt_taken = cls._collect_reserved_slots_for_pool(tt_pool_key, "tiktok")
            if anchor.isoformat() in tt_taken:
                conflicts.append(AnchorConflict("tiktok", "slot_taken"))
            else:
                resolved["tiktok"] = ResolvedSlot(
                    slot=anchor,
                    scheduled_at=cls._randomize_slot(anchor, now_utc),
                    available=True,
                )

        # Other platforms: take override if provided, else first free slot ≥ anchor.
        account = AccountService.get_account(account_id)
        for platform in cls._OTHER_PLATFORMS_FOR_ANCHOR:
            if account is None or not account.slots_for(platform):
                continue
            override = overrides.get(platform)
            if override is not None:
                slot = cls._normalize_utc_datetime(override)
                if not cls._is_slot_in_account_config(account_id, platform, slot):
                    conflicts.append(AnchorConflict(platform, "slot_not_configured"))
                    continue
                pool_key = cls._resolve_pool_key(account_id, platform)
                taken = cls._collect_reserved_slots_for_pool(pool_key, platform)
                if slot.isoformat() in taken:
                    conflicts.append(AnchorConflict(platform, "slot_taken"))
                    continue
                resolved[platform] = ResolvedSlot(
                    slot=slot,
                    scheduled_at=cls._randomize_slot(slot, now_utc),
                    available=True,
                )
                continue

            free = cls.find_free_slots_after(
                account_id=account_id,
                platform=platform,
                after=anchor,
                limit=1,
            )
            free_avail = next((s for s in free if s.available), None)
            if free_avail is None:
                conflicts.append(AnchorConflict(platform, "pool_full"))
                continue
            resolved[platform] = ResolvedSlot(
                slot=free_avail.slot,
                scheduled_at=cls._randomize_slot(free_avail.slot, now_utc),
                available=True,
            )

        return ResolveAnchorResult(resolved=resolved, conflicts=conflicts)
```

- [ ] **Step 4: Run the tests — they pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_v2_service.py
git commit -m "feat(scheduling): add resolve_anchor for TT-anchored multi-platform planning"
```

---

## Task 4: Implement `reserve_anchor` and `reschedule_anchor`

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_v2_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_reserve_anchor_persists_platform_schedules(isolated_scheduler):
    ProjectService.save(Project(id="proj"))
    result = SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    assert "tiktok" in result
    assert "youtube" in result
    reloaded = ProjectService.load("proj")
    assert reloaded.scheduled_account_id == "acc_a"
    assert "tiktok" in reloaded.platform_schedules


def test_reserve_anchor_idempotent_when_called_twice(isolated_scheduler):
    ProjectService.save(Project(id="proj"))
    first = SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    second = SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    assert first["tiktok"].slot == second["tiktok"].slot
    assert first["tiktok"].scheduled_at == second["tiktok"].scheduled_at


def test_reserve_anchor_raises_on_conflict(isolated_scheduler):
    ProjectService.save(Project(id="other", scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 8, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="proj"))
    with pytest.raises(ValueError) as exc:
        SchedulingService.reserve_anchor(
            project_id="proj",
            account_id="acc_a",
            tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        )
    assert "tiktok" in str(exc.value)


def test_reschedule_anchor_swaps_existing_reservations(isolated_scheduler):
    ProjectService.save(Project(id="proj"))
    SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    new_anchor = datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)
    SchedulingService.reschedule_anchor(
        project_id="proj",
        tiktok_slot=new_anchor,
    )
    reloaded = ProjectService.load("proj")
    assert reloaded.platform_schedules["tiktok"].slot == new_anchor
```

- [ ] **Step 2: Run the tests — they fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py::test_reserve_anchor_persists_platform_schedules -v
```

Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement `reserve_anchor` and `reschedule_anchor`**

Add inside `SchedulingService`:

```python
    @classmethod
    def reserve_anchor(
        cls,
        project_id: str,
        account_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
    ) -> dict[str, PlatformSchedule]:
        """Reserve TT (anchor) + each other configured platform on `project`.

        Idempotent: a second call with the same anchor reuses the existing
        per-platform reservations.
        Raises ValueError if any platform conflicts.
        """
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")

            # Reuse path: same account, same anchor TT slot already stored.
            existing_tt = (project.platform_schedules or {}).get("tiktok")
            anchor = cls._normalize_utc_datetime(tiktok_slot)
            if (
                existing_tt is not None
                and project.scheduled_account_id == account_id
                and cls._normalize_utc_datetime(existing_tt.slot) == anchor
            ):
                return dict(project.platform_schedules)

            # Drop reservations belonging to a different account before reserving.
            if project.scheduled_account_id and project.scheduled_account_id != account_id:
                project.platform_schedules = {}

            resolution = cls.resolve_anchor(account_id, anchor, overrides)
            if resolution.conflicts:
                conflict_summary = ", ".join(
                    f"{c.platform}:{c.reason}" for c in resolution.conflicts
                )
                raise ValueError(f"Anchor conflicts: {conflict_summary}")

            schedules = dict(project.platform_schedules or {})
            for platform, resolved in resolution.resolved.items():
                schedules[platform] = PlatformSchedule(
                    slot=resolved.slot, scheduled_at=resolved.scheduled_at
                )
            project.platform_schedules = schedules
            project.scheduled_account_id = account_id
            cls._recompute_aggregates(project)
            ProjectService.save(project)
            return dict(schedules)

    @classmethod
    def reschedule_anchor(
        cls,
        project_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
    ) -> dict[str, PlatformSchedule]:
        """Re-anchor a project's reservations on a new TT slot."""
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")
            account_id = project.scheduled_account_id
            if not account_id:
                raise ValueError("Project has no scheduled account")
            # Drop existing per-platform reservations so resolve_anchor sees
            # the slots as free in this pool.
            project.platform_schedules = {}
            ProjectService.save(project)

        return cls.reserve_anchor(
            project_id=project_id,
            account_id=account_id,
            tiktok_slot=tiktok_slot,
            overrides=overrides,
        )
```

- [ ] **Step 4: Run the tests — they pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 10 passed total.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_v2_service.py
git commit -m "feat(scheduling): add reserve_anchor and reschedule_anchor"
```

---

## Task 5: Implement `reschedule_platform`, `cancel_platform_slot`, `cancel_all_slots`

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_v2_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_reschedule_platform_replaces_single_platform_slot(isolated_scheduler):
    ProjectService.save(Project(id="proj"))
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    new_yt = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    sched = SchedulingService.reschedule_platform("proj", "youtube", new_yt)
    assert sched.slot == new_yt

    reloaded = ProjectService.load("proj")
    assert reloaded.platform_schedules["youtube"].slot == new_yt
    # tiktok unchanged
    assert reloaded.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 14, 0, tzinfo=timezone.utc
    )


def test_reschedule_platform_rejects_taken_slot(isolated_scheduler):
    ProjectService.save(Project(id="other", scheduled_account_id="acc_a",
        platform_schedules={
            "youtube": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 8, 14, 5, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="proj"))
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError):
        SchedulingService.reschedule_platform(
            "proj", "youtube", datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
        )


def test_cancel_platform_slot_removes_only_one_platform(isolated_scheduler):
    ProjectService.save(Project(id="proj"))
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    SchedulingService.cancel_platform_slot("proj", "youtube")
    reloaded = ProjectService.load("proj")
    assert "youtube" not in reloaded.platform_schedules
    assert "tiktok" in reloaded.platform_schedules


def test_cancel_all_slots_clears_everything(isolated_scheduler):
    ProjectService.save(Project(id="proj"))
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    SchedulingService.cancel_all_slots("proj")
    reloaded = ProjectService.load("proj")
    assert reloaded.platform_schedules == {}
    assert reloaded.scheduled_account_id is None
```

- [ ] **Step 2: Run — they fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 4 new FAILs.

- [ ] **Step 3: Implement the three methods**

Add inside `SchedulingService`:

```python
    @classmethod
    def reschedule_platform(
        cls, project_id: str, platform: str, new_slot: datetime
    ) -> PlatformSchedule:
        """Replace a single platform's slot. Validates against the pool."""
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")
            account_id = project.scheduled_account_id
            if not account_id:
                raise ValueError("Project has no scheduled account")
            slot = cls._normalize_utc_datetime(new_slot)
            if not cls._is_slot_in_account_config(account_id, platform, slot):
                raise ValueError(f"Slot {slot.isoformat()} not configured for {platform}")

            # Free old slot before checking pool: must NOT see ourselves as taking it.
            schedules = dict(project.platform_schedules or {})
            old = schedules.pop(platform, None)
            project.platform_schedules = schedules
            ProjectService.save(project)

            try:
                pool_key = cls._resolve_pool_key(account_id, platform)
                taken = cls._collect_reserved_slots_for_pool(pool_key, platform)
                if slot.isoformat() in taken:
                    raise ValueError(f"Slot {slot.isoformat()} already taken in {platform} pool")

                now_utc = datetime.now(timezone.utc)
                if slot < now_utc + timedelta(minutes=cls._MIN_LEAD_MINUTES):
                    raise ValueError("slot_too_close")

                new_sched = PlatformSchedule(
                    slot=slot,
                    scheduled_at=cls._randomize_slot(slot, now_utc),
                )
                schedules[platform] = new_sched
                project.platform_schedules = schedules
                cls._recompute_aggregates(project)
                ProjectService.save(project)
                return new_sched
            except Exception:
                # Restore the old reservation on failure.
                if old is not None:
                    schedules[platform] = old
                    project.platform_schedules = schedules
                    ProjectService.save(project)
                raise

    @classmethod
    def cancel_platform_slot(cls, project_id: str, platform: str) -> None:
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                return
            schedules = dict(project.platform_schedules or {})
            if platform in schedules:
                del schedules[platform]
                project.platform_schedules = schedules
                cls._recompute_aggregates(project)
                if not schedules:
                    project.scheduled_account_id = None
                ProjectService.save(project)

    @classmethod
    def cancel_all_slots(cls, project_id: str) -> None:
        cls.clear_reserved_slots(project_id)
```

- [ ] **Step 4: Run — they pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 14 passed total.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_v2_service.py
git commit -m "feat(scheduling): add reschedule_platform, cancel_platform_slot, cancel_all_slots"
```

---

## Task 6: Implement `compute_cascade`

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_v2_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_compute_cascade_simple_one_displaced(isolated_scheduler):
    ProjectService.save(Project(id="other", scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 5, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="urgent", anime_name="Urgent"))

    result = SchedulingService.compute_cascade("urgent", "acc_a")
    tt = next(p for p in result.per_platform if p.platform == "tiktok")
    assert tt.target_slot == datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    assert len(tt.displaced) == 1
    assert tt.displaced[0].project_id == "other"
    assert tt.displaced[0].from_slot == datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    assert tt.displaced[0].to_slot == datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)


def test_compute_cascade_chain_three_displaced(isolated_scheduler):
    for pid, hour in [("a", 14), ("b", 18), ("c", 21)]:
        ProjectService.save(Project(id=pid, scheduled_account_id="acc_a",
            platform_schedules={
                "tiktok": __import__("app").models.PlatformSchedule(
                    slot=datetime(2026, 5, 7, hour, 0, tzinfo=timezone.utc),
                    scheduled_at=datetime(2026, 5, 7, hour, 5, tzinfo=timezone.utc),
                )
            }
        ))
    ProjectService.save(Project(id="urgent"))

    result = SchedulingService.compute_cascade("urgent", "acc_a")
    tt = next(p for p in result.per_platform if p.platform == "tiktok")
    assert len(tt.displaced) == 3
    # cascade order: a -> 18, b -> 21, c -> next day 12
    assert tt.displaced[0].project_id == "a"
    assert tt.displaced[0].to_slot == datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    assert tt.displaced[1].project_id == "b"
    assert tt.displaced[1].to_slot == datetime(2026, 5, 7, 21, 0, tzinfo=timezone.utc)
    assert tt.displaced[2].project_id == "c"
    assert tt.displaced[2].to_slot == datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)


def test_compute_cascade_blocks_when_pool_busy(isolated_scheduler, monkeypatch):
    """A project in the pool with an active upload job blocks cascade."""
    ProjectService.save(Project(id="active", scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 3, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="urgent"))

    # Monkey-patch the queue check to simulate a running job for "active".
    from app.services import project_upload_service as pus
    class FakeJob:
        status = "running"
        project_id = "active"
    monkeypatch.setattr(
        pus.project_upload_queue, "list_jobs", lambda: [FakeJob()]
    )

    result = SchedulingService.compute_cascade("urgent", "acc_a")
    assert any(b.platform == "tiktok" and b.reason == "pool_busy" for b in result.blockers)
```

- [ ] **Step 2: Run — they fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 3 new failures.

- [ ] **Step 3: Implement `compute_cascade`**

Add the dataclasses near the top of `scheduling_service.py` (after the others):

```python
@dataclass
class DisplacedItem:
    project_id: str
    anime_title: str
    from_slot: datetime
    to_slot: datetime
    requires_platform_notification: bool


@dataclass
class CascadePlatform:
    platform: str
    target_slot: datetime
    target_scheduled_at: datetime
    displaced: list[DisplacedItem]


@dataclass
class CascadeBlocker:
    platform: str
    reason: str


@dataclass
class CascadeResult:
    per_platform: list[CascadePlatform]
    blockers: list[CascadeBlocker]
```

Inside `SchedulingService`:

```python
    _CASCADE_PLATFORMS: tuple[str, ...] = ("tiktok", "youtube", "facebook", "instagram")

    @classmethod
    def _platforms_for_project(cls, project_id: str, account_id: str) -> list[str]:
        """Return the cascade-relevant platforms for an urgent project upload."""
        from .project_upload_service import _platforms_to_reserve  # noqa: PLC0415
        account = AccountService.get_account(account_id)
        if account is None:
            return []
        return _platforms_to_reserve(account, requested_platforms=None)

    @classmethod
    def _slot_times_sorted(
        cls, account_id: str, platform: str
    ) -> list[tuple[int, int]]:
        account = AccountService.get_account(account_id)
        if not account:
            return []
        out: list[tuple[int, int]] = []
        for s in account.slots_for(platform):
            parts = s.strip().split(":")
            out.append((int(parts[0]), int(parts[1]) if len(parts) > 1 else 0))
        return sorted(out)

    @classmethod
    def _next_slot_after(
        cls,
        account_id: str,
        platform: str,
        after_slot: datetime,
    ) -> datetime | None:
        slot_times = cls._slot_times_sorted(account_id, platform)
        if not slot_times:
            return None
        cap = datetime.now(timezone.utc) + timedelta(days=cls._MAX_LOOKAHEAD_DAYS)
        current_date = after_slot.date()
        seen_after = False
        for _ in range(cls._MAX_LOOKAHEAD_DAYS + 1):
            for hour, minute in slot_times:
                candidate = datetime(
                    current_date.year, current_date.month, current_date.day,
                    hour, minute, 0, tzinfo=timezone.utc,
                )
                if candidate <= after_slot:
                    continue
                if candidate > cap:
                    return None
                return candidate
            current_date += timedelta(days=1)
            del seen_after
        return None

    @classmethod
    def _earliest_slot_at_or_after(
        cls, account_id: str, platform: str, lower_bound: datetime
    ) -> datetime | None:
        slot_times = cls._slot_times_sorted(account_id, platform)
        if not slot_times:
            return None
        cap = lower_bound + timedelta(days=cls._MAX_LOOKAHEAD_DAYS)
        current_date = lower_bound.date()
        for _ in range(cls._MAX_LOOKAHEAD_DAYS + 1):
            for hour, minute in slot_times:
                candidate = datetime(
                    current_date.year, current_date.month, current_date.day,
                    hour, minute, 0, tzinfo=timezone.utc,
                )
                if candidate < lower_bound:
                    continue
                if candidate > cap:
                    return None
                return candidate
            current_date += timedelta(days=1)
        return None

    @classmethod
    def _project_requires_platform_notification(
        cls, project: Project, platform: str
    ) -> bool:
        result = project.upload_last_result or {}
        platforms = result.get("platforms") if isinstance(result, dict) else None
        if not isinstance(platforms, dict):
            return False
        entry = platforms.get(platform)
        if not isinstance(entry, dict):
            return False
        return bool(entry.get("url"))

    @classmethod
    def _pool_is_busy_uploading(cls, account_id: str, platform: str) -> tuple[bool, str | None]:
        from .project_upload_service import project_upload_queue  # noqa: PLC0415
        pool_key = cls._resolve_pool_key(account_id, platform)
        account_pool_keys: dict[str, str] = {}
        for acc_id, acc in AccountService.all_accounts().items():
            account_pool_keys[acc_id] = (
                acc.pool_key_for(platform) or f"account:{acc_id}:{platform}"
            )
        in_pool_pids: set[str] = set()
        for project in ProjectService.list_all():
            if (project.scheduled_account_id
                and account_pool_keys.get(project.scheduled_account_id) == pool_key):
                in_pool_pids.add(project.id)

        for job in project_upload_queue.list_jobs():
            if job.project_id in in_pool_pids and job.status in ("running", "queued"):
                return True, job.project_id
        return False, None

    @classmethod
    def compute_cascade(cls, project_id: str, account_id: str) -> CascadeResult:
        """Pure simulation: which projects move where if `project_id` jumps in.

        Anchor per platform = first configured slot ≥ now+30min.
        Cascade rule: occupant of anchor slot moves to next configured slot;
        if that's also taken, occupant moves further; repeat until empty slot
        or lookahead window exhausted.
        """
        platforms = cls._platforms_for_project(project_id, account_id)
        per_platform: list[CascadePlatform] = []
        blockers: list[CascadeBlocker] = []
        now_utc = datetime.now(timezone.utc)
        earliest_allowed = now_utc + timedelta(minutes=cls._MIN_LEAD_MINUTES)
        fb_horizon = now_utc + timedelta(days=29)

        for platform in platforms:
            busy, _busy_pid = cls._pool_is_busy_uploading(account_id, platform)
            if busy:
                blockers.append(CascadeBlocker(platform, "pool_busy"))
                continue

            anchor = cls._earliest_slot_at_or_after(account_id, platform, earliest_allowed)
            if anchor is None:
                blockers.append(CascadeBlocker(platform, "pool_full"))
                continue

            pool_key = cls._resolve_pool_key(account_id, platform)
            reservations = cls._collect_pool_reservations(pool_key, platform)

            # Build map slot_iso -> project for efficient cascade walking.
            # We need the actual Project entries to keep titles/upload_last_result.
            account_pool_keys: dict[str, str] = {}
            for acc_id, acc in AccountService.all_accounts().items():
                account_pool_keys[acc_id] = (
                    acc.pool_key_for(platform) or f"account:{acc_id}:{platform}"
                )
            slot_to_project: dict[str, Project] = {}
            for project in ProjectService.list_all():
                if (project.scheduled_account_id
                    and account_pool_keys.get(project.scheduled_account_id) == pool_key):
                    sched = (project.platform_schedules or {}).get(platform)
                    if sched:
                        slot_to_project[
                            cls._normalize_utc_datetime(sched.slot).isoformat()
                        ] = project

            displaced: list[DisplacedItem] = []
            current_slot = anchor
            occupant = slot_to_project.get(current_slot.isoformat())
            blocked_for_platform = False
            while occupant is not None:
                next_slot = cls._next_slot_after(account_id, platform, current_slot)
                if next_slot is None:
                    blockers.append(CascadeBlocker(platform, "pool_full"))
                    blocked_for_platform = True
                    break
                if platform == "facebook" and next_slot > fb_horizon:
                    blockers.append(CascadeBlocker(platform, "facebook_horizon_exceeded"))
                    blocked_for_platform = True
                    break
                displaced.append(DisplacedItem(
                    project_id=occupant.id,
                    anime_title=occupant.anime_name or occupant.id,
                    from_slot=current_slot,
                    to_slot=next_slot,
                    requires_platform_notification=cls._project_requires_platform_notification(
                        occupant, platform
                    ),
                ))
                # Walk to next slot; check if it's also occupied.
                current_slot = next_slot
                occupant = slot_to_project.get(current_slot.isoformat())

            if blocked_for_platform:
                # Don't add a cascade plan for a blocked platform — the apply
                # path uses len(blockers) > 0 to abort entirely.
                continue

            target_slot = anchor
            target_scheduled_at = cls._randomize_slot(target_slot, now_utc)
            per_platform.append(CascadePlatform(
                platform=platform,
                target_slot=target_slot,
                target_scheduled_at=target_scheduled_at,
                displaced=displaced,
            ))

        return CascadeResult(per_platform=per_platform, blockers=blockers)
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_v2_service.py
git commit -m "feat(scheduling): add compute_cascade for urgent upload preview"
```

---

## Task 7: Implement `apply_cascade`

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_v2_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_apply_cascade_persists_displacements_and_reserves_urgent(isolated_scheduler):
    ProjectService.save(Project(id="other", scheduled_account_id="acc_a",
        anime_name="Other Anime",
        platform_schedules={
            "tiktok": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 4, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="urgent", anime_name="Urgent"))

    result = SchedulingService.apply_cascade("urgent", "acc_a")
    assert any(p.platform == "tiktok" for p in result.per_platform)

    other = ProjectService.load("other")
    urgent = ProjectService.load("urgent")
    assert other.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 18, 0, tzinfo=timezone.utc
    )
    assert urgent.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 14, 0, tzinfo=timezone.utc
    )
    assert urgent.scheduled_account_id == "acc_a"


def test_apply_cascade_aborts_with_blockers(isolated_scheduler, monkeypatch):
    from app.services import project_upload_service as pus
    class FakeJob:
        status = "running"
        project_id = "blocker"
    ProjectService.save(Project(id="blocker", scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 5, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="urgent"))
    monkeypatch.setattr(pus.project_upload_queue, "list_jobs", lambda: [FakeJob()])

    with pytest.raises(ValueError):
        SchedulingService.apply_cascade("urgent", "acc_a")

    # Ensure no partial state was persisted.
    blocker = ProjectService.load("blocker")
    assert blocker.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 14, 0, tzinfo=timezone.utc
    )
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py::test_apply_cascade_persists_displacements_and_reserves_urgent -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `apply_cascade`**

Inside `SchedulingService`:

```python
    @classmethod
    def apply_cascade(cls, project_id: str, account_id: str) -> CascadeResult:
        """Compute and persist a cascade. Reserves the urgent project's slots."""
        with cls._reservation_lock:
            result = cls.compute_cascade(project_id, account_id)
            if result.blockers:
                summary = ", ".join(f"{b.platform}:{b.reason}" for b in result.blockers)
                raise ValueError(f"Cascade blocked: {summary}")

            urgent = ProjectService.load(project_id)
            if urgent is None:
                raise ValueError("Urgent project not found")

            now_utc = datetime.now(timezone.utc)

            # 1. Move displaced projects in REVERSE order of their cascade chain.
            #    Walking from the farthest-pushed back to the closest avoids
            #    momentary "two projects on same slot" states inside the lock.
            for plat in result.per_platform:
                for item in reversed(plat.displaced):
                    project = ProjectService.load(item.project_id)
                    if project is None:
                        continue
                    schedules = dict(project.platform_schedules or {})
                    schedules[plat.platform] = PlatformSchedule(
                        slot=item.to_slot,
                        scheduled_at=cls._randomize_slot(item.to_slot, now_utc),
                    )
                    project.platform_schedules = schedules
                    cls._recompute_aggregates(project)
                    ProjectService.save(project)

            # 2. Reserve the urgent project's slots on the freed targets.
            schedules = dict(urgent.platform_schedules or {})
            for plat in result.per_platform:
                schedules[plat.platform] = PlatformSchedule(
                    slot=plat.target_slot,
                    scheduled_at=plat.target_scheduled_at,
                )
            urgent.platform_schedules = schedules
            urgent.scheduled_account_id = account_id
            cls._recompute_aggregates(urgent)
            ProjectService.save(urgent)

            return result
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_v2_service.py -v
```

Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_v2_service.py
git commit -m "feat(scheduling): add apply_cascade with atomic displacement"
```

---

## Task 8: Add the `/server/` PATCH and DELETE endpoints

**Files:**
- Modify: `server/app/api/internal.py`
- Test: `server/tests/test_internal_jobs_slot.py` (new)

- [ ] **Step 1: Write the failing tests**

`server/tests/test_internal_jobs_slot.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


JOB_PAYLOAD = {
    "project_id": "p1",
    "account_id": "anime_fr",
    "slot_time": "2026-05-07T14:00:00+00:00",
    "anime_title": "Test",
    "description": "d",
    "drive_video_url": "https://drive.google.com/uc?id=x",
    "platforms_requested": ["instagram"],
    "instagram": {
        "ig_user_id": "ig",
        "ig_access_token": "tok",
        "caption": "c",
    },
}
INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    app.state.discord = AsyncMock()
    app.state.discord.post_message = AsyncMock(return_value="msg_1")
    return app


def test_patch_job_slot_updates_slot_time(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 200

        new_slot = "2026-05-08T18:00:00+00:00"
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={
                "slot_time": new_slot,
                "platform_scheduled_at": {"instagram": "2026-05-08T18:11:00+00:00"},
            },
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["slot_time"].startswith("2026-05-08T18:00:00")


def test_patch_job_slot_404_for_missing(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.patch(
            "/api/internal/jobs/missing/slot",
            json={"slot_time": "2026-05-08T18:00:00+00:00"},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 404


def test_delete_job_removes_it(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
        assert r.status_code == 204
        # Subsequent PATCH should now 404.
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={"slot_time": "2026-05-09T14:00:00+00:00"},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 404
```

- [ ] **Step 2: Run — fail**

```bash
cd server && pixi run -- pytest tests/test_internal_jobs_slot.py -v
```

Expected: FAIL — endpoints not found (404 from FastAPI).

- [ ] **Step 3: Implement the endpoints**

Edit [server/app/api/internal.py](server/app/api/internal.py). Append before the file ends:

```python
class UpdateSlotRequest(BaseModel):
    slot_time: datetime
    platform_scheduled_at: dict[str, datetime] | None = None


@router.patch("/jobs/{project_id}/slot")
async def update_job_slot(
    project_id: str, req: UpdateSlotRequest, request: Request
) -> dict:
    store = request.app.state.job_store
    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, f"Job for project {project_id!r} not found")

    job.slot_time = req.slot_time
    if req.platform_scheduled_at is not None:
        job.platform_scheduled_at = dict(req.platform_scheduled_at)
    job.updated_at = datetime.now(tz=UTC)
    await store.update(job)
    return {
        "project_id": job.project_id,
        "slot_time": job.slot_time.isoformat(),
        "platform_scheduled_at": {
            p: dt.isoformat() for p, dt in job.platform_scheduled_at.items()
        },
    }


@router.delete("/jobs/{project_id}", status_code=204)
async def delete_job(project_id: str, request: Request) -> None:
    store = request.app.state.job_store
    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, f"Job for project {project_id!r} not found")
    await store.delete(project_id)
```

If the server's `JobStore` doesn't already expose `update` or `delete`, add them. Check first:

```bash
grep -n "async def update\|async def delete" server/app/services/job_store.py
```

If `update` or `delete` is missing, add to `server/app/services/job_store.py`:

```python
    async def update(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.project_id] = job
            await self._persist_locked()

    async def delete(self, project_id: str) -> None:
        async with self._lock:
            self._jobs.pop(project_id, None)
            await self._persist_locked()
```

- [ ] **Step 4: Run — pass**

```bash
cd server && pixi run -- pytest tests/test_internal_jobs_slot.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/api/internal.py server/app/services/job_store.py server/tests/test_internal_jobs_slot.py
git commit -m "feat(server): add PATCH /jobs/{id}/slot and DELETE /jobs/{id} for IG reschedule"
```

---

## Task 9: Create `PlatformRescheduleService` skeleton

**Files:**
- Create: `backend/app/services/platform_reschedule_service.py`
- Test: `backend/tests/test_platform_reschedule_service.py` (new)

- [ ] **Step 1: Write the failing test**

`backend/tests/test_platform_reschedule_service.py`:

```python
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project
from app.services.platform_reschedule_service import (
    NotificationResult,
    PlatformRescheduleService,
)


def test_notify_returns_skipped_for_unsupported_platform():
    project = Project(id="p1")
    result = PlatformRescheduleService.notify(
        project, "tiktok", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    assert result.status == "skipped"


def test_notify_skips_when_video_id_missing():
    project = Project(id="p1")
    result = PlatformRescheduleService.notify(
        project, "youtube", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    assert result.status == "skipped"
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: import error.

- [ ] **Step 3: Create the skeleton**

`backend/app/services/platform_reschedule_service.py`:

```python
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..models import Project

logger = logging.getLogger("uvicorn.error")


NotificationStatus = Literal["ok", "pending_retry", "skipped"]


@dataclass
class NotificationResult:
    status: NotificationStatus
    error: str | None = None


class PlatformRescheduleService:
    """Propagates slot changes to YouTube/Facebook/Instagram-server.

    TikTok is manual and never notified.
    """

    @classmethod
    def _platform_video_url(cls, project: Project, platform: str) -> str | None:
        result = project.upload_last_result or {}
        platforms = result.get("platforms") if isinstance(result, dict) else None
        if not isinstance(platforms, dict):
            return None
        entry = platforms.get(platform)
        if not isinstance(entry, dict):
            return None
        url = entry.get("url")
        return url if isinstance(url, str) else None

    @classmethod
    def _youtube_video_id(cls, url: str) -> str | None:
        # Accepts youtu.be/<id>, youtube.com/watch?v=<id>, youtube.com/shorts/<id>
        patterns = (
            r"youtu\.be/([A-Za-z0-9_\-]{6,})",
            r"[?&]v=([A-Za-z0-9_\-]{6,})",
            r"shorts/([A-Za-z0-9_\-]{6,})",
        )
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None

    @classmethod
    def _facebook_video_id(cls, url: str) -> str | None:
        m = re.search(r"/videos?/(\d+)", url) or re.search(r"v=(\d+)", url)
        return m.group(1) if m else None

    @classmethod
    def notify(
        cls, project: Project, platform: str, new_scheduled_at: datetime
    ) -> NotificationResult:
        if platform == "tiktok":
            return NotificationResult(status="skipped")

        url = cls._platform_video_url(project, platform)
        if not url:
            return NotificationResult(status="skipped")

        try:
            if platform == "youtube":
                return cls._notify_youtube(project, url, new_scheduled_at)
            if platform == "facebook":
                return cls._notify_facebook(project, url, new_scheduled_at)
            if platform == "instagram":
                return cls._notify_instagram(project, new_scheduled_at)
        except Exception as exc:
            logger.warning(
                "platform reschedule failed: project=%s platform=%s error=%s",
                project.id, platform, exc,
            )
            return NotificationResult(status="pending_retry", error=str(exc))
        return NotificationResult(status="skipped")

    @classmethod
    def cancel(cls, project: Project, platform: str) -> NotificationResult:
        if platform == "tiktok":
            return NotificationResult(status="skipped")

        url = cls._platform_video_url(project, platform)
        if not url and platform != "instagram":
            return NotificationResult(status="skipped")

        try:
            if platform == "youtube":
                return cls._cancel_youtube(project, url)
            if platform == "facebook":
                return cls._cancel_facebook(project, url)
            if platform == "instagram":
                return cls._cancel_instagram(project)
        except Exception as exc:
            logger.warning(
                "platform cancel failed: project=%s platform=%s error=%s",
                project.id, platform, exc,
            )
            return NotificationResult(status="pending_retry", error=str(exc))
        return NotificationResult(status="skipped")

    # Implementations live in tasks 10-12.
    @classmethod
    def _notify_youtube(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _notify_facebook(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _notify_instagram(cls, project: Project, new_scheduled_at: datetime) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _cancel_youtube(cls, project: Project, url: str) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _cancel_facebook(cls, project: Project, url: str) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _cancel_instagram(cls, project: Project) -> NotificationResult:
        raise NotImplementedError
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/platform_reschedule_service.py backend/tests/test_platform_reschedule_service.py
git commit -m "feat(reschedule): add PlatformRescheduleService skeleton"
```

---

## Task 10: Implement YouTube notify + cancel

**Files:**
- Modify: `backend/app/services/platform_reschedule_service.py`
- Test: `backend/tests/test_platform_reschedule_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from unittest.mock import patch

def test_notify_youtube_calls_videos_update_with_publish_at():
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"youtube": {"url": "https://youtu.be/abc12345"}}},
    )
    fake_youtube = MagicMock()
    update_call = MagicMock()
    fake_youtube.videos.return_value.update.return_value = update_call
    update_call.execute.return_value = {"id": "abc12345"}

    with patch(
        "app.services.platform_reschedule_service.AccountService.get_youtube_credentials"
    ), patch(
        "app.services.platform_reschedule_service.build", return_value=fake_youtube
    ):
        result = PlatformRescheduleService.notify(
            project, "youtube",
            datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
        )
    assert result.status == "ok"
    args, kwargs = fake_youtube.videos.return_value.update.call_args
    body = kwargs["body"]
    assert body["id"] == "abc12345"
    assert body["status"]["publishAt"].startswith("2026-05-08T14:00:00")
    assert body["status"]["privacyStatus"] == "private"


def test_cancel_youtube_clears_publish_at_and_sets_private():
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"youtube": {"url": "https://youtu.be/abc12345"}}},
    )
    fake_youtube = MagicMock()
    fake_youtube.videos.return_value.update.return_value.execute.return_value = {"id": "abc12345"}

    with patch(
        "app.services.platform_reschedule_service.AccountService.get_youtube_credentials"
    ), patch(
        "app.services.platform_reschedule_service.build", return_value=fake_youtube
    ):
        result = PlatformRescheduleService.cancel(project, "youtube")
    assert result.status == "ok"
    body = fake_youtube.videos.return_value.update.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert "publishAt" not in body["status"]
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: NotImplementedError.

- [ ] **Step 3: Implement YouTube methods**

In `backend/app/services/platform_reschedule_service.py`, replace the YouTube stubs:

```python
# Top of file — add imports:
from googleapiclient.discovery import build

from .account_service import AccountService
```

Then:

```python
    @classmethod
    def _notify_youtube(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        video_id = cls._youtube_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        creds = AccountService.get_youtube_credentials(project.scheduled_account_id)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": new_scheduled_at.isoformat(),
            },
        }
        youtube.videos().update(part="status", body=body).execute()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_youtube(cls, project: Project, url: str) -> NotificationResult:
        video_id = cls._youtube_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        creds = AccountService.get_youtube_credentials(project.scheduled_account_id)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "id": video_id,
            "status": {"privacyStatus": "private"},
        }
        youtube.videos().update(part="status", body=body).execute()
        return NotificationResult(status="ok")
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/platform_reschedule_service.py backend/tests/test_platform_reschedule_service.py
git commit -m "feat(reschedule): implement YouTube notify and cancel via videos.update"
```

---

## Task 11: Implement Facebook notify + cancel

**Files:**
- Modify: `backend/app/services/platform_reschedule_service.py`
- Test: `backend/tests/test_platform_reschedule_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_notify_facebook_posts_scheduled_publish_time(monkeypatch):
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"facebook": {"url": "https://www.facebook.com/page/videos/9876543210/"}}},
    )

    posted: dict = {}
    class FakeResp:
        status_code = 200
        def json(self) -> dict:
            return {"success": True}
        def raise_for_status(self) -> None:
            return None

    def fake_post(url, data=None, **kwargs):
        posted["url"] = url
        posted["data"] = data
        return FakeResp()

    monkeypatch.setattr(
        "app.services.platform_reschedule_service.AccountService.get_meta_credentials",
        lambda _id: type("C", (), {"facebook_page_access_token": "tok", "page_id": "p"})(),
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.post", fake_post
    )

    result = PlatformRescheduleService.notify(
        project, "facebook",
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
    )
    assert result.status == "ok"
    assert "9876543210" in posted["url"]
    assert posted["data"]["scheduled_publish_time"] == int(
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc).timestamp()
    )


def test_cancel_facebook_marks_unpublished(monkeypatch):
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"facebook": {"url": "https://www.facebook.com/page/videos/9876543210/"}}},
    )

    posted: dict = {}
    class FakeResp:
        status_code = 200
        def json(self): return {"success": True}
        def raise_for_status(self): return None
    def fake_post(url, data=None, **kwargs):
        posted["data"] = data
        return FakeResp()
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.AccountService.get_meta_credentials",
        lambda _id: type("C", (), {"facebook_page_access_token": "tok", "page_id": "p"})(),
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.post", fake_post)

    result = PlatformRescheduleService.cancel(project, "facebook")
    assert result.status == "ok"
    assert posted["data"]["published"] == "false"
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: NotImplementedError.

- [ ] **Step 3: Implement Facebook methods**

Add at the top of `platform_reschedule_service.py`:

```python
import httpx
```

Then:

```python
    _FB_GRAPH_VERSION = "v25.0"

    @classmethod
    def _notify_facebook(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        video_id = cls._facebook_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        creds = AccountService.get_meta_credentials(project.scheduled_account_id)
        epoch = int(new_scheduled_at.timestamp())
        api_url = f"https://graph.facebook.com/{cls._FB_GRAPH_VERSION}/{video_id}"
        resp = httpx.post(
            api_url,
            data={
                "scheduled_publish_time": epoch,
                "published": "false",
                "access_token": creds.facebook_page_access_token,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_facebook(cls, project: Project, url: str) -> NotificationResult:
        video_id = cls._facebook_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        creds = AccountService.get_meta_credentials(project.scheduled_account_id)
        api_url = f"https://graph.facebook.com/{cls._FB_GRAPH_VERSION}/{video_id}"
        resp = httpx.post(
            api_url,
            data={
                "published": "false",
                "access_token": creds.facebook_page_access_token,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        return NotificationResult(status="ok")
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/platform_reschedule_service.py backend/tests/test_platform_reschedule_service.py
git commit -m "feat(reschedule): implement Facebook notify and cancel via Graph API"
```

---

## Task 12: Implement Instagram notify + cancel via /server/

**Files:**
- Modify: `backend/app/services/platform_reschedule_service.py`
- Modify: `backend/app/config.py` — verify `tiktok_server_url` and `tiktok_server_internal_token` exist (they should already; re-verify)
- Test: `backend/tests/test_platform_reschedule_service.py`

- [ ] **Step 1: Verify config has the server settings**

```bash
grep -n "tiktok_server\|server_internal_token" backend/app/config.py
```

If missing: add them (existing infra usually has these — only add if grep returns nothing). Otherwise skip to step 2.

- [ ] **Step 2: Write the failing tests**

Append:

```python
def test_notify_instagram_patches_server_endpoint(monkeypatch):
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"instagram": {"url": "https://instagram.com/p/abc"}}},
    )

    captured: dict = {}
    class FakeResp:
        status_code = 200
        def raise_for_status(self): return None
    def fake_patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://server.example.com",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "secret",
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.patch", fake_patch)

    result = PlatformRescheduleService.notify(
        project, "instagram",
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
    )
    assert result.status == "ok"
    assert captured["url"] == "https://server.example.com/api/internal/jobs/p1/slot"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["slot_time"].startswith("2026-05-08T14:00:00")
    assert "instagram" in captured["json"]["platform_scheduled_at"]


def test_cancel_instagram_deletes_server_job(monkeypatch):
    project = Project(id="p1", scheduled_account_id="acc_a")

    captured: dict = {}
    class FakeResp:
        status_code = 204
        def raise_for_status(self): return None
    def fake_delete(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResp()
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://server.example.com",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "secret",
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.delete", fake_delete)

    result = PlatformRescheduleService.cancel(project, "instagram")
    assert result.status == "ok"
    assert captured["url"] == "https://server.example.com/api/internal/jobs/p1"
```

- [ ] **Step 3: Run — fail**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: NotImplementedError.

- [ ] **Step 4: Implement Instagram methods**

Add at the top of `platform_reschedule_service.py`:

```python
from ..config import settings
```

Then:

```python
    @classmethod
    def _notify_instagram(cls, project: Project, new_scheduled_at: datetime) -> NotificationResult:
        url = settings.tiktok_server_url.rstrip("/") + f"/api/internal/jobs/{project.id}/slot"
        resp = httpx.patch(
            url,
            json={
                "slot_time": new_scheduled_at.isoformat(),
                "platform_scheduled_at": {"instagram": new_scheduled_at.isoformat()},
            },
            headers={"Authorization": f"Bearer {settings.tiktok_server_internal_token}"},
            timeout=20.0,
        )
        if resp.status_code == 404:
            return NotificationResult(status="skipped")
        resp.raise_for_status()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_instagram(cls, project: Project) -> NotificationResult:
        url = settings.tiktok_server_url.rstrip("/") + f"/api/internal/jobs/{project.id}"
        resp = httpx.delete(
            url,
            headers={"Authorization": f"Bearer {settings.tiktok_server_internal_token}"},
            timeout=20.0,
        )
        if resp.status_code == 404:
            return NotificationResult(status="skipped")
        resp.raise_for_status()
        return NotificationResult(status="ok")
```

Update `cancel()` dispatch — it already covers IG without URL via the branch in step 3 of Task 9 (`if not url and platform != "instagram"`). Good.

- [ ] **Step 5: Run — pass**

```bash
pixi run -- pytest backend/tests/test_platform_reschedule_service.py -v
```

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/platform_reschedule_service.py backend/tests/test_platform_reschedule_service.py
git commit -m "feat(reschedule): implement Instagram notify and cancel via internal server API"
```

---

## Task 13: Implement `RescheduleRetryService`

**Files:**
- Create: `backend/app/services/reschedule_retry_service.py`
- Test: `backend/tests/test_reschedule_retry_service.py` (new)

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_reschedule_retry_service.py`:

```python
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.models import Project
from app.services.platform_reschedule_service import NotificationResult
from app.services.project_service import ProjectService
from app.services.reschedule_retry_service import (
    RescheduleRetryService,
    _BACKOFF_STEPS,
)


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch):
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", pdir
    )
    return pdir


def _seed(pid: str, target: datetime, retries: int = 0, last_attempt: datetime | None = None):
    project = Project(id=pid)
    project.reschedule_pending = {
        "youtube": {
            "target_scheduled_at": target,
            "retries": retries,
            "last_error": "boom",
            "last_attempt_at": last_attempt or datetime.now(timezone.utc),
        }
    }
    ProjectService.save(project)


def test_retry_clears_entry_on_success(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=0,
          last_attempt=datetime.now(timezone.utc) - timedelta(minutes=10))

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="ok"),
    ):
        asyncio.run(RescheduleRetryService.run_once())

    project = ProjectService.load("p1")
    assert project.reschedule_pending == {}


def test_retry_increments_retries_on_failure(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=0,
          last_attempt=datetime.now(timezone.utc) - timedelta(minutes=10))

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="pending_retry", error="503"),
    ):
        asyncio.run(RescheduleRetryService.run_once())

    project = ProjectService.load("p1")
    assert project.reschedule_pending["youtube"]["retries"] == 1
    assert project.reschedule_pending["youtube"]["last_error"] == "503"


def test_retry_alerts_after_5_failures(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=4,
          last_attempt=datetime.now(timezone.utc) - timedelta(hours=2))
    alerts: list = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="pending_retry", error="boom"),
    ), patch(
        "app.services.reschedule_retry_service._post_discord_alert", new=fake_alert
    ):
        asyncio.run(RescheduleRetryService.run_once())

    assert any("p1" in a and "youtube" in a for a in alerts)
    project = ProjectService.load("p1")
    # Entry retained for ops review
    assert "youtube" in project.reschedule_pending


def test_retry_skips_when_backoff_not_elapsed(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=0,
          last_attempt=datetime.now(timezone.utc) - timedelta(seconds=10))

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="ok"),
    ) as notify_mock:
        asyncio.run(RescheduleRetryService.run_once())
    notify_mock.assert_not_called()
```

- [ ] **Step 2: Run — fail**

Expected: import error.

- [ ] **Step 3: Implement the service**

`backend/app/services/reschedule_retry_service.py`:

```python
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..models import Project
from .platform_reschedule_service import PlatformRescheduleService
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")

# Backoff steps applied as (retries -> wait-since-last-attempt).
_BACKOFF_STEPS: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
_MAX_RETRIES_BEFORE_ALERT = 5
_POLL_INTERVAL_SECONDS = 60


async def _post_discord_alert(message: str) -> None:
    """Best-effort Discord alert — silenced on error."""
    try:
        from .discord_service import DiscordService  # noqa: PLC0415
        await DiscordService.post_alert(message)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Discord alert failed: %s", exc)


class RescheduleRetryService:
    @classmethod
    def _backoff_for_retries(cls, retries: int) -> timedelta:
        idx = min(retries, len(_BACKOFF_STEPS) - 1)
        return _BACKOFF_STEPS[idx]

    @classmethod
    async def run_once(cls) -> None:
        now_utc = datetime.now(timezone.utc)
        for project in ProjectService.list_all():
            pending = dict(project.reschedule_pending or {})
            if not pending:
                continue
            updated = False
            for platform, entry in list(pending.items()):
                last_attempt = entry.get("last_attempt_at")
                retries = int(entry.get("retries") or 0)
                target = entry.get("target_scheduled_at")
                if not isinstance(last_attempt, datetime):
                    last_attempt = now_utc
                if last_attempt.tzinfo is None:
                    last_attempt = last_attempt.replace(tzinfo=timezone.utc)
                if not isinstance(target, datetime):
                    continue
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                if now_utc - last_attempt < cls._backoff_for_retries(retries):
                    continue

                result = await asyncio.to_thread(
                    PlatformRescheduleService.notify, project, platform, target
                )
                if result.status == "ok":
                    pending.pop(platform, None)
                    updated = True
                    continue

                retries += 1
                entry["retries"] = retries
                entry["last_error"] = result.error or entry.get("last_error")
                entry["last_attempt_at"] = now_utc
                pending[platform] = entry
                updated = True

                if retries >= _MAX_RETRIES_BEFORE_ALERT:
                    await _post_discord_alert(
                        f"[reschedule-retry] project={project.id} platform={platform} "
                        f"failed {retries} times: {entry.get('last_error')}"
                    )

            if updated:
                project.reschedule_pending = pending
                ProjectService.save(project)

    @classmethod
    async def run_loop(cls, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await cls.run_once()
            except Exception:
                logger.exception("RescheduleRetryService.run_once failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_reschedule_retry_service.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reschedule_retry_service.py backend/tests/test_reschedule_retry_service.py
git commit -m "feat(reschedule): add async retry loop with exponential backoff"
```

---

## Task 14: Wire retry service into `main.py` lifespan

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: Verify the lifespan function signature**

```bash
grep -n "async def lifespan" backend/app/main.py
```

- [ ] **Step 2: Add the retry loop start/stop**

Edit `backend/app/main.py`. After existing imports, add:

```python
from .services.reschedule_retry_service import RescheduleRetryService
```

Inside `lifespan`, after `await project_upload_queue.startup_cleanup()` (around line 126), append:

```python
    reschedule_retry_stop = asyncio.Event()
    app.state.reschedule_retry_stop = reschedule_retry_stop
    _track_app_task(
        app,
        asyncio.create_task(
            RescheduleRetryService.run_loop(reschedule_retry_stop),
            name="reschedule-retry-loop",
        ),
    )
```

Just before `await _cancel_app_tasks(app)`:

```python
    reschedule_retry_stop.set()
```

- [ ] **Step 3: Run the existing test suite to make sure no regression**

```bash
pixi run test
```

Expected: all backend tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/main.py
git commit -m "feat(reschedule): start RescheduleRetryService loop in lifespan"
```

---

## Task 15: Create the `/api/scheduling` router — read endpoints

**Files:**
- Create: `backend/app/api/routes/scheduling.py`
- Test: `backend/tests/test_scheduling_routes.py` (new)

- [ ] **Step 1: Write the failing tests for GET /events and GET /free-slots**

`backend/tests/test_scheduling_routes.py`:

```python
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.models import PlatformSchedule, Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


_NOW = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    cfg = tmp_path / "accounts.yaml"
    cfg.write_text("""\
accounts:
  acc_a:
    name: "A"
    language: "fr"
    device: "poco"
    slots: ["14:00", "18:00"]
    youtube:
      refresh_token: "tok"
    tiktok:
      slots: ["12:00", "14:00", "18:00"]
""", encoding="utf-8")
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr("app.services.account_service.settings.accounts_config_path", cfg)
    monkeypatch.setattr("app.services.scheduling_service.datetime", _FixedDateTime)
    AccountService.invalidate()

    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c
    AccountService.invalidate()


def test_list_events_returns_filtered_events(client):
    project = Project(id="p1", anime_name="Show",
        scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 18, 5, tzinfo=timezone.utc),
            )
        }
    )
    ProjectService.save(project)
    r = client.get("/api/scheduling/events")
    assert r.status_code == 200
    events = r.json()["events"]
    assert any(e["project_id"] == "p1" and e["platform"] == "tiktok" for e in events)


def test_free_slots_endpoint(client):
    r = client.get("/api/scheduling/free-slots", params={
        "account_id": "acc_a", "platform": "tiktok",
        "after": _NOW.isoformat(), "limit": 4,
    })
    assert r.status_code == 200
    slots = r.json()["slots"]
    assert len(slots) == 4
    assert all("slot" in s and "available" in s for s in slots)
```

- [ ] **Step 2: Run — fail (router not registered yet)**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 404 on both.

- [ ] **Step 3: Create the router with the read endpoints**

`backend/app/api/routes/scheduling.py`:

```python
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ...models import Project
from ...services.account_service import AccountService
from ...services.project_service import ProjectService
from ...services.scheduling_service import SchedulingService

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

Platform = Literal["youtube", "facebook", "instagram", "tiktok"]


class PlanningEvent(BaseModel):
    project_id: str
    anime_title: str
    account_id: str
    account_avatar_url: str
    account_name: str
    platform: Platform
    slot: datetime
    scheduled_at: datetime
    drive_folder_url: str | None
    status: Literal["scheduled", "running", "complete"]


class FreeSlotResponse(BaseModel):
    slot: datetime
    available: bool
    taken_by_project_id: str | None = None


def _project_event_status(project: Project, platform: str) -> str:
    from ...services.project_upload_service import project_upload_queue  # noqa: PLC0415
    for job in project_upload_queue.list_jobs():
        if job.project_id == project.id:
            if job.status == "running":
                return "running"
            if job.status == "complete":
                return "complete"
    return "scheduled"


def _build_planning_event(
    project: Project, platform: str, accounts: dict
) -> PlanningEvent | None:
    sched = (project.platform_schedules or {}).get(platform)
    if sched is None or project.scheduled_account_id is None:
        return None
    account = accounts.get(project.scheduled_account_id)
    if account is None:
        return None
    return PlanningEvent(
        project_id=project.id,
        anime_title=project.anime_name or project.id,
        account_id=account.id,
        account_avatar_url=f"/api/accounts/{account.id}/avatar",
        account_name=account.name,
        platform=platform,  # type: ignore[arg-type]
        slot=sched.slot,
        scheduled_at=sched.scheduled_at,
        drive_folder_url=project.drive_folder_url,
        status=_project_event_status(project, platform),  # type: ignore[arg-type]
    )


def _platforms_visible_for_account_filter(
    selected_account_id: str | None,
    project: Project,
    platform: str,
) -> bool:
    if selected_account_id is None:
        return True
    if project.scheduled_account_id is None:
        return False
    selected = AccountService.get_account(selected_account_id)
    owner = AccountService.get_account(project.scheduled_account_id)
    if selected is None or owner is None:
        return False
    selected_pool = selected.pool_key_for(platform) or f"account:{selected.id}:{platform}"
    owner_pool = owner.pool_key_for(platform) or f"account:{owner.id}:{platform}"
    return selected_pool == owner_pool


@router.get("/events")
async def list_events(
    account_id: str | None = None,
    platforms: str | None = None,  # CSV
    range_start: datetime | None = None,
    range_end: datetime | None = None,
):
    accounts = AccountService.all_accounts()
    wanted_platforms = (
        [p.strip() for p in platforms.split(",") if p.strip()]
        if platforms else ["youtube", "facebook", "instagram", "tiktok"]
    )
    events: list[dict] = []
    now = datetime.now(tz=range_start.tzinfo if range_start else None)

    for project in await asyncio.to_thread(ProjectService.list_all):
        for platform in wanted_platforms:
            if not _platforms_visible_for_account_filter(account_id, project, platform):
                continue
            ev = _build_planning_event(project, platform, accounts)
            if ev is None:
                continue
            if ev.slot < (range_start or now):
                continue
            if range_end and ev.slot > range_end:
                continue
            events.append(ev.model_dump(mode="json"))
    return {"events": events}


@router.get("/free-slots")
async def free_slots(
    account_id: str,
    platform: Platform,
    after: datetime,
    limit: int = Query(default=20, ge=1, le=200),
):
    try:
        slots = await asyncio.to_thread(
            SchedulingService.find_free_slots_after,
            account_id, platform, after, limit,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {
        "slots": [
            FreeSlotResponse(
                slot=s.slot,
                available=s.available,
                taken_by_project_id=s.taken_by_project_id,
            ).model_dump(mode="json")
            for s in slots
        ]
    }
```

Register the router. Edit [backend/app/api/routes/__init__.py:17](backend/app/api/routes/__init__.py#L17), add:

```python
from .scheduling import router as scheduling_router
```

After the other `include_router` calls:

```python
api_router.include_router(scheduling_router)
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/scheduling.py backend/app/api/routes/__init__.py backend/tests/test_scheduling_routes.py
git commit -m "feat(api): add GET /scheduling/events and /free-slots"
```

---

## Task 16: Add anchor + reserve + reschedule endpoints

**Files:**
- Modify: `backend/app/api/routes/scheduling.py`
- Test: `backend/tests/test_scheduling_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_scheduling_routes.py`:

```python
def test_resolve_anchor_endpoint(client):
    ProjectService.save(Project(id="p1"))
    r = client.post("/api/scheduling/resolve-anchor", json={
        "project_id": "p1",
        "account_id": "acc_a",
        "tiktok_slot": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc).isoformat(),
    })
    assert r.status_code == 200
    body = r.json()
    assert "tiktok" in body["resolved"]
    assert body["conflicts"] == []


def test_reserve_anchor_endpoint(client):
    ProjectService.save(Project(id="p1"))
    r = client.post("/api/scheduling/projects/p1/reserve-anchor", json={
        "account_id": "acc_a",
        "tiktok_slot": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc).isoformat(),
    })
    assert r.status_code == 200
    schedules = r.json()["platform_schedules"]
    assert "tiktok" in schedules


def test_patch_platform_endpoint(client):
    ProjectService.save(Project(id="p1"))
    SchedulingService.reserve_anchor(
        "p1", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    r = client.patch(
        "/api/scheduling/projects/p1/platforms/youtube",
        json={"new_slot": datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc).isoformat()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["slot"].startswith("2026-05-08T14:00:00")
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 3 new failures (404).

- [ ] **Step 3: Implement the endpoints**

Append to `scheduling.py`:

```python
class ResolveAnchorRequest(BaseModel):
    project_id: str
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


class ReserveAnchorRequest(BaseModel):
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


class PatchPlatformRequest(BaseModel):
    new_slot: datetime


class PatchAnchorRequest(BaseModel):
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


def _platform_schedules_to_dict(schedules):
    return {
        p: {"slot": s.slot.isoformat(), "scheduled_at": s.scheduled_at.isoformat()}
        for p, s in schedules.items()
    }


def _notify_displaced(project_id: str, platform: str, new_scheduled_at: datetime) -> str:
    """Trigger platform notification, return 'ok' / 'pending_retry' / 'skipped'.

    Mutates project.reschedule_pending on pending_retry to feed the retry loop.
    """
    from ...services.platform_reschedule_service import PlatformRescheduleService  # noqa: PLC0415
    project = ProjectService.load(project_id)
    if project is None:
        return "skipped"
    result = PlatformRescheduleService.notify(project, platform, new_scheduled_at)
    if result.status == "pending_retry":
        pending = dict(project.reschedule_pending or {})
        pending[platform] = {
            "target_scheduled_at": new_scheduled_at,
            "retries": 0,
            "last_error": result.error,
            "last_attempt_at": datetime.now(tz=new_scheduled_at.tzinfo),
        }
        project.reschedule_pending = pending
        ProjectService.save(project)
    return result.status


@router.post("/resolve-anchor")
async def resolve_anchor(req: ResolveAnchorRequest):
    result = await asyncio.to_thread(
        SchedulingService.resolve_anchor,
        req.account_id, req.tiktok_slot, req.overrides,
    )
    return {
        "resolved": {
            p: {"slot": r.slot.isoformat(), "scheduled_at": r.scheduled_at.isoformat(),
                "available": r.available}
            for p, r in result.resolved.items()
        },
        "conflicts": [{"platform": c.platform, "reason": c.reason} for c in result.conflicts],
    }


@router.post("/projects/{project_id}/reserve-anchor")
async def reserve_anchor(project_id: str, req: ReserveAnchorRequest):
    try:
        schedules = await asyncio.to_thread(
            SchedulingService.reserve_anchor,
            project_id, req.account_id, req.tiktok_slot, req.overrides,
        )
    except ValueError as exc:
        msg = str(exc)
        if "tiktok" in msg and "slot_taken" in msg:
            raise HTTPException(409, "tiktok_slot_taken")
        if "slot_not_configured" in msg:
            raise HTTPException(422, "invalid_slot")
        if "Project not found" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(422, msg)
    return {"platform_schedules": _platform_schedules_to_dict(schedules)}


@router.patch("/projects/{project_id}/platforms/{platform}")
async def patch_platform(project_id: str, platform: str, req: PatchPlatformRequest):
    try:
        sched = await asyncio.to_thread(
            SchedulingService.reschedule_platform, project_id, platform, req.new_slot
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    notif_status = await asyncio.to_thread(
        _notify_displaced, project_id, platform, sched.scheduled_at
    )
    return {
        "slot": sched.slot.isoformat(),
        "scheduled_at": sched.scheduled_at.isoformat(),
        "notification_status": notif_status,
    }


@router.patch("/projects/{project_id}/anchor")
async def patch_anchor(project_id: str, req: PatchAnchorRequest):
    try:
        schedules = await asyncio.to_thread(
            SchedulingService.reschedule_anchor,
            project_id, req.tiktok_slot, req.overrides,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    statuses: dict[str, str] = {}
    for platform, sched in schedules.items():
        statuses[platform] = await asyncio.to_thread(
            _notify_displaced, project_id, platform, sched.scheduled_at
        )
    return {
        "platform_schedules": _platform_schedules_to_dict(schedules),
        "notification_status": statuses,
    }
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/scheduling.py backend/tests/test_scheduling_routes.py
git commit -m "feat(api): add anchor resolution, reserve, and per-platform reschedule"
```

---

## Task 17: Add cancel endpoints

**Files:**
- Modify: `backend/app/api/routes/scheduling.py`
- Test: `backend/tests/test_scheduling_routes.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_delete_platform_endpoint(client):
    ProjectService.save(Project(id="p1"))
    SchedulingService.reserve_anchor(
        "p1", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    r = client.delete("/api/scheduling/projects/p1/platforms/youtube")
    assert r.status_code == 204
    project = ProjectService.load("p1")
    assert "youtube" not in project.platform_schedules


def test_delete_all_endpoint(client):
    ProjectService.save(Project(id="p1"))
    SchedulingService.reserve_anchor(
        "p1", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    r = client.delete("/api/scheduling/projects/p1/all")
    assert r.status_code == 204
    project = ProjectService.load("p1")
    assert project.platform_schedules == {}
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 2 failures (404).

- [ ] **Step 3: Implement**

Append to `scheduling.py`:

```python
def _notify_cancellation(project_id: str, platform: str) -> str:
    from ...services.platform_reschedule_service import PlatformRescheduleService  # noqa: PLC0415
    project = ProjectService.load(project_id)
    if project is None:
        return "skipped"
    return PlatformRescheduleService.cancel(project, platform).status


@router.delete("/projects/{project_id}/platforms/{platform}", status_code=204)
async def delete_platform(project_id: str, platform: str):
    await asyncio.to_thread(_notify_cancellation, project_id, platform)
    await asyncio.to_thread(SchedulingService.cancel_platform_slot, project_id, platform)


@router.delete("/projects/{project_id}/all", status_code=204)
async def delete_all(project_id: str):
    project = await asyncio.to_thread(ProjectService.load, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    for platform in list(project.platform_schedules.keys()):
        await asyncio.to_thread(_notify_cancellation, project_id, platform)
    await asyncio.to_thread(SchedulingService.cancel_all_slots, project_id)
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/scheduling.py backend/tests/test_scheduling_routes.py
git commit -m "feat(api): add cancel-platform and cancel-all endpoints"
```

---

## Task 18: Add cascade preview + apply endpoints + reschedule-pending listing

**Files:**
- Modify: `backend/app/api/routes/scheduling.py`
- Test: `backend/tests/test_scheduling_routes.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_cascade_preview_endpoint(client):
    ProjectService.save(Project(id="other", scheduled_account_id="acc_a",
        anime_name="Other",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 6, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="urgent", anime_name="Urgent"))

    r = client.post("/api/scheduling/projects/urgent/cascade-preview",
                    json={"account_id": "acc_a"})
    assert r.status_code == 200
    body = r.json()
    tt = next(p for p in body["per_platform"] if p["platform"] == "tiktok")
    assert len(tt["displaced"]) == 1


def test_cascade_apply_endpoint(client):
    ProjectService.save(Project(id="other", scheduled_account_id="acc_a",
        anime_name="Other",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 6, tzinfo=timezone.utc),
            )
        }
    ))
    ProjectService.save(Project(id="urgent", anime_name="Urgent"))

    r = client.post("/api/scheduling/projects/urgent/cascade-apply",
                    json={"account_id": "acc_a"})
    assert r.status_code == 200
    other = ProjectService.load("other")
    assert other.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 18, 0, tzinfo=timezone.utc
    )


def test_reschedule_pending_endpoint(client):
    project = Project(id="p1")
    project.reschedule_pending = {
        "youtube": {
            "target_scheduled_at": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            "retries": 2,
            "last_error": "503",
            "last_attempt_at": datetime(2026, 5, 7, 14, 5, tzinfo=timezone.utc),
        }
    }
    ProjectService.save(project)
    r = client.get("/api/scheduling/reschedule-pending")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["project_id"] == "p1" and i["platform"] == "youtube" for i in items)
```

- [ ] **Step 2: Run — fail**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 3 failures.

- [ ] **Step 3: Implement**

Append to `scheduling.py`:

```python
class CascadeRequest(BaseModel):
    account_id: str


def _cascade_to_payload(result) -> dict:
    return {
        "per_platform": [
            {
                "platform": p.platform,
                "target_slot": p.target_slot.isoformat(),
                "target_scheduled_at": p.target_scheduled_at.isoformat(),
                "displaced": [
                    {
                        "project_id": d.project_id,
                        "anime_title": d.anime_title,
                        "from_slot": d.from_slot.isoformat(),
                        "to_slot": d.to_slot.isoformat(),
                        "requires_platform_notification": d.requires_platform_notification,
                    }
                    for d in p.displaced
                ],
            }
            for p in result.per_platform
        ],
        "blockers": [{"platform": b.platform, "reason": b.reason} for b in result.blockers],
    }


@router.post("/projects/{project_id}/cascade-preview")
async def cascade_preview(project_id: str, req: CascadeRequest):
    result = await asyncio.to_thread(
        SchedulingService.compute_cascade, project_id, req.account_id
    )
    return _cascade_to_payload(result)


@router.post("/projects/{project_id}/cascade-apply")
async def cascade_apply(project_id: str, req: CascadeRequest):
    try:
        result = await asyncio.to_thread(
            SchedulingService.apply_cascade, project_id, req.account_id
        )
    except ValueError as exc:
        msg = str(exc)
        if "pool_busy" in msg:
            raise HTTPException(409, msg)
        if "facebook_horizon_exceeded" in msg or "pool_full" in msg:
            raise HTTPException(422, msg)
        raise HTTPException(422, msg)

    notification_status: dict[str, dict[str, str]] = {}
    for plat in result.per_platform:
        notification_status[plat.platform] = {}
        for displaced in plat.displaced:
            ts = await asyncio.to_thread(
                _notify_displaced,
                displaced.project_id,
                plat.platform,
                # use the recomputed scheduled_at written by apply_cascade
                ProjectService.load(displaced.project_id)
                    .platform_schedules[plat.platform].scheduled_at,
            )
            notification_status[plat.platform][displaced.project_id] = ts

    payload = _cascade_to_payload(result)
    payload["notification_status"] = notification_status
    return payload


@router.get("/reschedule-pending")
async def reschedule_pending():
    items: list[dict] = []
    for project in await asyncio.to_thread(ProjectService.list_all):
        for platform, entry in (project.reschedule_pending or {}).items():
            items.append({
                "project_id": project.id,
                "platform": platform,
                "target_scheduled_at": entry.get("target_scheduled_at"),
                "retries": entry.get("retries", 0),
                "last_error": entry.get("last_error"),
                "last_attempt_at": entry.get("last_attempt_at"),
            })
    return {"items": items}
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/scheduling.py backend/tests/test_scheduling_routes.py
git commit -m "feat(api): add cascade preview/apply and reschedule-pending listing"
```

---

## Task 19: Add `ATR_SCHEDULING_V2_ENABLED` feature flag

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/api/routes/scheduling.py` (gate)
- Test: `backend/tests/test_scheduling_routes.py`

- [ ] **Step 1: Add the setting**

Edit `backend/app/config.py`. In the `Settings` class, add:

```python
    scheduling_v2_enabled: bool = True
```

(Pydantic settings reads `ATR_SCHEDULING_V2_ENABLED` automatically given the existing `env_prefix="ATR_"` convention.)

- [ ] **Step 2: Add a feature-flag dependency in scheduling.py**

In `backend/app/api/routes/scheduling.py`, near the imports:

```python
from fastapi import Depends

from ...config import settings as app_settings


def _require_v2() -> None:
    if not app_settings.scheduling_v2_enabled:
        raise HTTPException(503, "scheduling_v2_disabled")
```

Apply at the router level:

```python
router = APIRouter(
    prefix="/scheduling",
    tags=["scheduling"],
    dependencies=[Depends(_require_v2)],
)
```

- [ ] **Step 3: Add a flag-off test**

Append:

```python
def test_router_disabled_when_flag_off(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.scheduling.app_settings.scheduling_v2_enabled", False
    )
    r = client.get("/api/scheduling/events")
    assert r.status_code == 503
```

- [ ] **Step 4: Run — pass**

```bash
pixi run -- pytest backend/tests/test_scheduling_routes.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/api/routes/scheduling.py backend/tests/test_scheduling_routes.py
git commit -m "feat(scheduling): add ATR_SCHEDULING_V2_ENABLED feature flag"
```

---

## Task 20: Final regression — all tests green

- [ ] **Step 1: Run the full test suite**

```bash
pixi run test
cd server && pixi run -- pytest tests/ -v
```

Expected: every backend and server test passes.

- [ ] **Step 2: Smoke-run the FastAPI app**

```bash
pixi run backend &
sleep 3
curl -s http://127.0.0.1:8000/api/scheduling/events | jq
curl -s "http://127.0.0.1:8000/api/scheduling/free-slots?account_id=acc_a&platform=tiktok&after=2026-05-07T12:00:00%2B00:00&limit=3" | jq
kill %1
```

Expected: JSON responses (events list possibly empty, free-slots list of 3 entries).

- [ ] **Step 3: Commit any cleanup**

```bash
git status
# (no expected changes)
```

---

**Phase 1 complete.** Backend offers a full REST API for the Planning view, manual picker, and urgent cascade — including platform notifications and retries. Phase 2 (Planning frontend) and Phase 3 (Upload UX) consume this API.
