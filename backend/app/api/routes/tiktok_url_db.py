"""API routes for TikTok URL duplicate detection."""

from fastapi import APIRouter
from pydantic import BaseModel

from ...services.tiktok_url_db_service import TikTokUrlDbService

router = APIRouter(prefix="/tiktok-urls", tags=["tiktok-urls"])


class CheckUrlRequest(BaseModel):
    url: str


@router.post("/check")
async def check_url(request: CheckUrlRequest):
    """Check if a TikTok URL has been used before."""
    return await TikTokUrlDbService.check(request.url)
