from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from threading import Lock

from ..models import PlatformSchedule, Project
from .account_service import AccountService
from .project_service import ProjectService


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
    def _collect_reserved_slots_for_pool(cls, pool_key: str, platform: str) -> set[str]:
        """Return ISO slot strings already reserved in the given (pool, platform)."""
        account_pool_keys: dict[str, str] = {}
        for acc_id, acc in AccountService.all_accounts().items():
            account_pool_keys[acc_id] = acc.pool_key_for(platform) or f"account:{acc_id}:{platform}"

        reserved: set[str] = set()
        for project in ProjectService.list_all():
            schedules = project.platform_schedules or {}
            sched = schedules.get(platform)
            if sched is None:
                continue
            owner_id = project.scheduled_account_id
            if not owner_id:
                continue
            if account_pool_keys.get(owner_id) == pool_key:
                reserved.add(cls._normalize_utc_datetime(sched.slot).isoformat())
        return reserved

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
    def clear_reserved_slots(cls, project_id: str) -> None:
        with cls._reservation_lock:
            project = ProjectService.load(project_id)
            if project is None:
                return
            project.platform_schedules = {}
            project.scheduled_account_id = None
            cls._recompute_aggregates(project)
            ProjectService.save(project)
