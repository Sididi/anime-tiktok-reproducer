"""FastAPI app factory + lifespan."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.public import router as public_router
from app.config import Settings
from app.services.discord_client import DiscordClient
from app.services.job_store import JobStore
from app.services.reaction_listener import ReactionListener
from app.services.reminder_scheduler import run_scheduler_loop

logger = logging.getLogger(__name__)
_INSTAGRAM_TEMP_DOWNLOAD_GLOB = "ig-reel-*.mp4"
_INSTAGRAM_TEMP_PASS_DIR_GLOB = "ig-reel-pass-*"


def _resolve_paths() -> tuple[Path, Path, Path]:
    base = Path(__file__).resolve().parent.parent
    config_path = Path(
        os.environ.get("ATR_TIKTOK_SERVER_CONFIG_PATH", base / "config" / "config.yaml")
    )
    avatars_dir = Path(os.environ.get("ATR_TIKTOK_SERVER_AVATARS_DIR", base / "avatars"))
    data_dir = Path(os.environ.get("ATR_TIKTOK_SERVER_DATA_DIR", base / "data"))
    return config_path, avatars_dir, data_dir


def _cleanup_instagram_temp_downloads(temp_dir: Path | None = None) -> int:
    """Remove IG upload temp files left behind by a process crash/restart."""
    root = temp_dir or Path(tempfile.gettempdir())
    removed = 0
    for path in root.glob(_INSTAGRAM_TEMP_DOWNLOAD_GLOB):
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("Failed to remove orphan Instagram temp download %s", path)
    for path in root.glob(_INSTAGRAM_TEMP_PASS_DIR_GLOB):
        if not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            logger.warning("Failed to remove orphan Instagram temp pass directory %s", path)
    if removed:
        logger.info("Removed %d orphan Instagram temp artifact(s)", removed)
    return removed


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config_path, avatars_dir, data_dir = _resolve_paths()
    settings = Settings.load(config_path=config_path, avatars_dir=avatars_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    job_store = JobStore(data_dir / "jobs.json")
    _cleanup_instagram_temp_downloads()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # If a test has already injected a mock discord client, skip the real one.
        async def _start_scheduler(discord_client) -> tuple[asyncio.Task, asyncio.Event]:
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                run_scheduler_loop(
                    store=job_store,
                    settings=settings,
                    discord=discord_client,
                    interval_seconds=float(
                        os.environ.get("ATR_REMINDER_INTERVAL_SECONDS", "30")
                    ),
                    stop_event=stop_event,
                )
            )
            return task, stop_event

        async def _stop_scheduler(task: asyncio.Task, stop_event: asyncio.Event) -> None:
            stop_event.set()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if app.state.discord is None:
            async with DiscordClient(bot_token=settings.discord.bot_token) as discord:
                app.state.settings = settings
                app.state.job_store = job_store
                app.state.discord = discord
                listener = ReactionListener(
                    bot_token=settings.discord.bot_token,
                    store=job_store,
                    settings=settings,
                    rest_discord_client=discord,
                )
                await listener.start()
                sched_task, stop_event = await _start_scheduler(discord)
                try:
                    yield
                finally:
                    await _stop_scheduler(sched_task, stop_event)
                    await listener.stop()
                    app.state.discord = None
        else:
            app.state.settings = settings
            app.state.job_store = job_store
            sched_task, stop_event = await _start_scheduler(app.state.discord)
            try:
                yield
            finally:
                await _stop_scheduler(sched_task, stop_event)
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
    app.include_router(public_router)
    return app


app = create_app()
