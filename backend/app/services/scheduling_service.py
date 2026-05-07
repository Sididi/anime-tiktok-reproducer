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


class SchedulingService:
    """Finds and reserves the next available upload slot per (account, platform)."""

    _MIN_LEAD_MINUTES = 30
    _JITTER_MINUTES = 30
    _MAX_LOOKAHEAD_DAYS = 90
    _reservation_lock = Lock()

    # ------------------------------------------------------------------ helpers

    @classmethod
    def _earliest_allowed_publish_time(cls) -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=cls._MIN_LEAD_MINUTES)

    @classmethod
    def _normalize_utc_datetime(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

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
                        taken_by_project_id=taker,
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
