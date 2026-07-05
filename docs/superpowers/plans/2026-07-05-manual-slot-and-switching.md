# Manual Custom-Time Upload & Slot Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add (1) manual custom-time scheduling that lives entirely outside the slot pool yet displays/publishes normally everywhere, and (2) a slot-switching system where taken slots are amber+clickable and a confirmation popup lets the user displace the occupant via chain cascade or next-free-slot.

**Architecture:** `PlatformSchedule` gains a `manual` flag excluded from pool counting at a single point (`_collect_pool_reservations`). Switching reuses the urgent-cascade walk generalized to an arbitrary start slot (`compute_switch`/`apply_switch` + `steals` param on `reserve_anchor` for atomicity with the anchor flow). Displaced projects are notified through the existing `PlatformRescheduleService` (YT/FB/IG/TT all covered) with the existing `reschedule_pending` retry queue as fallback.

**Tech Stack:** FastAPI + Pydantic (backend), React + TypeScript + framer-motion (frontend), pytest (via `pixi run test`), Playwright e2e.

**Spec:** `docs/superpowers/specs/2026-07-05-manual-slot-and-switching-design.md`

## Global Constraints

- Manual entries: `slot == scheduled_at == exact user time`, `manual=True`, **no jitter**.
- Manual entries are invisible to pool counting, cascades, and switches — enforced ONLY in `_collect_pool_reservations()` (all occupancy maps must go through it).
- Minimum lead time everywhere: `now + 30 min` (`_MIN_LEAD_MINUTES`).
- Only the stolen platform's schedule moves on a switch; the occupant's other platforms stay.
- Displacement writes happen farthest-first (reverse order) under `_reservation_lock`.
- `apply_switch` re-verifies the occupant vs `expected_occupant_id` → `slot_state_changed` (HTTP 409) on mismatch.
- French UI copy in frontend (match existing style: "Heure personnalisée", "Programmer (manuel)", "Cascader", "Slot libre suivant", "Annuler", "Programmation manuelle").
- Backend tests: `pixi run test tests/<file>.py -v` (task cwd is `backend/`). Frontend type check: `cd frontend && npx tsc -b --noEmit` (or `npm run build`).
- Commit after every task (conventional commits, `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`).

## Delegation Map (orchestrator: Fable — do not implement tasks inline)

| Task | Subagent model | Rationale |
|---|---|---|
| 1, 2, 4, 5 | opus | Core scheduling-service logic, subtle invariants |
| 3, 6, 7 | opus | Route plumbing + notification loops |
| 8, 9, 13 | sonnet | Mechanical types/API-client/badge edits |
| 10, 11, 12 | opus | New modal + picker state machine + parent wiring |
| 14 | opus | e2e + full verification sweep |

---

### Task 1: `manual` flag + single-point pool exclusion + occupancy refactor

**Files:**
- Modify: `backend/app/models/project.py:27-31` (PlatformSchedule)
- Modify: `backend/app/services/scheduling_service.py:98-124` (`_collect_pool_reservations`, `_collect_reserved_slots_for_pool`), `:13-17` (FreeSlot), `:214-271` (find_free_slots_after), `:764-782` (compute_cascade occupancy map)
- Test: `backend/tests/test_scheduling_service.py` (append)

**Interfaces:**
- Consumes: existing `PlatformSchedule`, `_collect_pool_reservations` returning `dict[slot_iso, project_id]`.
- Produces:
  - `PlatformSchedule(slot, scheduled_at, manual: bool = False)`
  - `_collect_pool_reservations(pool_key, platform) -> dict[str, Project]` (values are now **Project objects**, manual entries skipped)
  - `FreeSlot(slot, available, taken_by_project_id, taken_by_title: str | None = None)`

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_scheduling_service.py`)

```python
def _setup_single_account(tmp_path, monkeypatch, slots=("10:00", "14:00", "18:00")):
    """One account 'acc1' with the given top-level slots. Returns 'acc1'."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(exist_ok=True)
    slot_yaml = ", ".join(f'"{s}"' for s in slots)
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        f"""\
accounts:
  acc1:
    name: "Acc 1"
    language: "fr"
    device: "poco"
    slots: [{slot_yaml}]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    AccountService.invalidate()
    return "acc1"


def _save_scheduled_project(pid, account_id, platform, slot_dt, manual=False, title=None):
    from app.models import PlatformSchedule
    project = Project(id=pid, anime_name=title or pid)
    project.scheduled_account_id = account_id
    project.platform_schedules = {
        platform: PlatformSchedule(slot=slot_dt, scheduled_at=slot_dt, manual=manual)
    }
    ProjectService.save(project)
    return project


