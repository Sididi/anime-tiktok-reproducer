from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from ..models import PlatformSchedule, Project
from .account_service import AccountService
from .project_service import ProjectService


@dataclass
class FreeSlot:
    slot: datetime
    available: bool
    taken_by_project_id: str | None = None
    taken_by_title: str | None = None


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


@dataclass
class StealSpec:
    mode: str
    expected_occupant_id: str | None


class SchedulingService:
    """Finds and reserves the next available upload slot per (account, platform)."""

    _MIN_LEAD_MINUTES = 30
    _JITTER_MINUTES = 30
    _MAX_LOOKAHEAD_DAYS = 90
    TIKTOK_EDIT_LOCK_MINUTES = 10
    _reservation_lock = Lock()

    # ------------------------------------------------------------------ helpers

    @classmethod
    def _earliest_allowed_publish_time(cls) -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=cls._MIN_LEAD_MINUTES)

    @classmethod
    def _normalize_utc_datetime(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

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

    @classmethod
    def _can_reuse_reserved_slot(cls, scheduled_at: datetime) -> bool:
        return cls._normalize_utc_datetime(scheduled_at) >= cls._earliest_allowed_publish_time()

    @classmethod
    def _resolve_pool_key(cls, account_id: str, platform: str) -> str:
        """Return the effective pool key, falling back to an account-scoped key."""
        account = AccountService.get_account(account_id)
        key = account.pool_key_for(platform) if account else None
        return key or f"account:{account_id}:{platform}"

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

    @classmethod
    def _collect_reserved_slots_for_pool(cls, pool_key: str, platform: str) -> set[str]:
        return set(cls._collect_pool_reservations(pool_key, platform).keys())

    @classmethod
    def _recompute_aggregates(cls, project: Project) -> None:
        """Recompute derived `scheduled_at` from per-platform reservations."""
        values = list((project.platform_schedules or {}).values())
        project.scheduled_at = (
            max((cls._normalize_utc_datetime(v.scheduled_at) for v in values), default=None)
            if values
            else None
        )
        project.scheduled_slot = None

    @classmethod
    def _randomize_slot(cls, slot_dt: datetime, now_utc: datetime) -> datetime:
        jitter = cls._JITTER_MINUTES
        lower = slot_dt - timedelta(minutes=jitter)
        upper = slot_dt + timedelta(minutes=jitter)

        min_publish = now_utc + timedelta(minutes=cls._MIN_LEAD_MINUTES)
        if lower < min_publish:
            lower = min_publish

        if lower > upper:
            lower = upper

        delta_minutes = int((upper - lower).total_seconds() / 60)
        if delta_minutes <= 0:
            return upper.replace(second=0, microsecond=0)

        offset = random.randint(0, delta_minutes)
        return (lower + timedelta(minutes=offset)).replace(second=0, microsecond=0)

    # ------------------------------------------------------------------- lookup

    @classmethod
    def find_next_slot_for_platform(
        cls,
        account_id: str,
        platform: str,
    ) -> tuple[datetime, datetime]:
        """Find the next free (slot_dt, scheduled_at) for (account, platform)."""
        account = AccountService.get_account(account_id)
        if not account:
            raise ValueError(f"Account {account_id} not found")

        slot_strings = account.slots_for(platform)
        if not slot_strings:
            raise ValueError(
                f"Account {account_id} has no slots configured for platform {platform}"
            )

        slot_times: list[tuple[int, int]] = []
        for slot_str in slot_strings:
            parts = slot_str.strip().split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            slot_times.append((hour, minute))
        slot_times.sort()

        pool_key = cls._resolve_pool_key(account_id, platform)
        reserved_slots = cls._collect_reserved_slots_for_pool(pool_key, platform)

        now_utc = datetime.now(timezone.utc)
        earliest_allowed = cls._earliest_allowed_publish_time()

        current_date = now_utc.date()
        end_date = current_date + timedelta(days=cls._MAX_LOOKAHEAD_DAYS)

        while current_date <= end_date:
            for hour, minute in slot_times:
                slot_dt = datetime(
                    current_date.year, current_date.month, current_date.day,
                    hour, minute, 0,
                    tzinfo=timezone.utc,
                )
                if slot_dt < earliest_allowed:
                    continue
                if slot_dt.isoformat() in reserved_slots:
                    continue
                return slot_dt, cls._randomize_slot(slot_dt, now_utc)

            current_date += timedelta(days=1)

        raise RuntimeError(
            f"No available slot found for account {account_id} platform {platform} "
            f"within {cls._MAX_LOOKAHEAD_DAYS} days"
        )

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
                if slot_dt <= after_utc:
                    continue
                slot_iso = slot_dt.isoformat()
                taker = reservations.get(slot_iso)
                results.append(
                    FreeSlot(
                        slot=slot_dt,
                        available=taker is None,
                        taken_by_project_id=taker.id if taker else None,
                        taken_by_title=(taker.anime_name or taker.id) if taker else None,
                    )
                )
                if len(results) >= limit:
                    return results
            current_date += timedelta(days=1)
        return results

    # ---------------------------------------------------------------- anchoring

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

            # Use a tick before the anchor so the anchor slot itself is a
            # valid candidate (find_free_slots_after is strictly > after).
            # Request a generous batch so we can skip taken slots and find the
            # first available one within the lookahead window.
            free = cls.find_free_slots_after(
                account_id=account_id,
                platform=platform,
                after=anchor - timedelta(microseconds=1),
                limit=cls._MAX_LOOKAHEAD_DAYS * max(len(account.slots_for(platform)), 1),
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

    # -------------------------------------------------------------- reservation

    @classmethod
    def _validate_duplication_restrictions(
        cls, project: Project, account_id: str, slots: list[datetime]
    ) -> None:
        """Enforce duplicated-project rules before persisting reservations."""
        from .project_duplication_service import UploadRestrictionService

        UploadRestrictionService.validate_upload(project, account_id, slots)

    @classmethod
    def _try_reuse_platform_reservation(
        cls,
        project: Project,
        account_id: str,
        platform: str,
    ) -> tuple[datetime, datetime] | None:
        if project.scheduled_account_id != account_id:
            return None
        sched = (project.platform_schedules or {}).get(platform)
        if sched is None:
            return None
        slot_dt = cls._normalize_utc_datetime(sched.slot)
        scheduled_at = cls._normalize_utc_datetime(sched.scheduled_at)
        if not cls._can_reuse_reserved_slot(scheduled_at):
            return None
        return slot_dt, scheduled_at

    @classmethod
    def _reserve_platform_inplace(
        cls,
        project: Project,
        account_id: str,
        platform: str,
    ) -> tuple[datetime, datetime]:
        """Reserve a single platform slot on `project` in memory (no save).

        Reuses an existing per-platform reservation when still valid. Always
        re-collects the reserved-slots set so sibling reservations made earlier
        in the same call are seen.
        """
        reused = cls._try_reuse_platform_reservation(project, account_id, platform)
        if reused is not None:
            return reused

        slot_dt, scheduled_at = cls.find_next_slot_for_platform(account_id, platform)
        schedules = dict(project.platform_schedules or {})
        schedules[platform] = PlatformSchedule(slot=slot_dt, scheduled_at=scheduled_at)
        project.platform_schedules = schedules
        return slot_dt, scheduled_at

    @classmethod
    def reserve_next_platform_slot(
        cls,
        project_id: str,
        account_id: str,
        platform: str,
    ) -> tuple[datetime, datetime]:
        """Reserve (and persist) one platform's slot on the project."""
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")

            reused = cls._try_reuse_platform_reservation(project, account_id, platform)
            if reused is not None:
                return reused

            slot_dt, scheduled_at = cls._reserve_platform_inplace(project, account_id, platform)
            cls._validate_duplication_restrictions(project, account_id, [slot_dt])
            project.scheduled_account_id = account_id
            cls._recompute_aggregates(project)
            ProjectService.save(project)
            return slot_dt, scheduled_at

    @classmethod
    def reserve_all_platform_slots(
        cls,
        project_id: str,
        account_id: str,
        platforms: list[str],
    ) -> dict[str, tuple[datetime, datetime]]:
        """Atomically reserve slots for every requested platform on the project.

        Holds the reservation lock once so sibling platforms in the same call
        never collide with each other. Reuses valid per-platform reservations.
        """
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")

            # If the stored reservation belongs to a different account, drop it
            # before reserving under the new account.
            if project.scheduled_account_id and project.scheduled_account_id != account_id:
                project.platform_schedules = {}

            results: dict[str, tuple[datetime, datetime]] = {}
            for platform in platforms:
                results[platform] = cls._reserve_platform_inplace(project, account_id, platform)

            cls._validate_duplication_restrictions(
                project, account_id, [slot for slot, _ in results.values()]
            )
            project.scheduled_account_id = account_id
            cls._recompute_aggregates(project)
            ProjectService.save(project)
            return results

    @classmethod
    def reserve_anchor(
        cls,
        project_id: str,
        account_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
        steals: dict[str, StealSpec] | None = None,
    ) -> tuple[dict[str, PlatformSchedule], dict[str, SwitchResult]]:
        """Reserve TT (anchor) + each other configured platform on `project`.

        Idempotent: a second call with the same anchor reuses the existing
        per-platform reservations.
        Optionally steals occupied slots (displacing occupants) BEFORE
        resolving the anchor, all under one lock, all-or-nothing.
        Returns (schedules, applied_switches).
        Raises ValueError if any platform conflicts or a steal is stale/blocked.
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
                return dict(project.platform_schedules), {}

            # Drop reservations belonging to a different account before reserving.
            if project.scheduled_account_id and project.scheduled_account_id != account_id:
                project.platform_schedules = {}

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

            # Snapshot every to-be-displaced project's current per-platform
            # schedule BEFORE moving anything, so a downstream anchor conflict
            # (e.g. a non-tiktok override collides with a non-stolen project)
            # can roll the moves back to zero net changes on disk.
            displacement_snapshots: dict[
                tuple[str, str], PlatformSchedule | None
            ] = {}
            for platform, plan in steal_plans:
                for item in plan.displaced:
                    key = (item.project_id, platform)
                    if key in displacement_snapshots:
                        continue
                    moved = ProjectService.load(item.project_id)
                    if moved is None:
                        continue
                    displacement_snapshots[key] = (moved.platform_schedules or {}).get(
                        platform
                    )

            for platform, plan in steal_plans:
                cls._apply_displacements(platform, plan.displaced, now_utc)

            try:
                resolution = cls.resolve_anchor(account_id, anchor, overrides)
                if resolution.conflicts:
                    conflict_summary = ", ".join(
                        f"{c.platform}:{c.reason}" for c in resolution.conflicts
                    )
                    raise ValueError(f"Anchor conflicts: {conflict_summary}")
                cls._validate_duplication_restrictions(
                    project,
                    account_id,
                    [resolved.slot for resolved in resolution.resolved.values()],
                )
            except Exception:
                # Roll back every displaced move so the conflict leaves no
                # partial write on disk.
                for (proj_id, platform), original in displacement_snapshots.items():
                    moved = ProjectService.load(proj_id)
                    if moved is None:
                        continue
                    schedules = dict(moved.platform_schedules or {})
                    if original is None:
                        schedules.pop(platform, None)
                    else:
                        schedules[platform] = original
                    moved.platform_schedules = schedules
                    cls._recompute_aggregates(moved)
                    ProjectService.save(moved)
                raise

            schedules = dict(project.platform_schedules or {})
            for platform, resolved in resolution.resolved.items():
                schedules[platform] = PlatformSchedule(
                    slot=resolved.slot, scheduled_at=resolved.scheduled_at
                )
            project.platform_schedules = schedules
            project.scheduled_account_id = account_id
            cls._recompute_aggregates(project)
            ProjectService.save(project)
            return dict(schedules), applied_switches

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
            cls._validate_duplication_restrictions(project, account_id, [at_utc])
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

    @classmethod
    def reschedule_anchor(
        cls,
        project_id: str,
        tiktok_slot: datetime,
        overrides: dict[str, datetime] | None = None,
        steals: dict[str, StealSpec] | None = None,
    ) -> tuple[dict[str, PlatformSchedule], dict[str, SwitchResult]]:
        """Re-anchor a project's reservations on a new TT slot."""
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                raise ValueError("Project not found")
            account_id = project.scheduled_account_id
            if not account_id:
                raise ValueError("Project has no scheduled account")
            if cls.tiktok_timing_locked(project):
                raise ValueError("timing_locked")
            # Drop existing per-platform reservations so resolve_anchor sees
            # the slots as free in this pool.
            project.platform_schedules = {}
            ProjectService.save(project)

        return cls.reserve_anchor(
            project_id=project_id,
            account_id=account_id,
            tiktok_slot=tiktok_slot,
            overrides=overrides,
            steals=steals,
        )

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
            if cls.tiktok_timing_locked(project):
                raise ValueError("timing_locked")
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

    @classmethod
    def clear_reserved_slots(cls, project_id: str) -> None:
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                return
            project.platform_schedules = {}
            project.scheduled_account_id = None
            cls._recompute_aggregates(project)
            ProjectService.save(project)

    # ----------------------------------------------------------------- cascade

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

        Anchor per platform = first configured slot >= now+30min.
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

            # Build map slot_iso -> project for efficient cascade walking.
            # We need the actual Project entries to keep titles/upload_last_result.
            slot_to_project = cls._collect_pool_reservations(pool_key, platform)

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
                # Don't add a cascade plan for a blocked platform - the apply
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
