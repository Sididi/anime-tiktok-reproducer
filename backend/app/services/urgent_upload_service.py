"""Urgent-immediate upload flow (2026-08 upload flows redesign).

"Upload urgently (immediate)" publishes right now on every available platform,
ignoring the slot system. Before publishing, posts of OTHER projects that
publish within the next 60 minutes on the same channel (pool) can be shifted
by the user; this service provides:

- compute_preview: the two-phase collision scan (TikTok first, then the other
  platforms) with per-item movability classification;
- apply_plan: the single deferred mutation — nothing is persisted anywhere
  until the user has confirmed the whole flow. Each colliding project's shift
  is applied and platform-notified independently (per-item degradation: a
  post published meanwhile becomes a warning, the rest proceeds).

The 15-minute lead/edit-lock floors do NOT block this flow: shifts run with
bypass flags and degrade to warnings when the platform refuses (PFM post
already processing, video already public, ...).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_service import AccountService
from .platform_reschedule_service import PlatformRescheduleService
from .project_service import ProjectService
from .scheduling_service import (
    SchedulingService,
    StealSpec,
    platform_upload_result_entry,
)
from .scheduling_errors import (
    SchedulingError,
    SchedulingNotFoundError,
)

logger = logging.getLogger("uvicorn.error")

COLLISION_WINDOW = timedelta(minutes=60)
# How far back an unpublished, past-due post still counts as a collision
# (it may publish any second — e.g. an Instagram job pending on the VPS).
_OVERDUE_LOOKBACK = timedelta(minutes=60)


def _normalize(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _platforms_for(account_id: str) -> list[str]:
    from .project_upload_service import _platforms_to_reserve  # noqa: PLC0415

    account = AccountService.get_account(account_id)
    if account is None:
        raise SchedulingNotFoundError(f"Account {account_id} not found")
    return _platforms_to_reserve(account, requested_platforms=None)


class UrgentUploadService:
    # ------------------------------------------------------------- preview

    @classmethod
    def compute_preview(
        cls, project_id: str, account_id: str, tiktok_only: bool = False
    ) -> dict[str, Any]:
        platforms = _platforms_for(account_id)
        immediate_platforms = (
            [p for p in platforms if p == "tiktok"] if tiktok_only else platforms
        )
        now = datetime.now(timezone.utc)

        pool_keys = {
            p: SchedulingService._resolve_pool_key(account_id, p)
            for p in immediate_platforms
        }

        phase1: dict[str, dict[str, Any]] = {}
        phase2: dict[str, dict[str, Any]] = {}
        for other in ProjectService.list_all():
            if other.id == project_id:
                continue
            owner_id = other.scheduled_account_id
            if not owner_id:
                continue
            owner = AccountService.get_account(owner_id)
            for p in immediate_platforms:
                sched = (other.platform_schedules or {}).get(p)
                if sched is None:
                    continue
                owner_pool = (
                    owner.pool_key_for(p) if owner else None
                ) or f"account:{owner_id}:{p}"
                if owner_pool != pool_keys[p]:
                    continue
                t = _normalize(sched.scheduled_at)
                if t >= now + COLLISION_WINDOW or t < now - _OVERDUE_LOOKBACK:
                    continue
                item = cls._classify(other, p, t, now)
                if item is None:
                    continue
                bucket = phase1 if p == "tiktok" else phase2
                entry = bucket.setdefault(
                    other.id,
                    {
                        "project_id": other.id,
                        "anime_title": other.anime_name or other.id,
                        "account_id": owner_id,
                        "items": [],
                    },
                )
                item["suggested_slot"] = cls._suggested_slot(other, owner_id, p, now)
                entry["items"].append(item)

        return {
            "window_minutes": int(COLLISION_WINDOW.total_seconds() // 60),
            "platforms": platforms,
            "immediate_platforms": immediate_platforms,
            "phase1": sorted(phase1.values(), key=lambda e: e["project_id"]),
            "phase2": sorted(phase2.values(), key=lambda e: e["project_id"]),
        }

    @classmethod
    def _classify(
        cls, project, platform: str, t: datetime, now: datetime
    ) -> dict[str, Any] | None:
        """Movability of one colliding (project, platform) post.

        Returns None when nothing will publish (failed/cancelled post) —
        i.e. not a collision at all."""
        from .vps_status_sync_service import VpsStatusSyncService  # noqa: PLC0415

        entry = platform_upload_result_entry(project, platform)
        url = entry.get("url") if entry else None
        entry_status = entry.get("status") if entry else None

        reason: str | None = None
        best_effort = False

        if platform == "tiktok" and project.tiktok_pfm is not None:
            stage = project.tiktok_pfm.stage
            if stage == "published":
                reason = "unmovable_published"
            elif stage == "failed":
                return None
            elif stage == "post_created":
                reason = "unmovable_processing"
            elif stage == "post_scheduled":
                best_effort = (t - now) < timedelta(
                    minutes=SchedulingService.TIKTOK_EDIT_LOCK_MINUTES
                )
        else:
            vps = (
                VpsStatusSyncService.cached_status(project.id, platform)
                if platform in ("instagram", "tiktok")
                else None
            )
            vps_status = (vps or {}).get("status")
            if vps_status == "uploaded" or (
                entry_status == "uploaded" and url and platform in ("instagram", "tiktok")
            ):
                reason = "unmovable_published"
                url = url or (vps or {}).get("url")
            elif vps_status == "uploading":
                reason = "unmovable_processing"
            elif vps_status == "failed" or entry_status == "failed":
                return None
            elif platform in ("youtube", "facebook") and url and t <= now:
                # Natively-scheduled post whose publish instant passed: public.
                reason = "unmovable_published"

        if reason is None and t <= now:
            # Publish window passed with no contrary information: it will
            # publish imminently (or already has) — treat as unshiftable.
            reason = "unmovable_window_passed"

        return {
            "platform": platform,
            "slot": _normalize(
                (project.platform_schedules or {})[platform].slot
            ).isoformat(),
            "scheduled_at": t.isoformat(),
            "manual": bool((project.platform_schedules or {})[platform].manual),
            "movable": reason is None,
            "reason": reason,
            "best_effort": best_effort,
            "posted_url": url,
        }

    @classmethod
    def _suggested_slot(
        cls, project, owner_account_id: str, platform: str, now: datetime
    ) -> str | None:
        """Default re-timing proposal: nearest free slot clear of the urgent
        window, at/after the project's TikTok for non-TT platforms."""
        not_before = now + COLLISION_WINDOW
        if platform != "tiktok":
            tt = (project.platform_schedules or {}).get("tiktok")
            if tt is not None:
                not_before = max(not_before, _normalize(tt.slot))
        try:
            slot_dt, _ = SchedulingService.find_next_slot_for_platform(
                owner_account_id, platform, project_id=project.id, not_before=not_before
            )
            return slot_dt.isoformat()
        except Exception:
            return None

    # --------------------------------------------------------------- apply

    @classmethod
    def apply_plan(
        cls,
        project_id: str,
        account_id: str,
        *,
        shifts: list[dict[str, Any]],
        own_reservations: dict[str, Any] | None,
        tiktok_only: bool,
        confirm_before_tiktok: bool,
    ) -> dict[str, Any]:
        shift_results = [
            cls._apply_shift(shift, confirm_before_tiktok) for shift in shifts
        ]

        own_schedules: dict[str, Any] = {}
        if tiktok_only and own_reservations:
            own_schedules = cls._apply_own_reservations(
                project_id, account_id, own_reservations
            )

        return {"shifts": shift_results, "own_schedules": own_schedules}

    @classmethod
    def _apply_shift(
        cls, shift: dict[str, Any], confirm_before_tiktok: bool
    ) -> dict[str, Any]:
        pid = str(shift.get("project_id") or "")
        kind = str(shift.get("kind") or "anchor")
        base = {"project_id": pid, "kind": kind}
        project = ProjectService.load(pid)
        if project is None:
            return {**base, "status": "failed", "detail": "project not found"}

        # Race guard: the post may have published (or moved) between preview
        # and apply — degrade to a warning instead of blind-shifting.
        now = datetime.now(timezone.utc)
        for platform, expected_iso in (shift.get("expected_scheduled_at") or {}).items():
            sched = (project.platform_schedules or {}).get(platform)
            if sched is None:
                return {**base, "status": "unmovable", "reason": "state_changed"}
            try:
                expected = _normalize(datetime.fromisoformat(str(expected_iso)))
            except ValueError:
                return {**base, "status": "failed", "detail": "bad expected_scheduled_at"}
            if _normalize(sched.scheduled_at) != expected:
                return {**base, "status": "unmovable", "reason": "state_changed"}
            item = cls._classify(project, platform, _normalize(sched.scheduled_at), now)
            if item is None:
                return {**base, "status": "unmovable", "reason": "gone"}
            if not item["movable"]:
                return {**base, "status": "unmovable", "reason": item["reason"]}

        steals = {
            p: StealSpec(
                mode=str(s.get("mode") or "relocate"),
                expected_occupant_id=s.get("expected_occupant_id"),
            )
            for p, s in (shift.get("steals") or {}).items()
        } or None

        try:
            if kind == "anchor":
                if shift.get("manual_at"):
                    at = _normalize(datetime.fromisoformat(str(shift["manual_at"])))
                    platforms = sorted((project.platform_schedules or {}).keys())
                    schedules = SchedulingService.reserve_manual(
                        pid,
                        project.scheduled_account_id or "",
                        at,
                        platforms,
                        bypass_lock=True,
                        bypass_lead=True,
                    )
                else:
                    tiktok_slot = _normalize(
                        datetime.fromisoformat(str(shift["tiktok_slot"]))
                    )
                    overrides = {
                        p: _normalize(datetime.fromisoformat(str(iso)))
                        for p, iso in (shift.get("overrides") or {}).items()
                    } or None
                    schedules, _switches = SchedulingService.reschedule_anchor(
                        pid,
                        tiktok_slot,
                        overrides,
                        steals,
                        allow_before_tiktok=confirm_before_tiktok,
                        bypass_lock=True,
                    )
            else:  # kind == "platform"
                platform = str(shift.get("platform") or "")
                if not platform:
                    return {**base, "status": "failed", "detail": "platform required"}
                if shift.get("manual_at"):
                    at = _normalize(datetime.fromisoformat(str(shift["manual_at"])))
                    schedules = SchedulingService.reserve_manual(
                        pid,
                        project.scheduled_account_id or "",
                        at,
                        [platform],
                        bypass_lock=True,
                        bypass_lead=True,
                    )
                    schedules = {platform: schedules[platform]}
                elif steals and platform in steals:
                    spec = steals[platform]
                    slot = _normalize(datetime.fromisoformat(str(shift["slot"])))
                    SchedulingService.apply_switch(
                        pid,
                        project.scheduled_account_id or "",
                        platform,
                        slot,
                        mode=spec.mode,
                        expected_occupant_id=spec.expected_occupant_id,
                        allow_before_tiktok=confirm_before_tiktok,
                        bypass_lock=True,
                    )
                    reloaded = ProjectService.load(pid)
                    schedules = {
                        platform: (reloaded.platform_schedules or {})[platform]
                    }
                else:
                    slot = _normalize(datetime.fromisoformat(str(shift["slot"])))
                    new_sched = SchedulingService.reschedule_platform(
                        pid,
                        platform,
                        slot,
                        allow_before_tiktok=confirm_before_tiktok,
                        bypass_lock=True,
                        bypass_lead=True,
                    )
                    schedules = {platform: new_sched}
        except SchedulingError as exc:
            return {**base, "status": "failed", "detail": str(exc)}
        except Exception as exc:
            logger.exception("Urgent shift failed for %s", pid)
            return {**base, "status": "failed", "detail": str(exc)}

        # Propagate to the platforms — one call per moved platform. Failures
        # feed the retry loop; PFM-immutable posts come back "skipped" with a
        # detail (surfaced as a warning by the UI).
        notification_status: dict[str, str] = {}
        for platform, sched in schedules.items():
            notification_status[platform] = cls._notify_and_track(
                pid, platform, sched.scheduled_at
            )
        status = "ok"
        if any(v == "pending_retry" for v in notification_status.values()):
            status = "pending_retry"
        return {**base, "status": status, "notification_status": notification_status}

    @classmethod
    def _notify_and_track(
        cls, project_id: str, platform: str, new_scheduled_at: datetime
    ) -> str:
        """PlatformRescheduleService.notify + reschedule_pending bookkeeping
        (same contract as routes/scheduling._notify_displaced)."""
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
                "last_attempt_at": datetime.now(timezone.utc),
            }
            project.reschedule_pending = pending
            ProjectService.save(project)
        return result.status

    @classmethod
    def _apply_own_reservations(
        cls, project_id: str, account_id: str, own: dict[str, Any]
    ) -> dict[str, Any]:
        """TikTok-only mode: schedule the urgent project's OTHER platforms
        from the user's chosen starting instant (or exact manual time)."""
        others = [p for p in _platforms_for(account_id) if p != "tiktok"]
        if not others:
            return {}
        if own.get("manual_at"):
            at = _normalize(datetime.fromisoformat(str(own["manual_at"])))
            schedules = SchedulingService.reserve_manual(
                project_id, account_id, at, others
            )
            return {
                p: {"slot": s.slot.isoformat(), "scheduled_at": s.scheduled_at.isoformat()}
                for p, s in schedules.items()
                if p in others
            }
        if own.get("first_slot"):
            first = _normalize(datetime.fromisoformat(str(own["first_slot"])))
            results = SchedulingService.reserve_platforms_not_before(
                project_id, account_id, first, others
            )
            return {
                p: {"slot": slot.isoformat(), "scheduled_at": sched.isoformat()}
                for p, (slot, sched) in results.items()
            }
        return {}
