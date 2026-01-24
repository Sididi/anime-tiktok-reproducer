from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    """Application settings."""

    # Paths
    data_dir: Path = Path(__file__).parent.parent / "data"
    projects_dir: Path = Path(__file__).parent.parent / "data" / "projects"
    cache_dir: Path = Path(__file__).parent.parent / "data" / "cache"

    # anime_searcher
    anime_searcher_path: Path = Path(__file__).parent.parent.parent / "modules" / "anime_searcher"
    anime_library_path: Path = Path(__file__).parent.parent.parent / "modules" / "anime_searcher" / "library"
    sscd_model_path: Path | None = None  # User should set this

    # CORS
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Video settings
    default_fps: float = 30.0

    class Config:
        env_prefix = "ATR_"


settings = Settings()

# Ensure directories exist
settings.projects_dir.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)
