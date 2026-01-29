from fastapi import APIRouter

from .projects import router as projects_router
from .video import router as video_router
from .scenes import router as scenes_router
from .download import router as download_router
from .matching import router as matching_router
from .transcription import router as transcription_router
from .processing import router as processing_router
from .anime import router as anime_router
# TEMPORARILY DISABLED - Subtitle video generation feature
# from .subtitles import router as subtitles_router

api_router = APIRouter(prefix="/api")
api_router.include_router(projects_router)
api_router.include_router(video_router)
api_router.include_router(scenes_router)
api_router.include_router(download_router)
api_router.include_router(matching_router)
api_router.include_router(transcription_router)
api_router.include_router(processing_router)
api_router.include_router(anime_router)
# TEMPORARILY DISABLED - Subtitle video generation feature
# api_router.include_router(subtitles_router)

__all__ = ["api_router"]