def test_manual_entries_do_not_block_slots(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    _save_scheduled_project("manualproj", acc, "tiktok", tomorrow, manual=True)

    slots = SchedulingService.find_free_slots_after(
        acc, "tiktok", tomorrow - timedelta(minutes=1), 1
    )
    assert slots[0].slot == tomorrow
    assert slots[0].available is True          # manual entry invisible to the pool
    assert slots[0].taken_by_project_id is None


def test_taken_slot_reports_project_and_title(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    _save_scheduled_project("slotproj", acc, "tiktok", tomorrow, title="Naruto")

    slots = SchedulingService.find_free_slots_after(
        acc, "tiktok", tomorrow - timedelta(minutes=1), 1
    )
    assert slots[0].available is False
    assert slots[0].taken_by_project_id == "slotproj"
    assert slots[0].taken_by_title == "Naruto"


def test_cascade_skips_manual_entries(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SchedulingService, "_pool_is_busy_uploading", classmethod(lambda cls, a, p: (False, None))
    )
    monkeypatch.setattr(
        SchedulingService,
        "_platforms_for_project",
        classmethod(lambda cls, pid, aid: ["tiktok"]),
    )
    # Place the manual project exactly ON the cascade anchor slot, so the
    # test fails if manual entries are ever visible to the cascade walk.
    anchor = SchedulingService._earliest_slot_at_or_after(
        acc, "tiktok", datetime.now(timezone.utc) + timedelta(minutes=30)
    )
    _save_scheduled_project("manualproj", acc, "tiktok", anchor, manual=True)

    result = SchedulingService.compute_cascade("newproj", acc)
    tt = next(p for p in result.per_platform if p.platform == "tiktok")
    assert tt.target_slot == anchor
    # the manual project is NOT displaced even though it sits on the anchor slot
    assert tt.displaced == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/test_scheduling_service.py -v -k "manual or taken_slot_reports"`
Expected: FAIL — `PlatformSchedule.__init__() got an unexpected keyword argument 'manual'` / `taken_by_title` attribute error.

- [ ] **Step 3: Implement**

In `backend/app/models/project.py`:

```python
class PlatformSchedule(BaseModel):
    """Per-platform slot reservation on a Project."""

    slot: datetime
    scheduled_at: datetime
    manual: bool = False
```

In `backend/app/services/scheduling_service.py` — `FreeSlot` gains a title:

```python
@dataclass
class FreeSlot:
    slot: datetime
    available: bool
    taken_by_project_id: str | None = None
    taken_by_title: str | None = None
```

Replace `_collect_pool_reservations` (values become Projects; manual skipped — THE single exclusion point):

```python
    @classmethod
    def _collect_pool_reservations(
        cls, pool_key: str, platform: str
    ) -> dict[str, "Project"]:
        """Return {slot_iso: Project} for the given pool/platform.

        Manual reservations are invisible to the pool: they neither block
        slots nor get displaced. This is the single exclusion point.
        """
        account_pool_keys: dict[str, str] = {}
        for acc_id, acc in AccountService.all_accounts().items():
            account_pool_keys[acc_id] = (
                acc.pool_key_for(platform) or f"account:{acc_id}:{platform}"
            )

        reservations: dict[str, Project] = {}
        for project in ProjectService.list_all():
            schedules = project.platform_schedules or {}
            sched = schedules.get(platform)
            if sched is None or sched.manual:
                continue
            owner_id = project.scheduled_account_id
            if not owner_id:
                continue
            if account_pool_keys.get(owner_id) == pool_key:
                slot_iso = cls._normalize_utc_datetime(sched.slot).isoformat()
                reservations[slot_iso] = project
        return reservations
```

`_collect_reserved_slots_for_pool` is unchanged (already `set(keys)`).

In `find_free_slots_after`, the taker is now a Project:

```python
                taker = reservations.get(slot_iso)
                results.append(
                    FreeSlot(
                        slot=slot_dt,
                        available=taker is None,
                        taken_by_project_id=taker.id if taker else None,
                        taken_by_title=(taker.anime_name or taker.id) if taker else None,
                    )
                )
```

In `compute_cascade`, delete the inline `account_pool_keys`/`slot_to_project` block (lines ~767-782) and replace with:

```python
            slot_to_project = cls._collect_pool_reservations(pool_key, platform)
```

(The `reservations = cls._collect_pool_reservations(...)` line just above it becomes redundant — remove it; `slot_to_project` serves both uses.)

- [ ] **Step 4: Run the full scheduling test files**

Run: `pixi run test tests/test_scheduling_service.py tests/test_scheduling_v2_service.py tests/test_scheduling_routes.py -v`
Expected: ALL PASS (new tests + no regression from the return-type change).

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/project.py backend/app/services/scheduling_service.py backend/tests/test_scheduling_service.py
git commit -m "feat(scheduling): manual flag on PlatformSchedule, excluded from pool at single point"
```

---

### Task 2: `SchedulingService.reserve_manual`

**Files:**
- Modify: `backend/app/services/scheduling_service.py` (add method after `reserve_anchor`)
- Test: `backend/tests/test_scheduling_service.py` (append)

**Interfaces:**
- Consumes: `_setup_single_account` helper (Task 1), `PlatformSchedule.manual`.
- Produces: `SchedulingService.reserve_manual(project_id: str, account_id: str, at: datetime, platforms: list[str]) -> dict[str, PlatformSchedule]` — raises `ValueError("Project not found")` / `ValueError("slot_too_close")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_reserve_manual_writes_exact_time_no_jitter(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.save(Project(id="p1", anime_name="Bleach"))
    at = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)

    schedules = SchedulingService.reserve_manual("p1", acc, at, ["tiktok", "youtube"])

    assert set(schedules) == {"tiktok", "youtube"}
    for sched in schedules.values():
        assert sched.manual is True
        assert sched.slot == at
        assert sched.scheduled_at == at          # exact, no jitter
    saved = ProjectService.load("p1")
    assert saved.scheduled_account_id == acc
    assert saved.platform_schedules["tiktok"].manual is True


def test_reserve_manual_rejects_too_close(tmp_path, monkeypatch):
    from datetime import timedelta
    import pytest
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.save(Project(id="p1"))
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    with pytest.raises(ValueError, match="slot_too_close"):
        SchedulingService.reserve_manual("p1", acc, at, ["tiktok"])


def test_reserve_manual_overwrites_previous_manual(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.save(Project(id="p1"))
    at1 = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)
    at2 = at1 + timedelta(hours=3)
    SchedulingService.reserve_manual("p1", acc, at1, ["tiktok"])
    SchedulingService.reserve_manual("p1", acc, at2, ["tiktok"])
    assert ProjectService.load("p1").platform_schedules["tiktok"].slot == at2
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run test tests/test_scheduling_service.py -v -k reserve_manual`
Expected: FAIL — `AttributeError: ... has no attribute 'reserve_manual'`.

- [ ] **Step 3: Implement** (in `scheduling_service.py`, after `reserve_anchor`)

```python
    @classmethod
    def reserve_manual(
        cls,
        project_id: str,
        account_id: str,
        at: datetime,
        platforms: list[str],
    ) -> dict[str, PlatformSchedule]:
        """Reserve an exact user-chosen time OUTSIDE the slot system.

        No slot-config check, no pool check, no jitter. Overwrites any
        existing entries for the given platforms (that's also the edit path).
        """
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")
            at_utc = cls._normalize_utc_datetime(at)
            if at_utc < cls._earliest_allowed_publish_time():
                raise ValueError("slot_too_close")
            if project.scheduled_account_id and project.scheduled_account_id != account_id:
                project.platform_schedules = {}
            schedules = dict(project.platform_schedules or {})
            for platform in platforms:
                schedules[platform] = PlatformSchedule(
                    slot=at_utc, scheduled_at=at_utc, manual=True
                )
            project.platform_schedules = schedules
            project.scheduled_account_id = account_id
            cls._recompute_aggregates(project)
            ProjectService.save(project)
            return dict(schedules)
```

- [ ] **Step 4: Run tests**

Run: `pixi run test tests/test_scheduling_service.py -v -k reserve_manual`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_service.py
git commit -m "feat(scheduling): reserve_manual - exact-time reservations outside the slot pool"
```

---

### Task 3: reserve-manual route + `manual`/`taken_by_title` in API payloads

**Files:**
- Modify: `backend/app/api/routes/scheduling.py` (`PlanningEvent`, `FreeSlotResponse`, `_platform_schedules_to_dict`, `_build_planning_event`, `free_slots`, new route)
- Test: `backend/tests/test_scheduling_routes.py` (append)

**Interfaces:**
- Consumes: `SchedulingService.reserve_manual` (Task 2), `FreeSlot.taken_by_title` (Task 1), `_notify_displaced` (existing, `scheduling.py:187`), `_platforms_to_reserve(account, requested_platforms)` from `project_upload_service`.
- Produces:
  - `POST /scheduling/projects/{id}/reserve-manual` body `{account_id, at, platforms?}` → `{"platform_schedules": {...incl. manual}, "notification_status": {platform: str}}`; 422 `slot_too_close`, 404s.
  - `PlanningEvent.manual: bool`, `FreeSlotResponse.taken_by_title: str | None`, `_platform_schedules_to_dict` entries gain `"manual": bool`.

- [ ] **Step 1: Write the failing tests.** The file's existing pattern: a `client` fixture (account `acc_a`, tiktok slots 12/14/18) with `scheduling_service.datetime` frozen at `_NOW = 2026-05-07 12:00 UTC`. IMPORTANT: the routes module's `datetime` is NOT frozen, so `/events` must be queried with an explicit `range_start` or frozen-time events get filtered as past. Append:

```python
from datetime import timedelta


def _mk_project(pid: str, **kwargs) -> Project:
    p = Project(id=pid, **kwargs)
    ProjectService.get_project_dir(p.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(p)
    return p


def test_reserve_manual_route_and_planning_flag(client):
    _mk_project("p1", anime_name="Show")
    at = _NOW + timedelta(hours=3)
    resp = client.post(
        "/api/scheduling/projects/p1/reserve-manual",
        json={"account_id": "acc_a", "at": at.isoformat(), "platforms": ["tiktok"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_schedules"]["tiktok"]["manual"] is True
    assert body["platform_schedules"]["tiktok"]["slot"] == at.isoformat()
    assert "notification_status" in body

    events = client.get(
        "/api/scheduling/events", params={"range_start": _NOW.isoformat()}
    ).json()["events"]
    ev = next(e for e in events if e["project_id"] == "p1")
    assert ev["manual"] is True


def test_reserve_manual_route_rejects_too_close(client):
    _mk_project("p1")
    at = _NOW + timedelta(minutes=5)
    resp = client.post(
        "/api/scheduling/projects/p1/reserve-manual",
        json={"account_id": "acc_a", "at": at.isoformat(), "platforms": ["tiktok"]},
    )
    assert resp.status_code == 422
    assert "slot_too_close" in resp.text
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run test tests/test_scheduling_routes.py -v -k reserve_manual`
Expected: FAIL with 404 (route doesn't exist).

- [ ] **Step 3: Implement** in `backend/app/api/routes/scheduling.py`:

`PlanningEvent` gains `manual: bool = False`; `FreeSlotResponse` gains `taken_by_title: str | None = None`.

`_build_planning_event`: add `manual=sched.manual,` to the constructor call.

`free_slots` endpoint: add `taken_by_title=s.taken_by_title,` to `FreeSlotResponse(...)`.

`_platform_schedules_to_dict`:

```python
def _platform_schedules_to_dict(schedules):
    return {
        p: {
            "slot": s.slot.isoformat(),
            "scheduled_at": s.scheduled_at.isoformat(),
            "manual": s.manual,
        }
        for p, s in schedules.items()
    }
```

New route (after `reserve_anchor`):

```python
class ReserveManualRequest(BaseModel):
    account_id: str
    at: datetime
    platforms: list[Platform] | None = None


@router.post("/projects/{project_id}/reserve-manual")
async def reserve_manual(project_id: str, req: ReserveManualRequest):
    platforms = list(req.platforms) if req.platforms else None
    if not platforms:
        from ...services.project_upload_service import _platforms_to_reserve  # noqa: PLC0415
        account = AccountService.get_account(req.account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
        platforms = _platforms_to_reserve(account, requested_platforms=None)
    try:
        schedules = await asyncio.to_thread(
            SchedulingService.reserve_manual,
            project_id, req.account_id, req.at, platforms,
        )
    except ValueError as exc:
        msg = str(exc)
        if "slot_too_close" in msg:
            raise HTTPException(422, "slot_too_close")
        if "Project not found" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(422, msg)

    # Editing an already-uploaded manual schedule must reach the platforms
    # (YT publishAt, FB scheduled_publish_time, VPS reminder). For not-yet
    # uploaded projects every notify is a cheap skip.
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

- [ ] **Step 4: Run tests**

Run: `pixi run test tests/test_scheduling_routes.py -v`
Expected: ALL PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/scheduling.py backend/tests/test_scheduling_routes.py
git commit -m "feat(api): reserve-manual endpoint + manual/taken_by_title in scheduling payloads"
```

---

### Task 4: `compute_switch` — both plans + blockers + uploaded_count

**Files:**
- Modify: `backend/app/services/scheduling_service.py` (new dataclasses near top; method in the cascade section)
- Test: `backend/tests/test_scheduling_service.py` (append)

**Interfaces:**
- Consumes: `_collect_pool_reservations` (Task 1), existing `_next_slot_after`, `_is_slot_in_account_config`, `_pool_is_busy_uploading`, `_project_requires_platform_notification`, `DisplacedItem`, `CascadeBlocker`.
- Produces:

```python
@dataclass
class SwitchPlan:
    mode: str                      # "cascade" | "next_free"
    displaced: list[DisplacedItem]
    blockers: list[CascadeBlocker]

@dataclass
class SwitchResult:
    platform: str
    slot: datetime
    occupant_project_id: str | None
    occupant_title: str | None
    cascade: SwitchPlan
    next_free: SwitchPlan
    uploaded_count: int            # displaced-with-uploaded-video count (cascade plan)
```
  and `SchedulingService.compute_switch(project_id, account_id, platform, slot) -> SwitchResult` (pure read).

- [ ] **Step 1: Write the failing tests**

```python
def _future_slot(days, hour):
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _patch_pool_not_busy(monkeypatch):
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (False, None)),
    )


def test_compute_switch_chain_and_next_free(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)   # slots 10/14/18
    _patch_pool_not_busy(monkeypatch)
    s10, s14, s18 = _future_slot(1, 10), _future_slot(1, 14), _future_slot(1, 18)
    _save_scheduled_project("projB", acc, "tiktok", s10, title="B")
    _save_scheduled_project("projC", acc, "tiktok", s14, title="C")
    # 18:00 free

    result = SchedulingService.compute_switch("newproj", acc, "tiktok", s10)

    assert result.occupant_project_id == "projB"
    assert result.occupant_title == "B"
    # cascade: B -> 14 pushes C -> 18
    assert [(d.project_id, d.from_slot, d.to_slot) for d in result.cascade.displaced] == [
        ("projB", s10, s14),
        ("projC", s14, s18),
    ]
    assert result.cascade.blockers == []
    # next_free: B jumps over taken 14 straight to 18
    assert [(d.project_id, d.to_slot) for d in result.next_free.displaced] == [
        ("projB", s18)
    ]
    assert result.next_free.blockers == []
    assert result.uploaded_count == 0


def test_compute_switch_skips_own_reservation_and_manual(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14 = _future_slot(1, 10), _future_slot(1, 14)
    _save_scheduled_project("projB", acc, "tiktok", s10, title="B")
    _save_scheduled_project("me", acc, "tiktok", s14, title="Me")          # my own old slot
    _save_scheduled_project("manualp", acc, "tiktok", _future_slot(1, 18), manual=True)

    result = SchedulingService.compute_switch("me", acc, "tiktok", s10)
    # my own 14:00 counts as free (it's released by the switch), so B lands there
    assert [(d.project_id, d.to_slot) for d in result.cascade.displaced] == [("projB", s14)]
    assert result.next_free.displaced[0].to_slot == s14


def test_compute_switch_free_slot_has_no_occupant(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    result = SchedulingService.compute_switch("newproj", acc, "tiktok", _future_slot(1, 10))
    assert result.occupant_project_id is None
    assert result.cascade.displaced == [] and result.next_free.displaced == []


def test_compute_switch_pool_busy_blocks_both_plans(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (True, "busyproj")),
    )
    _save_scheduled_project("projB", acc, "tiktok", _future_slot(1, 10))
    result = SchedulingService.compute_switch("newproj", acc, "tiktok", _future_slot(1, 10))
    assert any(b.reason == "pool_busy" for b in result.cascade.blockers)
    assert any(b.reason == "pool_busy" for b in result.next_free.blockers)
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run test tests/test_scheduling_service.py -v -k compute_switch`
Expected: FAIL — no attribute `compute_switch`.

- [ ] **Step 3: Implement.** Dataclasses after `CascadeResult` (~line 66):

```python
@dataclass
class SwitchPlan:
    mode: str
    displaced: list[DisplacedItem]
    blockers: list[CascadeBlocker]


@dataclass
class SwitchResult:
    platform: str
    slot: datetime
    occupant_project_id: str | None
    occupant_title: str | None
    cascade: SwitchPlan
    next_free: SwitchPlan
    uploaded_count: int
```

Method (in the cascade section, after `compute_cascade`):

```python
    @classmethod
    def compute_switch(
        cls, project_id: str, account_id: str, platform: str, slot: datetime,
    ) -> SwitchResult:
        """Pure simulation of stealing `slot` on `platform` for `project_id`.

        Returns BOTH displacement plans (chain cascade / next-free-slot);
        the user picks one at apply time.
        """
        slot_utc = cls._normalize_utc_datetime(slot)
        now_utc = datetime.now(timezone.utc)
        earliest_allowed = cls._earliest_allowed_publish_time()
        fb_horizon = now_utc + timedelta(days=29)

        pool_key = cls._resolve_pool_key(account_id, platform)
        occupancy = {
            iso: proj
            for iso, proj in cls._collect_pool_reservations(pool_key, platform).items()
            if proj.id != project_id  # our own old slot frees up as part of the switch
        }
        occupant = occupancy.get(slot_utc.isoformat())

        shared: list[CascadeBlocker] = []
        if not cls._is_slot_in_account_config(account_id, platform, slot_utc):
            shared.append(CascadeBlocker(platform, "slot_not_configured"))
        if slot_utc < earliest_allowed:
            shared.append(CascadeBlocker(platform, "slot_too_close"))
        busy, _busy_pid = cls._pool_is_busy_uploading(account_id, platform)
        if busy:
            shared.append(CascadeBlocker(platform, "pool_busy"))

        cascade = SwitchPlan(mode="cascade", displaced=[], blockers=list(shared))
        next_free = SwitchPlan(mode="next_free", displaced=[], blockers=list(shared))

        if occupant is not None and not shared:
            # Chain: each occupant pushes into the next configured slot.
            current_slot, current_occ = slot_utc, occupant
            while current_occ is not None:
                nxt = cls._next_slot_after(account_id, platform, current_slot)
                if nxt is None:
                    cascade.blockers.append(CascadeBlocker(platform, "pool_full"))
                    break
                if platform == "facebook" and nxt > fb_horizon:
                    cascade.blockers.append(
                        CascadeBlocker(platform, "facebook_horizon_exceeded")
                    )
                    break
                cascade.displaced.append(DisplacedItem(
                    project_id=current_occ.id,
                    anime_title=current_occ.anime_name or current_occ.id,
                    from_slot=current_slot,
                    to_slot=nxt,
                    requires_platform_notification=(
                        cls._project_requires_platform_notification(current_occ, platform)
                    ),
                ))
                current_slot = nxt
                current_occ = occupancy.get(nxt.isoformat())

            # Next-free: the occupant alone jumps over taken slots.
            landing = cls._next_slot_after(account_id, platform, slot_utc)
            while landing is not None and landing.isoformat() in occupancy:
                landing = cls._next_slot_after(account_id, platform, landing)
            if landing is None:
                next_free.blockers.append(CascadeBlocker(platform, "pool_full"))
            elif platform == "facebook" and landing > fb_horizon:
                next_free.blockers.append(
                    CascadeBlocker(platform, "facebook_horizon_exceeded")
                )
            else:
                next_free.displaced.append(DisplacedItem(
                    project_id=occupant.id,
                    anime_title=occupant.anime_name or occupant.id,
                    from_slot=slot_utc,
                    to_slot=landing,
                    requires_platform_notification=(
                        cls._project_requires_platform_notification(occupant, platform)
                    ),
                ))

        uploaded_count = sum(
            1 for d in cascade.displaced if d.requires_platform_notification
        )
        return SwitchResult(
            platform=platform,
            slot=slot_utc,
            occupant_project_id=occupant.id if occupant else None,
            occupant_title=(occupant.anime_name or occupant.id) if occupant else None,
            cascade=cascade,
            next_free=next_free,
            uploaded_count=uploaded_count,
        )
```

- [ ] **Step 4: Run tests**

Run: `pixi run test tests/test_scheduling_service.py -v -k compute_switch`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_service.py
git commit -m "feat(scheduling): compute_switch with cascade and next-free displacement plans"
```

---

### Task 5: `apply_switch` + `_apply_displacements`

**Files:**
- Modify: `backend/app/services/scheduling_service.py`
- Test: `backend/tests/test_scheduling_service.py` (append)

**Interfaces:**
- Consumes: `compute_switch` (Task 4), `_reservation_lock`, `_randomize_slot`, `_recompute_aggregates`.
- Produces:
  - `SchedulingService._apply_displacements(platform: str, displaced: list[DisplacedItem], now_utc: datetime) -> None` (writes farthest-first; caller must hold the lock)
  - `SchedulingService.apply_switch(project_id, account_id, platform, slot, mode: str, expected_occupant_id: str | None) -> SwitchResult` — raises `ValueError("slot_state_changed")` on occupant mismatch, `ValueError("Switch blocked: ...")` on plan blockers.

- [ ] **Step 1: Write the failing tests**

```python
def test_apply_switch_cascade_moves_chain_and_reserves(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14, s18 = _future_slot(1, 10), _future_slot(1, 14), _future_slot(1, 18)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    _save_scheduled_project("projC", acc, "tiktok", s14)
    ProjectService.save(Project(id="me"))

    SchedulingService.apply_switch("me", acc, "tiktok", s10, "cascade", "projB")

    assert ProjectService.load("me").platform_schedules["tiktok"].slot == s10
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s14
    assert ProjectService.load("projC").platform_schedules["tiktok"].slot == s18


def test_apply_switch_next_free_moves_only_occupant(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14, s18 = _future_slot(1, 10), _future_slot(1, 14), _future_slot(1, 18)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    _save_scheduled_project("projC", acc, "tiktok", s14)
    ProjectService.save(Project(id="me"))

    SchedulingService.apply_switch("me", acc, "tiktok", s10, "next_free", "projB")

    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s18
    assert ProjectService.load("projC").platform_schedules["tiktok"].slot == s14  # untouched


def test_apply_switch_stale_occupant_raises(tmp_path, monkeypatch):
    import pytest
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10 = _future_slot(1, 10)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    ProjectService.save(Project(id="me"))
    with pytest.raises(ValueError, match="slot_state_changed"):
        SchedulingService.apply_switch("me", acc, "tiktok", s10, "cascade", "someoneelse")
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run test tests/test_scheduling_service.py -v -k apply_switch`
Expected: FAIL — no attribute `apply_switch`.

- [ ] **Step 3: Implement** (after `compute_switch`):

```python
    @classmethod
    def _apply_displacements(
        cls, platform: str, displaced: list[DisplacedItem], now_utc: datetime
    ) -> None:
        """Persist displacement moves farthest-first. Caller holds the lock."""
        for item in reversed(displaced):
            project = ProjectService.load(item.project_id)
            if project is None:
                continue
            schedules = dict(project.platform_schedules or {})
            schedules[platform] = PlatformSchedule(
                slot=item.to_slot,
                scheduled_at=cls._randomize_slot(item.to_slot, now_utc),
            )
            project.platform_schedules = schedules
            cls._recompute_aggregates(project)
            ProjectService.save(project)

    @classmethod
    def apply_switch(
        cls,
        project_id: str,
        account_id: str,
        platform: str,
        slot: datetime,
        mode: str,
        expected_occupant_id: str | None,
    ) -> SwitchResult:
        """Steal `slot` on `platform`: displace the occupant per `mode`."""
        with cls._reservation_lock:
            result = cls.compute_switch(project_id, account_id, platform, slot)
            if (result.occupant_project_id or None) != (expected_occupant_id or None):
                raise ValueError("slot_state_changed")
            plan = result.cascade if mode == "cascade" else result.next_free
            if plan.blockers:
                summary = ", ".join(f"{b.platform}:{b.reason}" for b in plan.blockers)
                raise ValueError(f"Switch blocked: {summary}")

            now_utc = datetime.now(timezone.utc)
            cls._apply_displacements(platform, plan.displaced, now_utc)

            switcher = ProjectService.load(project_id)
            if switcher is None:
                raise ValueError("Project not found")
            if switcher.scheduled_account_id and switcher.scheduled_account_id != account_id:
                switcher.platform_schedules = {}
            schedules = dict(switcher.platform_schedules or {})
            slot_utc = cls._normalize_utc_datetime(slot)
            schedules[platform] = PlatformSchedule(
                slot=slot_utc, scheduled_at=cls._randomize_slot(slot_utc, now_utc)
            )
            switcher.platform_schedules = schedules
            switcher.scheduled_account_id = account_id
            cls._recompute_aggregates(switcher)
            ProjectService.save(switcher)
            return result
```

- [ ] **Step 4: Run tests**

Run: `pixi run test tests/test_scheduling_service.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/tests/test_scheduling_service.py
git commit -m "feat(scheduling): apply_switch with occupant re-verification and farthest-first writes"
```

---

### Task 6: switch-preview / switch-apply routes + displaced notifications

**Files:**
- Modify: `backend/app/api/routes/scheduling.py` (after the cascade routes)
- Test: `backend/tests/test_scheduling_routes.py` (append)

**Interfaces:**
- Consumes: `compute_switch` / `apply_switch` (Tasks 4-5), `_notify_displaced` (existing).
- Produces:
  - `POST /scheduling/projects/{id}/switch-preview` `{account_id, platform, slot}` → payload below.
  - `POST /scheduling/projects/{id}/switch-apply` `{account_id, platform, slot, mode, expected_occupant_id}` → same payload + `"notification_status": {project_id: str}`. 409 on `slot_state_changed`/`pool_busy`, 422 otherwise.
  - Payload shape (used verbatim by frontend Task 8):

```json
{
  "platform": "tiktok",
  "slot": "2026-07-06T10:00:00+00:00",
  "occupant_project_id": "abc",
  "occupant_title": "Naruto",
  "uploaded_count": 1,
  "cascade":   {"displaced": [{"project_id","anime_title","from_slot","to_slot","requires_platform_notification"}], "blockers": [{"platform","reason"}]},
  "next_free": {"displaced": [...], "blockers": [...]}
}
```

- [ ] **Step 1: Write the failing tests** (uses `_mk_project` from Task 3; `acc_a` has tiktok slots 12/14/18, frozen `_NOW` = 2026-05-07 12:00 UTC):

```python
def _seed_pool_b_c():
    """projB @ 2026-05-08 12:00, projC @ 14:00, 18:00 free, plus 'me'."""
    slot1 = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    slot2 = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    _mk_project("projB", anime_name="B", scheduled_account_id="acc_a",
        platform_schedules={"tiktok": PlatformSchedule(slot=slot1, scheduled_at=slot1)})
    _mk_project("projC", anime_name="C", scheduled_account_id="acc_a",
        platform_schedules={"tiktok": PlatformSchedule(slot=slot2, scheduled_at=slot2)})
    _mk_project("me")
    return slot1


def test_switch_preview_and_apply(client):
    slot1 = _seed_pool_b_c()
    resp = client.post(
        "/api/scheduling/projects/me/switch-preview",
        json={"account_id": "acc_a", "platform": "tiktok", "slot": slot1.isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["occupant_project_id"] == "projB"
    assert len(body["cascade"]["displaced"]) == 2      # B->14 pushes C->18
    assert len(body["next_free"]["displaced"]) == 1    # B jumps to 18

    resp = client.post(
        "/api/scheduling/projects/me/switch-apply",
        json={
            "account_id": "acc_a", "platform": "tiktok", "slot": slot1.isoformat(),
            "mode": "next_free", "expected_occupant_id": "projB",
        },
    )
    assert resp.status_code == 200
    assert "projB" in resp.json()["notification_status"]
    assert ProjectService.load("me").platform_schedules["tiktok"].slot == slot1


def test_switch_apply_stale_occupant_409(client):
    slot1 = _seed_pool_b_c()
    resp = client.post(
        "/api/scheduling/projects/me/switch-apply",
        json={
            "account_id": "acc_a", "platform": "tiktok", "slot": slot1.isoformat(),
            "mode": "cascade", "expected_occupant_id": "wrong",
        },
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run test tests/test_scheduling_routes.py -v -k switch`
Expected: FAIL with 404.

- [ ] **Step 3: Implement:**

```python
class SwitchPreviewRequest(BaseModel):
    account_id: str
    platform: Platform
    slot: datetime


class SwitchApplyRequest(SwitchPreviewRequest):
    mode: Literal["cascade", "next_free"]
    expected_occupant_id: str | None = None


def _switch_plan_payload(plan) -> dict:
    return {
        "displaced": [
            {
                "project_id": d.project_id,
                "anime_title": d.anime_title,
                "from_slot": d.from_slot.isoformat(),
                "to_slot": d.to_slot.isoformat(),
                "requires_platform_notification": d.requires_platform_notification,
            }
            for d in plan.displaced
        ],
        "blockers": [{"platform": b.platform, "reason": b.reason} for b in plan.blockers],
    }


def _switch_to_payload(result) -> dict:
    return {
        "platform": result.platform,
        "slot": result.slot.isoformat(),
        "occupant_project_id": result.occupant_project_id,
        "occupant_title": result.occupant_title,
        "uploaded_count": result.uploaded_count,
        "cascade": _switch_plan_payload(result.cascade),
        "next_free": _switch_plan_payload(result.next_free),
    }


@router.post("/projects/{project_id}/switch-preview")
async def switch_preview(project_id: str, req: SwitchPreviewRequest):
    result = await asyncio.to_thread(
        SchedulingService.compute_switch,
        project_id, req.account_id, req.platform, req.slot,
    )
    return _switch_to_payload(result)


@router.post("/projects/{project_id}/switch-apply")
async def switch_apply(project_id: str, req: SwitchApplyRequest):
    try:
        result = await asyncio.to_thread(
            SchedulingService.apply_switch,
            project_id, req.account_id, req.platform, req.slot,
            req.mode, req.expected_occupant_id,
        )
    except ValueError as exc:
        msg = str(exc)
        if "slot_state_changed" in msg or "pool_busy" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(422, msg)

    plan = result.cascade if req.mode == "cascade" else result.next_free
    notification_status: dict[str, str] = {}
    for displaced in plan.displaced:
        moved = ProjectService.load(displaced.project_id)
        if moved is None:
            continue
        sched = (moved.platform_schedules or {}).get(req.platform)
        if sched is None:
            continue
        notification_status[displaced.project_id] = await asyncio.to_thread(
            _notify_displaced, displaced.project_id, req.platform, sched.scheduled_at
        )
    payload = _switch_to_payload(result)
    payload["notification_status"] = notification_status
    return payload
```

- [ ] **Step 4: Run tests**

Run: `pixi run test tests/test_scheduling_routes.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/scheduling.py backend/tests/test_scheduling_routes.py
git commit -m "feat(api): switch-preview and switch-apply endpoints with displaced notifications"
```

---

### Task 7: `steals` on reserve/reschedule-anchor (atomic anchor steal)

**Files:**
- Modify: `backend/app/services/scheduling_service.py` (`reserve_anchor`, `reschedule_anchor`, new `StealSpec` dataclass)
- Modify: `backend/app/api/routes/scheduling.py` (`ReserveAnchorRequest`, `PatchAnchorRequest`, both endpoints)
- Test: `backend/tests/test_scheduling_service.py`, `backend/tests/test_scheduling_routes.py` (append)

**Interfaces:**
- Consumes: `compute_switch`, `_apply_displacements` (Tasks 4-5).
- Produces:

```python
@dataclass
class StealSpec:
    mode: str                       # "cascade" | "next_free"
    expected_occupant_id: str | None
```
  - `reserve_anchor(project_id, account_id, tiktok_slot, overrides=None, steals: dict[str, StealSpec] | None = None) -> tuple[dict[str, PlatformSchedule], dict[str, SwitchResult]]` — **return type changes** to `(schedules, applied_switches)`; all callers updated in this task.
  - `reschedule_anchor(..., steals=...)` passes through, same tuple return.
  - Route request models gain `steals: dict[str, StealSpecModel] | None`; responses gain `"notification_status"` entries for displaced projects.

- [ ] **Step 1: Write the failing service test**

```python
def test_reserve_anchor_with_steal_is_atomic(tmp_path, monkeypatch):
    from app.services.scheduling_service import StealSpec
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14 = _future_slot(1, 10), _future_slot(1, 14)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    ProjectService.save(Project(id="me"))

    schedules, switches = SchedulingService.reserve_anchor(
        "me", acc, s10,
        steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="projB")},
    )

    assert schedules["tiktok"].slot == s10
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s14
    assert switches["tiktok"].occupant_project_id == "projB"


def test_reserve_anchor_steal_stale_occupant_writes_nothing(tmp_path, monkeypatch):
    import pytest
    from app.services.scheduling_service import StealSpec
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10 = _future_slot(1, 10)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    ProjectService.save(Project(id="me"))

    with pytest.raises(ValueError, match="slot_state_changed"):
        SchedulingService.reserve_anchor(
            "me", acc, s10,
            steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="wrong")},
        )
    # nothing moved, nothing reserved
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s10
    assert not (ProjectService.load("me").platform_schedules or {})
```

And a route-level test (append to `test_scheduling_routes.py`, reusing `_mk_project`/`_seed_pool_b_c` from Tasks 3/6):

```python
def test_reserve_anchor_with_steals_route(client):
    slot1 = _seed_pool_b_c()
    resp = client.post(
        "/api/scheduling/projects/me/reserve-anchor",
        json={
            "account_id": "acc_a",
            "tiktok_slot": slot1.isoformat(),
            "steals": {
                "tiktok": {"mode": "cascade", "expected_occupant_id": "projB"}
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_schedules"]["tiktok"]["slot"] == slot1.isoformat()
    assert "projB" in body["notification_status"]["tiktok"]
    # stale occupant -> 409, nothing moved
    resp = client.post(
        "/api/scheduling/projects/projC/reserve-anchor",
        json={
            "account_id": "acc_a",
            "tiktok_slot": slot1.isoformat(),
            "steals": {"tiktok": {"mode": "cascade", "expected_occupant_id": "wrong"}},
        },
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run test tests/test_scheduling_service.py -v -k steal`
Expected: FAIL — `ImportError: cannot import name 'StealSpec'`.

- [ ] **Step 3: Implement.** Dataclass after `SwitchResult`:

```python
@dataclass
class StealSpec:
    mode: str
    expected_occupant_id: str | None
```

In `reserve_anchor`, change the signature and insert the steal phase between the account-drop and `resolve_anchor` (all inside the existing `with cls._reservation_lock:`); the reuse path and final return also change to the tuple form:

```python
    @classmethod
    def reserve_anchor(
        cls,
        project_id: str,
        account_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
        steals: dict[str, StealSpec] | None = None,
    ) -> tuple[dict[str, PlatformSchedule], dict[str, SwitchResult]]:
```

… reuse path returns `return dict(project.platform_schedules), {}` … then before `resolution = cls.resolve_anchor(...)`:

```python
            # Steal phase: validate EVERY steal before writing ANY move, so a
            # stale occupant or blocker aborts with zero side effects.
            applied_switches: dict[str, SwitchResult] = {}
            steal_plans: list[tuple[str, SwitchPlan]] = []
            for platform, spec in (steals or {}).items():
                steal_slot = anchor if platform == "tiktok" else (overrides or {}).get(platform)
                if steal_slot is None:
                    raise ValueError(f"steal for {platform} requires an override slot")
                result = cls.compute_switch(project_id, account_id, platform, steal_slot)
                if (result.occupant_project_id or None) != (spec.expected_occupant_id or None):
                    raise ValueError("slot_state_changed")
                plan = result.cascade if spec.mode == "cascade" else result.next_free
                if plan.blockers:
                    summary = ", ".join(f"{b.platform}:{b.reason}" for b in plan.blockers)
                    raise ValueError(f"Switch blocked: {summary}")
                steal_plans.append((platform, plan))
                applied_switches[platform] = result

            now_utc = datetime.now(timezone.utc)
            for platform, plan in steal_plans:
                cls._apply_displacements(platform, plan.displaced, now_utc)
```

Final return: `return dict(schedules), applied_switches`.

`reschedule_anchor` gains `steals: dict[str, StealSpec] | None = None` and forwards it; its return type becomes the same tuple (it just returns `reserve_anchor(...)`'s result).

Update ALL existing callers of both methods (`backend/app/api/routes/scheduling.py` endpoints `reserve_anchor`/`patch_anchor`, plus any `reserve_anchor(`/`reschedule_anchor(` call sites found via `grep -rn "reserve_anchor\|reschedule_anchor" backend/`) to unpack the tuple.

Route layer changes in `scheduling.py`:

```python
class StealSpecModel(BaseModel):
    mode: Literal["cascade", "next_free"]
    expected_occupant_id: str | None = None


class ReserveAnchorRequest(BaseModel):
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None
    steals: dict[str, StealSpecModel] | None = None


class PatchAnchorRequest(BaseModel):
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None
    steals: dict[str, StealSpecModel] | None = None
```

Both endpoints convert models to specs and notify displaced projects after success (same loop as switch_apply Step 3, iterating `applied_switches[platform]`'s chosen plan; the chosen plan is `result.cascade if req.steals[platform].mode == "cascade" else result.next_free`). Map `slot_state_changed` → 409 in both endpoints. Example for `reserve_anchor` endpoint:

```python
    steals = (
        {p: StealSpec(mode=s.mode, expected_occupant_id=s.expected_occupant_id)
         for p, s in req.steals.items()}
        if req.steals else None
    )
    try:
        schedules, switches = await asyncio.to_thread(
            SchedulingService.reserve_anchor,
            project_id, req.account_id, req.tiktok_slot, req.overrides, steals,
        )
    except ValueError as exc:
        msg = str(exc)
        if "slot_state_changed" in msg or "pool_busy" in msg:
            raise HTTPException(409, msg)
        if "tiktok" in msg and "slot_taken" in msg:
            raise HTTPException(409, "tiktok_slot_taken")
        if "slot_not_configured" in msg:
            raise HTTPException(422, "invalid_slot")
        if "Project not found" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(422, msg)

    notification_status: dict[str, dict[str, str]] = {}
    for platform, result in switches.items():
        spec = req.steals[platform]
        plan = result.cascade if spec.mode == "cascade" else result.next_free
        notification_status[platform] = {}
        for displaced in plan.displaced:
            moved = ProjectService.load(displaced.project_id)
            sched = (moved.platform_schedules or {}).get(platform) if moved else None
            if sched is None:
                continue
            notification_status[platform][displaced.project_id] = await asyncio.to_thread(
                _notify_displaced, displaced.project_id, platform, sched.scheduled_at
            )
    return {
        "platform_schedules": _platform_schedules_to_dict(schedules),
        "notification_status": notification_status,
    }
```

(`StealSpec` import: `from ...services.scheduling_service import SchedulingService, StealSpec`.)

- [ ] **Step 4: Run the whole backend suite**

Run: `pixi run test -v`
Expected: ALL PASS (tuple-return callers all updated).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduling_service.py backend/app/api/routes/scheduling.py backend/tests/
git commit -m "feat(scheduling): atomic steals in reserve/reschedule-anchor"
```

---

### Task 8: Frontend types + API client

**Files:**
- Modify: `frontend/src/types/index.ts:415-459`
- Modify: `frontend/src/api/client.ts:1130-1264`

**Interfaces:**
- Consumes: payload shapes from Tasks 3, 6, 7 (copied exactly).
- Produces (used by Tasks 10-13):

```ts
// types/index.ts additions
export type SwitchMode = "cascade" | "next_free";
export interface StealSpec { mode: SwitchMode; expected_occupant_id: string | null; }
export interface SwitchPlanDto {
  displaced: Array<{
    project_id: string; anime_title: string;
    from_slot: string; to_slot: string;
    requires_platform_notification: boolean;
  }>;
  blockers: Array<{ platform: Platform; reason: string }>;
}
export interface SwitchPreview {
  platform: Platform; slot: string;
  occupant_project_id: string | null;
  occupant_title: string | null;
  uploaded_count: number;
  cascade: SwitchPlanDto;
  next_free: SwitchPlanDto;
}
```
  plus `FreeSlot.taken_by_title?: string` and `PlanningEvent.manual: boolean`.

- [ ] **Step 1: Edit `types/index.ts`** — add `manual: boolean;` to `PlanningEvent` (after `status`), `taken_by_title?: string;` to `FreeSlot`, and append the block above after `ResolveAnchorResult`.

- [ ] **Step 2: Edit `api/client.ts`** — extend `reserveAnchor` and `rescheduleAnchor` payload types with `steals?: Partial<Record<import("@/types").Platform, import("@/types").StealSpec>>;`, and append before the closing `};`:

```ts
  async reserveManual(
    project_id: string,
    payload: {
      account_id: string;
      at: string;
      platforms?: import("@/types").Platform[];
    },
  ): Promise<{
    platform_schedules: Record<
      string,
      { slot: string; scheduled_at: string; manual: boolean }
    >;
    notification_status: Record<string, string>;
  }> {
    return request(`/scheduling/projects/${project_id}/reserve-manual`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async switchPreview(
    project_id: string,
    payload: {
      account_id: string;
      platform: import("@/types").Platform;
      slot: string;
    },
  ): Promise<import("@/types").SwitchPreview> {
    return request(`/scheduling/projects/${project_id}/switch-preview`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async switchApply(
    project_id: string,
    payload: {
      account_id: string;
      platform: import("@/types").Platform;
      slot: string;
      mode: import("@/types").SwitchMode;
      expected_occupant_id: string | null;
    },
  ): Promise<
    import("@/types").SwitchPreview & {
      notification_status: Record<string, string>;
    }
  > {
    return request(`/scheduling/projects/${project_id}/switch-apply`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts
git commit -m "feat(frontend): types + api client for manual scheduling and slot switching"
```

---

### Task 9: SlotChips — three states (free / impossible / switchable)

**Files:**
- Modify: `frontend/src/components/project-manager/SlotChips.tsx` (full rewrite below)

**Interfaces:**
- Consumes: `FreeSlot.taken_by_project_id`, `taken_by_title` (Task 8).
- Produces: props consumed by Task 11:

```ts
interface SlotChipsProps {
  slots: FreeSlot[];
  selectedIso: string | null;
  onSelect: (iso: string) => void;
  /** Called when the user clicks a slot taken by another project. */
  onSelectTaken?: (slot: FreeSlot) => void;
  /** ISO slots with a locally-pending steal (rendered amber-selected). */
  stolenIsos?: Set<string>;
  /** Project whose own slots should not be treated as stealable. */
  ownProjectId?: string;
}
```

- [ ] **Step 1: Rewrite the component**

```tsx
import type { FreeSlot } from "@/types";

interface SlotChipsProps {
  slots: FreeSlot[];
  selectedIso: string | null;
  onSelect: (iso: string) => void;
  onSelectTaken?: (slot: FreeSlot) => void;
  stolenIsos?: Set<string>;
  ownProjectId?: string;
}

const MIN_LEAD_MS = 30 * 60 * 1000;

function fmtTime(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function SlotChips({
  slots, selectedIso, onSelect, onSelectTaken, stolenIsos, ownProjectId,
}: SlotChipsProps) {
  if (!slots.length) {
    return (
      <div className="text-xs text-[hsl(var(--muted-foreground))] py-2">
        No slot configured this day
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {slots.map((s) => {
        const selected = s.slot === selectedIso;
        const impossible = new Date(s.slot).getTime() < Date.now() + MIN_LEAD_MS;
        const mine = !!ownProjectId && s.taken_by_project_id === ownProjectId;
        const stealable =
          !s.available && !impossible && !mine && !!onSelectTaken;
        const stolen = stealable && !!stolenIsos?.has(s.slot);
        const taken = !s.available && !mine;
        const disabled = impossible || (taken && !stealable);

        const cls = selected || stolen
          ? stolen
            ? "border-amber-500 text-amber-500 bg-amber-500/10"
            : "border-[hsl(var(--primary))] text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10"
          : impossible
            ? "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] line-through opacity-60 cursor-not-allowed"
            : stealable
              ? "border-amber-500/60 text-amber-500 hover:bg-amber-500/10"
              : taken
                ? "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] line-through opacity-60 cursor-not-allowed"
                : "border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]";

        return (
          <button
            key={s.slot}
            type="button"
            disabled={disabled}
            title={
              impossible
                ? "Trop proche / passé"
                : stealable
                  ? `Occupé par « ${s.taken_by_title ?? s.taken_by_project_id} » — cliquer pour échanger`
                  : undefined
            }
            onClick={() => {
              if (stealable) onSelectTaken!(s);
              else onSelect(s.slot);
            }}
            className={`text-xs px-2.5 py-1 rounded border transition-colors ${cls}`}
          >
            {fmtTime(s.slot)}
          </button>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Type check** — `cd frontend && npx tsc -b --noEmit`. Expected: no errors (existing call sites pass no new props — all optional).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/project-manager/SlotChips.tsx
git commit -m "feat(frontend): three-state slot chips - free, impossible, switchable (amber)"
```

---

### Task 10: SwitchSlotConfirmModal

**Files:**
- Create: `frontend/src/components/project-manager/SwitchSlotConfirmModal.tsx`

**Interfaces:**
- Consumes: `api.switchPreview` (Task 8), `SwitchPreview`, `SwitchMode`, `PLATFORM_SHORT`.
- Produces:

```ts
interface SwitchSlotConfirmModalProps {
  open: boolean;
  projectId: string;
  accountId: string;
  platform: Platform;
  slotIso: string;
  onClose: () => void;
  /** Parent decides: apply immediately (single-platform) or store a pending
   *  steal (anchor flow). */
  onChoose: (mode: SwitchMode, preview: SwitchPreview) => void | Promise<void>;
}
```

- [ ] **Step 1: Create the component**

```tsx
import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { ArrowLeftRight } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type { Platform, SwitchMode, SwitchPreview } from "@/types";
import { PLATFORM_SHORT } from "@/components/planning/platformColors";

interface SwitchSlotConfirmModalProps {
  open: boolean;
  projectId: string;
  accountId: string;
  platform: Platform;
  slotIso: string;
  onClose: () => void;
  onChoose: (mode: SwitchMode, preview: SwitchPreview) => void | Promise<void>;
}

function fmt(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function SwitchSlotConfirmModal({
  open, projectId, accountId, platform, slotIso, onClose, onChoose,
}: SwitchSlotConfirmModalProps) {
  const [preview, setPreview] = useState<SwitchPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState<SwitchMode | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setPreview(null); setError(null); setLoading(true);
    api.switchPreview(projectId, { account_id: accountId, platform, slot: slotIso })
      .then(setPreview)
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false));
  }, [open, projectId, accountId, platform, slotIso]);

  if (!open) return null;

  const cascadeBlocked = (preview?.cascade.blockers.length ?? 0) > 0;
  const nextFreeBlocked = (preview?.next_free.blockers.length ?? 0) > 0;
  const cascadeCount = preview?.cascade.displaced.length ?? 0;
  const ytQuotaWarning =
    platform === "youtube" && (preview?.uploaded_count ?? 0) > 10;

  const choose = async (mode: SwitchMode) => {
    if (!preview) return;
    setSubmitting(mode); setError(null);
    try {
      await onChoose(mode, preview);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <div className="fixed inset-0 z-[70] bg-black/55 flex items-center justify-center" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-5 w-[480px] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-2">
          <ArrowLeftRight className="h-5 w-5 text-amber-500" />
          <h3 className="text-sm font-semibold">
            Échanger le slot {PLATFORM_SHORT[platform]} · {fmt(slotIso)}
          </h3>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mb-3">
          Ce slot est occupé par «{preview?.occupant_title ?? "…"}». Choisissez
          comment le libérer.
        </p>

        {loading && <div className="text-xs">Calcul des déplacements…</div>}
        {error && <div className="text-xs text-[hsl(var(--destructive))] mb-2">{error}</div>}

        {preview && (
          <div className="space-y-3">
            <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
              <div className="text-[11px] font-semibold mb-1">
                Cascade en chaîne — {cascadeCount} vidéo{cascadeCount > 1 ? "s" : ""} déplacée{cascadeCount > 1 ? "s" : ""}
              </div>
              <div className="font-mono text-[11px] leading-relaxed text-[hsl(var(--muted-foreground))] max-h-32 overflow-y-auto">
                {preview.cascade.displaced.map((d) => (
                  <div key={d.project_id}>
                    ↳ {d.anime_title} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                ))}
                {preview.cascade.blockers.map((b, i) => (
                  <div key={i} className="text-[hsl(var(--destructive))]">✗ {b.reason}</div>
                ))}
              </div>
              {ytQuotaWarning && (
                <div className="text-[11px] text-amber-500 mt-1">
                  ⚠ {preview.uploaded_count} vidéos YouTube déjà uploadées seront
                  re-planifiées (~{preview.uploaded_count * 50} unités de quota API).
                </div>
              )}
            </div>

            <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
              <div className="text-[11px] font-semibold mb-1">Prochain slot libre — 1 vidéo déplacée</div>
              <div className="font-mono text-[11px] text-[hsl(var(--muted-foreground))]">
                {preview.next_free.displaced.map((d) => (
                  <div key={d.project_id}>
                    ↳ {d.anime_title} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                ))}
                {preview.next_free.blockers.map((b, i) => (
                  <div key={i} className="text-[hsl(var(--destructive))]">✗ {b.reason}</div>
                ))}
              </div>
            </div>
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="ghost" onClick={onClose}>Annuler</Button>
          <Button
            size="sm" variant="outline"
            disabled={!preview || nextFreeBlocked || submitting !== null}
            onClick={() => choose("next_free")}
          >
            {submitting === "next_free" ? "…" : "Slot libre suivant (1 vidéo)"}
          </Button>
          <Button
            size="sm"
            disabled={!preview || cascadeBlocked || submitting !== null}
            onClick={() => choose("cascade")}
          >
            {submitting === "cascade" ? "…" : `Cascader (${cascadeCount} vidéo${cascadeCount > 1 ? "s" : ""})`}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
```

- [ ] **Step 2: Type check** — `cd frontend && npx tsc -b --noEmit`. Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/project-manager/SwitchSlotConfirmModal.tsx
git commit -m "feat(frontend): switch confirmation modal with cascade vs next-free choice"
```

---

### Task 11: SlotPickerPopover — custom time + steal wiring

**Files:**
- Modify: `frontend/src/components/project-manager/SlotPickerPopover.tsx`

**Interfaces:**
- Consumes: `SlotChips` new props (Task 9), `SwitchSlotConfirmModal` (Task 10), `StealSpec`/`SwitchMode`/`SwitchPreview` (Task 8).
- Produces — the `onConfirm` payload union becomes (parents adapt in Task 12):

```ts
export type SlotPickerConfirmPayload =
  | { tiktok_slot: string; overrides?: Partial<Record<Platform, string>>;
      steals?: Partial<Record<Platform, StealSpec>> }   // anchor
  | { slot: string; steal?: StealSpec }                  // single-platform
  | { manual_at: string };                               // manual custom time
```
  New optional prop: `allowManual?: boolean` (default true in anchor mode, false in single-platform); `initialManual?: boolean` (opens with custom time active, for editing manual events).

- [ ] **Step 1: Implement.** Key edits to `SlotPickerPopover.tsx` (state + handlers + render):

Add imports/state:

```tsx
import type { FreeSlot, Platform, ResolveAnchorResult, StealSpec, SwitchMode, SwitchPreview } from "@/types";
import { SwitchSlotConfirmModal } from "./SwitchSlotConfirmModal";
```

```tsx
  const [customTime, setCustomTime] = useState<string>("");       // "HH:MM"
  const [customActive, setCustomActive] = useState(!!props.initialManual);
  const [steals, setSteals] = useState<Partial<Record<Platform, StealSpec>>>({});
  const [switchTarget, setSwitchTarget] = useState<{ platform: Platform; slotIso: string } | null>(null);
```

Custom datetime derivation + proximity warning (place with the other `useMemo`s):

```tsx
  const customIso = useMemo(() => {
    if (!customActive || !selectedDate || !/^\d{2}:\d{2}$/.test(customTime)) return null;
    const [h, m] = customTime.split(":").map(Number);
    const d = new Date(selectedDate);
    d.setHours(h, m, 0, 0);
    return d.toISOString();
  }, [customActive, selectedDate, customTime]);

  const customTooClose =
    customIso !== null && new Date(customIso).getTime() < Date.now() + 30 * 60 * 1000;

  const proximityWarning = useMemo(() => {
    if (!customIso) return null;
    const t = new Date(customIso).getTime();
    const near = slotsForDay.find(
      (s) => !s.available && Math.abs(new Date(s.slot).getTime() - t) <= 60 * 60 * 1000,
    );
    return near
      ? `Un upload (« ${near.taken_by_title ?? "?"} ») est déjà programmé vers ${new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris" }).format(new Date(near.slot))} ce jour-là.`
      : null;
  }, [customIso, slotsForDay]);
```

Suppress `slot_taken` conflicts covered by a pending steal, and gate submit:

```tsx
  const effectiveConflicts = useMemo(
    () =>
      (resolveResult?.conflicts ?? []).filter(
        (c) => !(c.reason === "slot_taken" && steals[c.platform]),
      ),
    [resolveResult, steals],
  );

  const canSubmit = useMemo(() => {
    if (customActive) return !!customIso && !customTooClose;
    if (!selectedSlotIso) return false;
    if (mode === "anchor" && effectiveConflicts.length) return false;
    return true;
  }, [customActive, customIso, customTooClose, selectedSlotIso, mode, effectiveConflicts]);
```

`handleSubmit` becomes:

```tsx
  const handleSubmit = useCallback(async () => {
    setSubmitting(true); setError(null);
    try {
      if (customActive && customIso) {
        await onConfirm({ manual_at: customIso });
      } else if (mode === "anchor" && selectedSlotIso) {
        await onConfirm({
          tiktok_slot: selectedSlotIso,
          overrides,
          steals: Object.keys(steals).length ? steals : undefined,
        });
      } else if (selectedSlotIso) {
        await onConfirm({ slot: selectedSlotIso, steal: steals[platform!] });
      } else {
        return;
      }
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [customActive, customIso, selectedSlotIso, mode, overrides, steals, platform, onConfirm, onClose]);
```

`SlotChips` call gains the steal props (chips of the anchor surface target platform `platformForFetch`):

```tsx
          <SlotChips
            slots={slotsForDay}
            selectedIso={customActive ? null : selectedSlotIso}
            onSelect={(iso) => {
              setCustomActive(false);
              setSelectedSlotIso(iso);
              setSteals((prev) => {
                const next = { ...prev };
                delete next[platformForFetch];
                return next;
              });
            }}
            onSelectTaken={(s) =>
              setSwitchTarget({ platform: platformForFetch, slotIso: s.slot })
            }
            stolenIsos={
              new Set(
                steals[platformForFetch] && selectedSlotIso ? [selectedSlotIso] : [],
              )
            }
            ownProjectId={projectId}
          />
```

Custom-time row (right after the SlotChips block, only when `allowManual !== false && mode === "anchor"`):

```tsx
        {mode === "anchor" && props.allowManual !== false && (
          <div className="border-t border-[hsl(var(--border))] mt-3 pt-2">
            <label className="flex items-center gap-2 text-[11px] text-[hsl(var(--muted-foreground))]">
              <input
                type="checkbox"
                checked={customActive}
                onChange={(e) => {
                  setCustomActive(e.target.checked);
                  if (e.target.checked) { setSelectedSlotIso(null); setSteals({}); }
                }}
              />
              Heure personnalisée (hors slots, toutes plateformes)
            </label>
            {customActive && (
              <div className="mt-1.5">
                <input
                  type="time"
                  value={customTime}
                  onChange={(e) => setCustomTime(e.target.value)}
                  className="text-xs bg-transparent border border-[hsl(var(--border))] rounded px-2 py-1"
                />
                {customTooClose && (
                  <div className="text-[11px] text-[hsl(var(--destructive))] mt-1">
                    Minimum 30 minutes dans le futur.
                  </div>
                )}
                {proximityWarning && (
                  <div className="text-[11px] text-amber-500 mt-1">⚠ {proximityWarning}</div>
                )}
              </div>
            )}
          </div>
        )}
```

Anchor-mode extras (`resolveResult` preview + `PerPlatformOverride`) are wrapped in `!customActive && (…)`; the conflicts line uses `effectiveConflicts` instead of `resolveResult.conflicts`. The submit button label: `{submitting ? "Saving…" : customActive ? "Programmer (manuel)" : "Schedule"}`.

Steal modal at the end of the popover root (before closing `</motion.div>`):

```tsx
        {switchTarget && (
          <SwitchSlotConfirmModal
            open
            projectId={projectId}
            accountId={accountId}
            platform={switchTarget.platform}
            slotIso={switchTarget.slotIso}
            onClose={() => setSwitchTarget(null)}
            onChoose={(chosenMode: SwitchMode, preview: SwitchPreview) => {
              setSteals((prev) => ({
                ...prev,
                [switchTarget.platform]: {
                  mode: chosenMode,
                  expected_occupant_id: preview.occupant_project_id,
                },
              }));
              setSelectedSlotIso(switchTarget.slotIso);
              setCustomActive(false);
            }}
          />
        )}
```

Also add `allowManual?: boolean; initialManual?: boolean;` to `SlotPickerPopoverProps` and widen `onConfirm`'s payload type to `SlotPickerConfirmPayload` (export the type from this file).

`PerPlatformOverride` steal support: pass `onSelectTaken`-equivalent through if that component renders `SlotChips`; open the same `SwitchSlotConfirmModal` with the override platform and store the steal + override iso (`setOverrides` + `setSteals`). Follow the same pattern as the main chips (read `PerPlatformOverride.tsx` first; it takes an `onChangeOverride(p, iso)` callback — add an optional `onStealRequest?: (p: Platform, slotIso: string) => void` prop and wire it to `setSwitchTarget({ platform: p, slotIso })`; on modal confirm for an override platform also call `setOverrides`).

- [ ] **Step 2: Type check** — `cd frontend && npx tsc -b --noEmit`. Expected: errors ONLY in the two parent files (payload union) — fix those in Task 12; if parents error, that confirms the union propagated. If popover itself errors, fix here.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/project-manager/SlotPickerPopover.tsx frontend/src/components/project-manager/PerPlatformOverride.tsx
git commit -m "feat(frontend): custom-time and steal wiring in slot picker"
```

---

### Task 12: Parent wiring — ProjectManagerModal + PlanningModal

**Files:**
- Modify: `frontend/src/components/project-manager/ProjectManagerModal.tsx` (~lines 550-620 `startUploadWithChecks`, ~1247-1272 popover usage; `AnchorPayload` type)
- Modify: `frontend/src/components/planning/PlanningModal.tsx` (~lines 227-285 both popover usages)

**Interfaces:**
- Consumes: `SlotPickerConfirmPayload` (Task 11), `api.reserveManual` / `api.switchApply` / extended `reserveAnchor`/`rescheduleAnchor` (Task 8).
- Produces: end-to-end flows — no new exports.

- [ ] **Step 1: ProjectManagerModal.** Extend `AnchorPayload` (find its declaration via grep) to include `steals?: Partial<Record<Platform, StealSpec>>`, pass it through in `startUploadWithChecks`'s `api.reserveAnchor` call:

```tsx
          await api.reserveAnchor(projectId, {
            account_id: accountId!,
            tiktok_slot: anchorPayload.tiktok_slot,
            overrides: anchorPayload.overrides,
            steals: anchorPayload.steals,
          });
```

The popover `onConfirm` (line ~1257) handles the union:

```tsx
              onConfirm={async (payload) => {
                const ctx = schedulingForProject;
                setSchedulingForProject(null);
                if ("manual_at" in payload) {
                  await api.reserveManual(ctx.row.project_id, {
                    account_id: ctx.accountId,
                    at: payload.manual_at,
                  });
                  // reservations exist now; "auto" upload path reuses them
                  await startUploadWithChecks(ctx.row.project_id, ctx.accountId, "auto");
                  return;
                }
                await startUploadWithChecks(
                  ctx.row.project_id,
                  ctx.accountId,
                  "scheduled",
                  payload as AnchorPayload,
                );
              }}
```

- [ ] **Step 2: PlanningModal.** Single-platform usage (line ~237):

```tsx
              onConfirm={async (payload) => {
                if ("slot" in payload && payload.steal) {
                  const res = await api.switchApply(reslottingSingle.project_id, {
                    account_id: reslottingSingle.account_id,
                    platform: reslottingSingle.platform,
                    slot: payload.slot,
                    mode: payload.steal.mode,
                    expected_occupant_id: payload.steal.expected_occupant_id,
                  });
                  if (Object.values(res.notification_status).includes("pending_retry")) {
                    setError(
                      "Certaines replanifications plateforme seront resynchronisées automatiquement.",
                    );
                  }
                } else if ("slot" in payload) {
                  await api.reschedulePlatform(
                    reslottingSingle.project_id,
                    reslottingSingle.platform,
                    payload.slot,
                  );
                }
                setReslottingSingle(null);
                setPopover(null);
                await reload();
              }}
```

Re-anchor usage (line ~272): handle `manual_at` (edit of a manual project) and pass steals:

```tsx
              onConfirm={async (payload) => {
                if ("manual_at" in payload) {
                  const platforms = Array.from(new Set(
                    events
                      .filter((e) => e.project_id === reAnchoring.project_id)
                      .map((e) => e.platform),
                  ));
                  await api.reserveManual(reAnchoring.project_id, {
                    account_id: reAnchoring.account_id,
                    at: payload.manual_at,
                    platforms,
                  });
                } else if ("tiktok_slot" in payload) {
                  await api.rescheduleAnchor(reAnchoring.project_id, payload);
                }
                setReAnchoring(null);
                setPopover(null);
                await reload();
              }}
```

And pre-fill manual mode when re-anchoring a manual project — where `reAnchoring`'s popover is rendered, add:

```tsx
              initialManual={events.some(
                (e) => e.project_id === reAnchoring.project_id && e.manual,
              )}
```

- [ ] **Step 3: Type check + build**

Run: `cd frontend && npx tsc -b --noEmit && npm run build`
Expected: clean build, zero type errors anywhere.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/project-manager/ProjectManagerModal.tsx frontend/src/components/planning/PlanningModal.tsx
git commit -m "feat(frontend): wire manual scheduling and slot steals into upload + planning flows"
```

---

### Task 13: Planning display — manual badges

**Files:**
- Modify: `frontend/src/components/planning/PlanningCalendar.tsx:94-199` (GroupEventCard)
- Modify: `frontend/src/components/planning/EventPopover.tsx:105-118`

**Interfaces:**
- Consumes: `PlanningEvent.manual` (Task 8).
- Produces: visual only.

- [ ] **Step 1: PlanningCalendar.** In `GroupEventCard`, derive `const isManual = members.some((m) => m.manual);` after `const first = members[0];`, make the card border dashed when manual — change the container style line to:

```tsx
        border: isManual
          ? "1px dashed hsl(45 90% 55%)"
          : "1px solid hsl(var(--border))",
```

and inside the platform-chips row (after the `ordered.map` block), add:

```tsx
        {isManual && (
          <span
            title="Programmation manuelle"
            style={{
              fontSize: 9, fontWeight: 700, padding: "1px 4px",
              borderRadius: 3, color: "hsl(45 90% 55%)",
              border: "1px dashed hsl(45 90% 55%)", lineHeight: "1",
            }}
          >
            M
          </span>
        )}
```

- [ ] **Step 2: EventPopover.** After the `{formatSlot(first.slot)}` div (line ~105-107), add:

```tsx
        {first.manual && (
          <div className="text-[11px] text-amber-500 mb-2">
            Programmation manuelle — hors système de slots
          </div>
        )}
```

- [ ] **Step 3: Type check** — `cd frontend && npx tsc -b --noEmit`. Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/planning/PlanningCalendar.tsx frontend/src/components/planning/EventPopover.tsx
git commit -m "feat(frontend): manual-schedule badge in planning calendar and event popover"
```

---

### Task 14: e2e coverage + full verification sweep

**Files:**
- Modify: `frontend/e2e/upload-split-button.spec.ts`, `frontend/e2e/planning.spec.ts`
- No production code (fix regressions only).

**Interfaces:** consumes everything above.

- [ ] **Step 1: Read both existing spec files** to learn their API-mocking helpers (they mock backend routes with `page.route`). Mirror those patterns exactly.

- [ ] **Step 2: Add e2e scenarios** (adapting helper names to the files' conventions):

1. *Custom time flow* (upload-split-button.spec.ts): open Schedule picker → tick "Heure personnalisée" → type `17:23` → assert submit button reads "Programmer (manuel)" → mock `POST */reserve-manual` and assert it's called with `at` ending in `T15:23:00.000Z` (17:23 Paris = 15:23 UTC in July) and the resolve/override sections are hidden.
2. *Amber chip + switch modal* (upload-split-button.spec.ts): mock `GET */free-slots` returning one slot with `available: false, taken_by_project_id: "projB", taken_by_title: "Naruto"` (future time) → assert chip has amber class (`border-amber-500/60`) and is enabled → click → mock `POST */switch-preview` (2 cascade displaced, 1 next_free) → assert both plan panels render → click "Slot libre suivant (1 vidéo)" → assert final `reserve-anchor` request body contains `steals: { tiktok: { mode: "next_free", expected_occupant_id: "projB" } }`.
3. *Manual badge* (planning.spec.ts): mock `GET */scheduling/events` with one `manual: true` event → assert the "M" badge is visible on the calendar card and "Programmation manuelle" appears in the popover.
4. *Single-platform steal* (planning.spec.ts): open Move on a platform row → click amber chip → choose "Cascader" → assert `POST */switch-apply` called with `mode: "cascade"`.

- [ ] **Step 3: Run e2e**

Run: `cd frontend && npm run test`
Expected: new tests PASS, no existing test broken.

- [ ] **Step 4: Full verification sweep**

```bash
pixi run test -v                      # entire backend suite
cd frontend && npx tsc -b --noEmit && npm run build && npm run test
```
Expected: everything green.

- [ ] **Step 5: Commit**

```bash
git add frontend/e2e/
git commit -m "test(e2e): manual custom-time flow, slot switching, planning badges"
```

---

## Post-plan notes for the orchestrator

- Tasks 1→7 are strictly sequential (backend). Task 8 can start after 7 (payload shapes frozen). Tasks 9, 10 depend on 8 and are independent of each other. 11 depends on 9+10; 12 on 11; 13 on 8 only (can run parallel to 9-12); 14 last.
- Each subagent gets: its full task text (self-contained), the Global Constraints section, and the spec path for context.
- After each task, orchestrator (Fable) reviews the diff before dispatching the next.
