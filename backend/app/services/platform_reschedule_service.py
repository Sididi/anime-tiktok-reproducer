from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from googleapiclient.discovery import build

from ..config import settings
from ..models import Project
from .account_service import AccountService

logger = logging.getLogger("uvicorn.error")


NotificationStatus = Literal["ok", "pending_retry", "skipped"]


@dataclass
class NotificationResult:
    status: NotificationStatus
    error: str | None = None


class PlatformRescheduleService:
    """Propagates schedule changes to platform APIs and the VPS scheduler."""

    @classmethod
    def _platform_video_url(cls, project: Project, platform: str) -> str | None:
        result = project.upload_last_result or {}
        platforms = result.get("platforms") if isinstance(result, dict) else None
        entry = None
        if isinstance(platforms, dict):
            entry = platforms.get(platform)
        elif isinstance(platforms, list):
            entry = next(
                (
                    item
                    for item in platforms
                    if isinstance(item, dict) and item.get("platform") == platform
                ),
                None,
            )
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
        m = (
            re.search(r"/(?:videos?|reel)/(\d+)", url)
            or re.search(r"v=(\d+)", url)
        )
        return m.group(1) if m else None

    @classmethod
    def notify(
        cls, project: Project, platform: str, new_scheduled_at: datetime
    ) -> NotificationResult:
        # TikTok is published via Post for Me; the backend owns the scheduled
        # post (project.tiktok_pfm) since the 2026-08 migration. Rescheduling
        # updates the PFM post directly (legacy VPS-relayed projects still
        # PATCH the server job slot).
        if platform == "tiktok":
            try:
                return cls._notify_tiktok(project, new_scheduled_at)
            except Exception as exc:
                logger.warning(
                    "platform reschedule failed: project=%s platform=tiktok error=%s",
                    project.id, exc,
                )
                return NotificationResult(status="pending_retry", error=str(exc))

        url = cls._platform_video_url(project, platform)
        # Facebook must run even without a posted URL: a >29d server-held
        # CREATE hold has no native post yet (its slot lives on the /server).
        if not url and platform != "facebook":
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
        # TikTok cancel: delete the backend-owned PFM post (see _cancel_tiktok).
        if platform == "tiktok":
            try:
                return cls._cancel_tiktok(project)
            except Exception as exc:
                logger.warning(
                    "platform cancel failed: project=%s platform=tiktok error=%s",
                    project.id, exc,
                )
                return NotificationResult(status="pending_retry", error=str(exc))

        url = cls._platform_video_url(project, platform)
        # Facebook: an unconverted >29d server hold has no posted URL but must
        # still be dropped on cancel.
        if not url and platform not in ("instagram", "facebook"):
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

    _FB_GRAPH_VERSION = "v25.0"

    # Implementations live in tasks 10-12.
    @classmethod
    def _notify_youtube(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        video_id = cls._youtube_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        try:
            creds = AccountService.get_youtube_credentials(project.scheduled_account_id)
        except ValueError as exc:
            # Permanent config gap (account has no YouTube credentials):
            # retrying can never succeed — surface and skip instead of
            # feeding the retry loop forever.
            return NotificationResult(status="skipped", error=str(exc))
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

    # Meta's scheduled_publish_time window; keep in sync with
    # UploadPhaseService._FACEBOOK_NATIVE_HORIZON_DAYS and the server's
    # FACEBOOK_CONVERT_LEAD_DAYS (28 = 29 - 1 day of margin).
    _FB_NATIVE_HORIZON_DAYS = 29
    _FB_PLACEHOLDER_LEAD_DAYS = 28

    @classmethod
    def _facebook_known_video_id(cls, project: Project, url: str | None) -> str | None:
        """Native post id: from the posted URL, or from the VPS hold state
        (a server-converted hold reports facebook_video_id via status sync)."""
        if url:
            video_id = cls._facebook_video_id(url)
            if video_id:
                return video_id
        from .vps_status_sync_service import VpsStatusSyncService  # noqa: PLC0415

        return VpsStatusSyncService.cached_facebook_video_id(project.id)

    @classmethod
    def _patch_facebook_schedule(
        cls, project: Project, video_id: str, at: datetime
    ) -> None:
        creds = AccountService.get_meta_credentials(project.scheduled_account_id)
        epoch = int(at.timestamp())
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

    @classmethod
    def _notify_facebook(
        cls, project: Project, url: str | None, new_scheduled_at: datetime
    ) -> NotificationResult:
        """Facebook reschedule matrix (2026-08 long-range redesign).

        1. Native post + target inside the window → one Graph call.
        2. Native post + target beyond the window → park the post at a
           placeholder inside the window and register a RETIME hold on the
           /server (which pushes it to the real target at T-28d).
        3. No native post (server-held CREATE hold, not converted yet) →
           PATCH the server job's slot; the hold logic does the rest.
        """
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=cls._FB_NATIVE_HORIZON_DAYS)
        video_id = cls._facebook_known_video_id(project, url)

        if video_id is None:
            # Case 3: unconverted server hold (or nothing uploaded yet).
            return cls._patch_server_slot(project, "facebook", new_scheduled_at)

        if new_scheduled_at <= horizon:
            # Case 1: direct native reschedule. Best-effort sync of any
            # server hold so a pending conversion targets the same instant.
            cls._patch_facebook_schedule(project, video_id, new_scheduled_at)
            try:
                cls._patch_server_slot(project, "facebook", new_scheduled_at)
            except Exception:
                logger.warning(
                    "FB server-hold slot sync failed for %s (non-fatal)",
                    project.id,
                    exc_info=True,
                )
            return NotificationResult(status="ok")

        # Case 2: native post pushed beyond the window — park + RETIME hold.
        placeholder = now + timedelta(days=cls._FB_PLACEHOLDER_LEAD_DAYS)
        cls._patch_facebook_schedule(project, video_id, placeholder)
        from .discord_service import DiscordService  # noqa: PLC0415

        creds = AccountService.get_meta_credentials(project.scheduled_account_id)
        hold = DiscordService.facebook_hold(
            project.id,
            account_id=project.scheduled_account_id or "",
            scheduled_at=new_scheduled_at,
            facebook={
                "page_id": creds.page_id,
                "page_access_token": creds.facebook_page_access_token,
                "video_id": video_id,
                "graph_api_version": cls._FB_GRAPH_VERSION,
            },
            anime_title=project.anime_name,
        )
        if hold is None:
            # The post is parked at the placeholder — without the hold it
            # would publish there. Keep retrying via the pending loop.
            return NotificationResult(
                status="pending_retry",
                error="facebook hold registration failed (post parked at placeholder)",
            )
        return NotificationResult(status="ok")

    @classmethod
    def _notify_instagram(cls, project: Project, new_scheduled_at: datetime) -> NotificationResult:
        return cls._patch_server_slot(project, "instagram", new_scheduled_at)

    @classmethod
    def _notify_tiktok(
        cls, project: Project, new_scheduled_at: datetime
    ) -> NotificationResult:
        from .post_for_me_client import (  # noqa: PLC0415
            PostForMeClient,
            PostForMeError,
            build_post_body,
        )
        from .project_service import ProjectService  # noqa: PLC0415

        state = project.tiktok_pfm
        if state is None:
            # Legacy pre-migration project: the VPS job still owns TikTok.
            return cls._patch_server_slot(project, "tiktok", new_scheduled_at)

        if state.stage in ("post_created", "published"):
            # TikTok is processing / has posted: the PFM post is immutable.
            return NotificationResult(
                status="skipped",
                error=f"tiktok post is {state.stage}; cannot be rescheduled",
            )
        if not state.media_url:
            # Nothing staged on PFM yet (upload failed early): the local
            # reservation change is all there is to do.
            return NotificationResult(status="skipped")

        body = build_post_body(
            social_account_id=state.social_account_id or "",
            media_url=state.media_url,
            caption=state.caption or "",
            post_for_me_platform=state.post_for_me_platform,
            privacy_status=state.privacy_status,
            allow_comment=state.allow_comment,
            allow_duet=state.allow_duet,
            allow_stitch=state.allow_stitch,
            scheduled_at=new_scheduled_at,
        )
        if state.stage == "post_scheduled" and state.post_id:
            try:
                PostForMeClient.update_post(state.post_id, body)
            except PostForMeError as exc:
                # PUT semantics are best-effort: fall back to delete+recreate
                # (the staged media_url is reusable).
                logger.warning(
                    "PFM update_post failed for %s (%s); falling back to "
                    "delete+recreate",
                    project.id,
                    exc.detail,
                )
                PostForMeClient.delete_post(state.post_id)
                state.post_id = PostForMeClient.create_post(body)
        else:
            # media staged but no live post (creation failed / was cancelled):
            # (re)create the post at the new instant.
            state.post_id = PostForMeClient.create_post(body)
        state.stage = "post_scheduled"
        state.scheduled_at = new_scheduled_at
        state.last_error = None
        project.tiktok_pfm = state
        ProjectService.save(project)
        return NotificationResult(status="ok")

    @classmethod
    def _server_base_url(cls) -> str | None:
        base = settings.tiktok_server_internal_url
        return base.rstrip("/") if base else None

    @classmethod
    def _patch_server_slot(
        cls, project: Project, platform: str, new_scheduled_at: datetime
    ) -> NotificationResult:
        base = cls._server_base_url()
        if not base or not settings.tiktok_server_internal_token:
            # No /server/ deployed — silently skip the platforms that depend
            # on it (TT reminder, IG publish). The local platform_schedules
            # change is still persisted; nothing to propagate.
            return NotificationResult(status="skipped")
        url = f"{base}/api/internal/jobs/{project.id}/slot"
        resp = httpx.patch(
            url,
            json={
                "platform_scheduled_at": {platform: new_scheduled_at.isoformat()},
            },
            headers={"Authorization": f"Bearer {settings.tiktok_server_internal_token}"},
            timeout=20.0,
        )
        if resp.status_code == 404:
            return NotificationResult(status="skipped")
        resp.raise_for_status()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_youtube(cls, project: Project, url: str) -> NotificationResult:
        video_id = cls._youtube_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        try:
            creds = AccountService.get_youtube_credentials(project.scheduled_account_id)
        except ValueError as exc:
            return NotificationResult(status="skipped", error=str(exc))
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "id": video_id,
            "status": {"privacyStatus": "private"},
        }
        youtube.videos().update(part="status", body=body).execute()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_facebook(cls, project: Project, url: str | None) -> NotificationResult:
        # Drop any long-range server hold first (best-effort): marking the
        # platform status "skipped" makes the hold dispatcher terminal.
        from .discord_service import DiscordService  # noqa: PLC0415
        from .vps_status_sync_service import VpsStatusSyncService  # noqa: PLC0415

        if VpsStatusSyncService.cached_status(project.id, "facebook") is not None:
            try:
                DiscordService.update_job_platform(
                    project.id, "facebook", status="skipped", detail="cancelled"
                )
            except Exception:
                logger.warning(
                    "FB server-hold cancel sync failed for %s", project.id, exc_info=True
                )

        video_id = cls._facebook_known_video_id(project, url)
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

    @classmethod
    def _cancel_instagram(cls, project: Project) -> NotificationResult:
        return cls.delete_server_job(project)

    @classmethod
    def delete_server_job(cls, project: Project) -> NotificationResult:
        """Delete the VPS job, cancelling pending Instagram and TikTok work."""
        base = cls._server_base_url()
        if not base or not settings.tiktok_server_internal_token:
            return NotificationResult(status="skipped")
        url = f"{base}/api/internal/jobs/{project.id}"
        resp = httpx.delete(
            url,
            headers={"Authorization": f"Bearer {settings.tiktok_server_internal_token}"},
            timeout=20.0,
        )
        if resp.status_code == 404:
            return NotificationResult(status="skipped")
        resp.raise_for_status()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_tiktok(cls, project: Project) -> NotificationResult:
        """Cancel the backend-owned PFM post (2026-08 migration).

        The legacy path PATCHed {"reminder_cancelled": true} on the VPS job —
        which the server dispatcher never read, so a scheduled PFM post kept
        publishing (bug). The PFM DELETE below actually cancels the post.
        """
        from .post_for_me_client import PostForMeClient, PostForMeError  # noqa: PLC0415
        from .project_service import ProjectService  # noqa: PLC0415

        state = project.tiktok_pfm
        if state is None:
            # No backend-owned PFM post: either a manual-mode job (2026-08 —
            # the VPS row is pending until the Discord ✅) or a legacy
            # pre-migration project. Flip the server row to "skipped": the
            # server cancels/deletes its reminder on any terminal TikTok
            # status. Deleting the whole VPS job would also cancel Instagram.
            if project.final_upload_discord_message_id:
                from .discord_service import DiscordService  # noqa: PLC0415

                DiscordService.update_job_platform(
                    project.id, "tiktok", status="skipped", detail="Annulé"
                )
                return NotificationResult(status="ok")
            return NotificationResult(status="skipped")

        if state.stage == "published":
            return NotificationResult(
                status="skipped", error="tiktok already published"
            )
        if state.post_id and state.stage in ("post_scheduled", "post_created"):
            try:
                PostForMeClient.delete_post(state.post_id)
            except PostForMeError as exc:
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    # PFM refuses (e.g. TikTok already processing): surface,
                    # don't retry forever.
                    return NotificationResult(status="skipped", error=exc.detail)
                raise
        project.tiktok_pfm = None
        ProjectService.save(project)
        return NotificationResult(status="ok")

    # --- legacy VPS reminder cancel (pre 2026-08 PFM migration) -------------
    # Kept for reference per owner instruction. The server never consumed
    # `reminder_cancelled` in its dispatcher, so this was a silent no-op for
    # scheduled PFM posts.
    # @classmethod
    # def _cancel_tiktok_legacy(cls, project: Project) -> NotificationResult:
    #     base = cls._server_base_url()
    #     if not base or not settings.tiktok_server_internal_token:
    #         return NotificationResult(status="skipped")
    #     url = f"{base}/api/internal/jobs/{project.id}/slot"
    #     resp = httpx.patch(
    #         url,
    #         json={"reminder_cancelled": True},
    #         headers={"Authorization": f"Bearer {settings.tiktok_server_internal_token}"},
    #         timeout=20.0,
    #     )
    #     if resp.status_code == 404:
    #         return NotificationResult(status="skipped")
    #     resp.raise_for_status()
    #     return NotificationResult(status="ok")
