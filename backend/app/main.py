import os

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

app = FastAPI(
    title="Anime TikTok Reproducer",
    description="Web app to remaster TikToks by finding anime source clips",
    version="0.1.0",
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
