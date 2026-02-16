from fastapi import APIRouter, HTTPException

from ...services.integration_health_service import IntegrationHealthService


router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/health")
async def integrations_health():
    """Return startup integration health checks (computed once at server launch)."""
    result = IntegrationHealthService.get_cached_health()
    if result is None:
        raise HTTPException(status_code=503, detail="Integration health has not completed yet")
    return result
