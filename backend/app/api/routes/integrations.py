from fastapi import APIRouter

from ...services.integration_health_service import IntegrationHealthService


router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/health")
async def integrations_health():
    """Return cached startup readiness without blocking the backend."""
    return IntegrationHealthService.get_cached_health() or {
        "status": "pending",
        "checked_at": None,
        "run_mode": "startup_background",
        "summary_status": None,
        "global": {},
        "checks": {},
        "startup": {
            "integration_checks": {
                "status": "pending",
                "detail": "Integration checks queued.",
                "checked_at": None,
            },
            "storage_box_catalogs": {
                "status": "pending",
                "detail": "Storage Box catalog warmup queued.",
                "checked_at": None,
                "libraries": {},
            },
        },
    }
