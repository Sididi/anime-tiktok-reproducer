"""Pure function: build a Discord embed dict from a TikTokJob + config."""
from __future__ import annotations

from typing import Any

from app.config import AccountConfig, DeviceConfig
from app.models.job import PlatformStatus, TikTokJob

# Months in French for footer/description rendering.
_FR_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

_PLATFORM_DISPLAY = {
    "youtube": "YouTube",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

_STATUS_EMOJI = {
    "pending": "⏳",
    "uploading": "⏳",
    "uploaded": "✅",
    "skipped": "⚠️",
    "failed": "❌",
}


def _format_french_datetime(dt) -> str:
    return (
        f"{_FR_DAYS[dt.weekday()]} {dt.day} {_FR_MONTHS[dt.month - 1]} "
        f"{dt.year} à {dt.strftime('%H:%M')} UTC"
    )


def _format_platform_line(platform: str, ps: PlatformStatus) -> str:
    label = _PLATFORM_DISPLAY.get(platform, platform.title())
    if platform == "tiktok" and ps.status == "uploaded":
        emoji = "✅"
        suffix = " — Posté"
    elif platform == "tiktok" and ps.status == "pending":
        emoji = "🎯"
        suffix = " — Pending handoff"
    else:
        emoji = _STATUS_EMOJI.get(ps.status, "·")
        if ps.url:
            suffix = f" — {ps.url}"
        elif ps.detail:
            suffix = f" — {ps.status.title()} ({ps.detail})"
        else:
            suffix = f" — {ps.status.title()}"
    return f"{emoji} {label}{suffix}"


def build_embed(
    job: TikTokJob,
    accounts: dict[str, AccountConfig],
    devices: dict[str, DeviceConfig],
    public_base_url: str,
) -> dict[str, Any]:
    account = accounts[job.account_id]
    avatar_url = f"{public_base_url.rstrip('/')}/api/avatars/{account.avatar}"

    plat_lines = [
        _format_platform_line(p, job.platform_statuses.get(p, PlatformStatus(status="pending")))
        for p in job.platforms_requested
    ]

    fields = [
        {"name": "📱 Device", "value": job.device_id, "inline": True},
        {"name": "🆔 Project", "value": job.project_id, "inline": True},
        {"name": "Plateformes", "value": "\n".join(plat_lines), "inline": False},
        {
            "name": "Description TikTok",
            "value": f"```\n{job.description}\n```",
            "inline": False,
        },
        {"name": "Lien vidéo", "value": job.drive_video_url, "inline": False},
    ]

    return {
        "author": {"name": account.name, "icon_url": avatar_url},
        "title": job.anime_title,
        "description": f"Programmé le **{_format_french_datetime(job.slot_time)}**",
        "fields": fields,
        "footer": {
            "text": f"{account.name} · {job.device_id} · {job.slot_time.strftime('%H:%M')} UTC"
        },
    }
