"""One-shot: post a sample upload embed to the configured Discord channel.

Run from anywhere the server's env vars are available:

    cd server
    uv run python scripts/send_test_embed.py

Required env vars:
  ATR_DISCORD_BOT_TOKEN
  ATR_DISCORD_UPLOAD_CHANNEL_ID
  ATR_PUBLIC_BASE_URL          (used for the avatar URL — anything reachable works)

Optional:
  ATR_TEST_DESCRIPTION         (override the description content; default exercises
                                every Discord markdown char so you can confirm
                                escaping survives a mobile copy/paste round-trip)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make `app.*` importable when run from the server/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AccountConfig
from app.models.job import Job, PlatformStatus
from app.services.discord_client import DiscordClient
from app.services.embed_builder import build_embed


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing required env var: {name}")
    return v


async def main() -> None:
    bot_token = _required("ATR_DISCORD_BOT_TOKEN")
    channel_id = _required("ATR_DISCORD_UPLOAD_CHANNEL_ID")
    public_base_url = os.environ.get("ATR_PUBLIC_BASE_URL", "https://tiktok.sididi.tv")

    description = os.environ.get(
        "ATR_TEST_DESCRIPTION",
        # Mix of plain text, hashtags, an emoji, and every markdown char Discord
        # would otherwise interpret. Long-press → Copy on mobile should yield
        # this exact string with no backticks or backslashes.
        "Test description — copy me on mobile! "
        "#OnePiece #anime *not bold* _not italic_ ~not strike~ "
        "|not spoiler| `not code` > not quote # not heading 🔥",
    )

    now = datetime.now(UTC)
    job = Job(
        project_id="test_proj",
        job_id="test_job",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="🧪 Embed copy test",
        description=description,
        drive_video_url="https://drive.google.com/uc?id=test",
        slot_time=now,
        platforms_requested=["tiktok"],
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=None,
        reminder_message_id=None,
        created_at=now,
        updated_at=now,
    )
    accounts = {
        "anime_fr": AccountConfig(
            id="anime_fr",
            name="Anime FR (TEST)",
            language="fr",
            device="iphone_13_pro",
            avatar="anime_fr.jpg",
        )
    }

    embed = build_embed(job, accounts, public_base_url)

    async with DiscordClient(bot_token=bot_token) as client:
        msg_id = await client.post_message(
            channel_id,
            content="🧪 **Test embed** — long-press the description on mobile and copy it.",
            embed=embed,
        )
    print(f"Posted message {msg_id} to channel {channel_id}")


if __name__ == "__main__":
    asyncio.run(main())
