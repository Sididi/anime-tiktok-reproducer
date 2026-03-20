"""Service for tracking TikTok URLs to detect duplicates."""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings


class TikTokUrlDbService:
    """Lightweight JSON-based database for tracking used TikTok URLs."""

    DB_PATH: Path = settings.data_dir / "tiktok_urls.json"
    _lock = asyncio.Lock()

    # Matches /video/<digits> in any TikTok URL variant
    _VIDEO_ID_RE = re.compile(r"/video/(\d+)")

    @classmethod
    def normalize(cls, url: str) -> str | None:
        """Extract the TikTok video ID from a URL.

        Returns the video ID string, or None if not a valid TikTok video URL.
        """
        m = cls._VIDEO_ID_RE.search(url)
        return m.group(1) if m else None

    @classmethod
    def _load_db(cls) -> dict:
        if cls.DB_PATH.exists():
            return json.loads(cls.DB_PATH.read_text(encoding="utf-8"))
        return {}

    @classmethod
    def _save_db(cls, db: dict) -> None:
        cls.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        cls.DB_PATH.write_text(
            json.dumps(db, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    async def check(cls, url: str) -> dict:
        """Check if a TikTok URL has been used before.

        Returns:
            {exists: bool, video_id: str | None, registered_at: str | None}
        """
        video_id = cls.normalize(url)
        if not video_id:
            return {"exists": False, "video_id": None, "registered_at": None}

        db = cls._load_db()
        entry = db.get(video_id)
        if entry:
            return {
                "exists": True,
                "video_id": video_id,
                "registered_at": entry.get("registered_at"),
            }
        return {"exists": False, "video_id": video_id, "registered_at": None}

    @classmethod
    async def register(cls, url: str) -> None:
        """Register a TikTok URL in the database (idempotent)."""
        video_id = cls.normalize(url)
        if not video_id:
            return

        async with cls._lock:
            db = cls._load_db()
            if video_id not in db:
                db[video_id] = {
                    "url": url,
                    "registered_at": datetime.now(timezone.utc).isoformat(),
                }
                cls._save_db(db)
