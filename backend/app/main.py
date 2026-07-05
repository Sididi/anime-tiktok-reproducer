import os
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

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
from .services.lan_transfer_service import LanTransferService
from .services.library_hydration_service import LibraryHydrationService
from .services.project_service import ProjectService
from .services.project_startup_service import project_startup_queue
from .services.project_upload_service import project_upload_queue
from .services.reschedule_retry_service import RescheduleRetryService
from .services.storage_box_sftp_client import StorageBoxSftpClient


# Reuse uvicorn's logger so startup diagnostics are visible in normal dev logs.
logger = logging.getLogger("uvicorn.error")


def _track_app_task(app: FastAPI, task: asyncio.Task[None]) -> None:
    tasks: set[asyncio.Task[None]] = getattr(app.state, "startup_tasks", set())
    app.state.startup_tasks = tasks
    tasks.add(task)

    def _cleanup(completed: asyncio.Task[None]) -> None:
        tasks.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Background startup task failed")

    task.add_done_callback(_cleanup)


async def _cancel_app_tasks(app: FastAPI) -> None:
    tasks: set[asyncio.Task[None]] = set(getattr(app.state, "startup_tasks", set()))
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


async def _warm_storage_box_catalog(library_type: LibraryType) -> None:
    try:
        await LibraryHydrationService.ensure_catalog_available(library_type)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Storage Box catalog initialization failed for %s", library_type.value)
        IntegrationHealthService.update_catalog_warmup(
            library_type=library_type,
            status="error",
            detail=str(exc),
        )
        return

    IntegrationHealthService.update_catalog_warmup(
        library_type=library_type,
        status="ok",
        detail="Catalog warmup complete.",
    )


async def _run_storage_box_catalog_warmup() -> None:
    if not settings.storage_box_enabled:
        return

    async with asyncio.TaskGroup() as task_group:
        for library_type in LibraryType:
            task_group.create_task(_warm_storage_box_catalog(library_type))


async def _run_integration_health_check_background() -> None:
    if not settings.integration_startup_health_check_enabled:
        return

    try:
        result = await asyncio.to_thread(IntegrationHealthService.run_startup_health_check)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Integration health check failed during startup")
        IntegrationHealthService.record_integration_check_failure(str(exc))
        return

    logger.info(
        "Integration startup health completed with status=%s summary=%s",
        result.get("status"),
        result.get("summary_status"),
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load local state quickly, then warm external readiness in the background."""
    AccountService.load()
    ProjectService.sync_all_project_pins()
    await LibraryHydrationService.startup_cleanup()
    await project_startup_queue.startup_cleanup()
    await project_upload_queue.startup_cleanup()
    if settings.lan_transfer_enabled:
        LanTransferService.sweep_stale_tmp_files()

    reschedule_retry_stop = asyncio.Event()
    app.state.reschedule_retry_stop = reschedule_retry_stop
    _track_app_task(
        app,
        asyncio.create_task(
            RescheduleRetryService.run_loop(reschedule_retry_stop),
            name="reschedule-retry-loop",
        ),
    )

    app.state.integrations_health = IntegrationHealthService.initialize_startup_state(
        integration_enabled=settings.integration_startup_health_check_enabled,
        storage_box_enabled=settings.storage_box_enabled,
        library_types=list(LibraryType),
    )

    if settings.storage_box_enabled:
        _track_app_task(
            app,
            asyncio.create_task(
                _run_storage_box_catalog_warmup(),
                name="storage-box-catalog-warmup",
            ),
        )
    if settings.integration_startup_health_check_enabled:
        _track_app_task(
            app,
            asyncio.create_task(
                _run_integration_health_check_background(),
                name="integration-startup-health",
            ),
        )

    yield
    reschedule_retry_stop.set()
    await _cancel_app_tasks(app)
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
