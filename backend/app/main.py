import os
import asyncio
import logging
from contextlib import asynccontextmanager

# Set BEFORE any import that may transitively load torch (e.g. anime_searcher).
# torch._inductor.config reads TORCHINDUCTOR_COMPILE_THREADS at import time
# and caches it; if already loaded with the default (os.cpu_count()) the later
# setdefault in transcriber.py has no effect, leading to dozens of compile
# worker processes that never get cleaned up.
os.environ.setdefault("TORCHINDUCTOR_COMPILE_THREADS", "1")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .api import api_router
from .library_types import LibraryType
from .services.account_service import AccountService
from .services.integration_health_service import IntegrationHealthService
from .services.library_hydration_service import LibraryHydrationService
from .services.storage_box_sftp_client import StorageBoxSftpClient


# Reuse uvicorn's logger so startup diagnostics are visible in normal dev logs.
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load accounts and run integration health checks on startup."""
    AccountService.load()
    await LibraryHydrationService.startup_cleanup()
    if settings.storage_box_enabled:
        for library_type in LibraryType:
            try:
                await LibraryHydrationService.ensure_catalog_available(library_type)
            except Exception:
                logger.exception("Storage Box catalog initialization failed for %s", library_type.value)
    if not settings.integration_startup_health_check_enabled:
        app.state.integrations_health = {"status": "skipped", "checks": {}}
        yield
        await StorageBoxSftpClient.close_pool()
        return
    try:
        result = await asyncio.to_thread(IntegrationHealthService.run_startup_health_check)
        app.state.integrations_health = result
        logger.info(
            "Integration startup health completed with status=%s",
            result.get("status"),
        )
        checks = result.get("checks", {})
        if isinstance(checks, dict):
            for name, payload in checks.items():
                if not isinstance(payload, dict):
                    continue
                logger.info(
                    "Integration check %s: status=%s detail=%s",
                    name,
                    payload.get("status", "unknown"),
                    payload.get("detail", ""),
                )
    except Exception:
        logger.exception("Integration health check failed during startup")
        app.state.integrations_health = {"status": "error", "checks": {}}
    yield
    await StorageBoxSftpClient.close_pool()


app = FastAPI(
    title="Anime TikTok Reproducer",
    description="Web app to remaster TikToks by finding anime source clips",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_router)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
