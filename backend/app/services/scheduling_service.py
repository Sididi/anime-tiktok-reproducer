from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .account_service import AccountService
from .project_service import ProjectService


class SchedulingService:
    """Finds the next available upload slot for an account."""

    # Minimum time from now before a slot can be used
    _MIN_LEAD_MINUTES = 30
    # Randomization window around the slot time
    _JITTER_MINUTES = 30
    # Maximum days to look ahead
    _MAX_LOOKAHEAD_DAYS = 90

    @classmethod
    def find_next_slot(cls, account_id: str) -> tuple[datetime, datetime]:
        """
        Find the next available slot for the given account.

        Returns (slot_datetime, randomized_datetime):
          - slot_datetime: the exact UTC slot time that was reserved
          - randomized_datetime: slot +/- 30min jitter for actual publish
        """
        account = AccountService.get_account(account_id)
        if not account:
            raise ValueError(f"Account {account_id} not found")
        if not account.slots:
            raise ValueError(f"Account {account_id} has no slots configured")

        # Parse slot hours (e.g. "14:00" -> (14, 0))
        slot_times: list[tuple[int, int]] = []
        for slot_str in account.slots:
            parts = slot_str.strip().split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            slot_times.append((hour, minute))
        slot_times.sort()

        # Collect already-reserved slots for this account
        reserved_slots: set[str] = set()
        projects = ProjectService.list_all()
        for project in projects:
            if project.scheduled_account_id == account_id and project.scheduled_slot:
                reserved_slots.add(project.scheduled_slot)

        now_utc = datetime.now(timezone.utc)
        earliest_allowed = now_utc + timedelta(minutes=cls._MIN_LEAD_MINUTES)

        # Iterate days starting from today
        current_date = now_utc.date()
        end_date = current_date + timedelta(days=cls._MAX_LOOKAHEAD_DAYS)

        while current_date <= end_date:
            for hour, minute in slot_times:
                slot_dt = datetime(
                    current_date.year, current_date.month, current_date.day,
                    hour, minute, 0,
                    tzinfo=timezone.utc,
                )
                # Skip if slot is in the past or too close to now
                if slot_dt < earliest_allowed:
                    continue
                # Skip if already reserved
                slot_iso = slot_dt.isoformat()
                if slot_iso in reserved_slots:
                    continue

                # Found a valid slot - randomize
                randomized = cls._randomize_slot(slot_dt, now_utc)
                return slot_dt, randomized

            current_date += timedelta(days=1)

        raise RuntimeError(
            f"No available slot found for account {account_id} "
            f"within {cls._MAX_LOOKAHEAD_DAYS} days"
        )

    @classmethod
    def _randomize_slot(cls, slot_dt: datetime, now_utc: datetime) -> datetime:
        """Add uniform jitter of +/- _JITTER_MINUTES around the slot time."""
        jitter = cls._JITTER_MINUTES
        lower = slot_dt - timedelta(minutes=jitter)
        upper = slot_dt + timedelta(minutes=jitter)

        # Clamp lower bound to ensure we're at least _MIN_LEAD_MINUTES from now
        min_publish = now_utc + timedelta(minutes=cls._MIN_LEAD_MINUTES)
        if lower < min_publish:
            lower = min_publish

        if lower > upper:
            lower = upper

        # Random minute-precision time in [lower, upper]
        delta_minutes = int((upper - lower).total_seconds() / 60)
        if delta_minutes <= 0:
            return upper.replace(second=0, microsecond=0)

        offset = random.randint(0, delta_minutes)
        result = lower + timedelta(minutes=offset)
        return result.replace(second=0, microsecond=0)
