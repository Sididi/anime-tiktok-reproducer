"""Pure function: build a Discord embed dict from a Job + config."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import AccountConfig
from app.models.job import Job, PlatformStatus

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



def format_french_datetime(dt: datetime, *, tz: str = "UTC") -> str:
    """Render `dt` in French. `tz` is the IANA timezone to display in."""
    target = ZoneInfo(tz)
    local = dt.astimezone(target)
    label = "UTC" if tz == "UTC" else (local.tzname() or tz)
    return (
        f"{_FR_DAYS[local.weekday()]} {local.day} {_FR_MONTHS[local.month - 1]} "
        f"{local.year} à {local.strftime('%H:%M')} {label}"
    )


# Backwards-compat private alias for the original caller.
_format_french_datetime = format_french_datetime


def _format_platform_line(platform: str, ps: PlatformStatus) -> str:
    label = _PLATFORM_DISPLAY.get(platform, platform.title())
    emoji = _STATUS_EMOJI.get(ps.status, "·")
    if ps.url:
        suffix = f" — {ps.url}"
    elif ps.detail:
        suffix = f" — {ps.status.title()} ({ps.detail})"
    else:
        suffix = f" — {ps.status.title()}"
    return f"{emoji} {label}{suffix}"


def build_embed(
    job: Job,
    accounts: dict[str, AccountConfig],
    public_base_url: str,
) -> dict[str, Any]:
    account = accounts[job.account_id]
    avatar_url = f"{public_base_url.rstrip('/')}/api/avatars/{account.avatar}"

    plat_lines = [
        _format_platform_line(p, job.platform_statuses.get(p, PlatformStatus(status="pending")))
        for p in job.platforms_requested
    ]

    fields: list[dict[str, Any]] = []
    if job.device_id:
        fields.append({"name": "📱 Device", "value": job.device_id, "inline": True})
    fields.extend([
        {"name": "🆔 Project", "value": job.project_id, "inline": True},
        {"name": "Plateformes", "value": "\n".join(plat_lines), "inline": False},
        {
            "name": "Description TikTok",
            # Plain text, no markdown escaping. Discord mobile's "copy" returns
            # raw source — backticks (`x`) come through as backticks, escapes
            # (\*) come through as backslashes. Both ruin a paste into TikTok.
            # Anime TikTok descriptions don't use *, _, ~, |, >, so the
            # markdown-collision case is acceptably rare.
            "value": job.description,
            "inline": False,
        },
        {"name": "Lien vidéo", "value": job.drive_video_url, "inline": False},
    ])

    footer_bits = [account.name]
    if job.device_id:
        footer_bits.append(job.device_id)
    footer_bits.append(f"{job.slot_time.strftime('%H:%M')} UTC")

    return {
        "author": {"name": account.name, "icon_url": avatar_url},
        "title": job.anime_title,
        "description": f"Programmé le **{_format_french_datetime(job.slot_time)}**",
        "fields": fields,
        "footer": {"text": " · ".join(footer_bits)},
    }
