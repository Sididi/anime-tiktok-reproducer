"""FastAPI app factory + lifespan."""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.mobile import router as mobile_router
from app.config import Settings
from app.services.discord_client import DiscordClient
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)


def _resolve_paths() -> tuple[Path, Path, Path]:
    base = Path(__file__).resolve().parent.parent
    config_path = Path(os.environ.get("ATR_TIKTOK_SERVER_CONFIG_PATH", base / "config" / "config.yaml"))
    avatars_dir = Path(os.environ.get("ATR_TIKTOK_SERVER_AVATARS_DIR", base / "avatars"))
    data_dir = Path(os.environ.get("ATR_TIKTOK_SERVER_DATA_DIR", base / "data"))
    return config_path, avatars_dir, data_dir


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config_path, avatars_dir, data_dir = _resolve_paths()
    settings = Settings.load(config_path=config_path, avatars_dir=avatars_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    job_store = JobStore(data_dir / "jobs.json")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # If a test has already injected a mock discord client, skip the real one.
        if app.state.discord is None:
            async with DiscordClient(bot_token=settings.discord.bot_token) as discord:
                app.state.settings = settings
                app.state.job_store = job_store
                app.state.discord = discord
                yield
                app.state.discord = None
        else:
            app.state.settings = settings
            app.state.job_store = job_store
            yield
            app.state.discord = None

    app = FastAPI(title="TikTok Server", lifespan=lifespan)
    # Bind for tests that don't go through lifespan. `discord` is None until the
    # lifespan starts; tests that need it should either run inside `with TestClient(app)`
    # or assign `app.state.discord = AsyncMock()` directly.
    app.state.settings = settings
    app.state.job_store = job_store
    app.state.discord = None
    app.include_router(health_router)
    app.include_router(internal_router)
    app.include_router(mobile_router)
    return app


app = create_app()
