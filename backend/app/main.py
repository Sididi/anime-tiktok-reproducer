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
from .services.integration_health_service import IntegrationHealthService


# Reuse uvicorn's logger so startup diagnostics are visible in normal dev logs.
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run integration health checks on startup."""
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
