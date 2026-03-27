from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.anime_library import AnimeLibraryService


def test_anime_searcher_subprocess_env_preserves_existing_env_and_appends_allocator_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.anime_library.get_media_subprocess_env",
        lambda cmd: {
            "PATH": "/tmp/bin",
            "LD_LIBRARY_PATH": "/tmp/lib",
            "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128",
        },
    )

    env = AnimeLibraryService._anime_searcher_subprocess_env(
        ["pixi", "run", "--locked", "python", "-m", "anime_searcher.cli"]
    )

    assert env["PATH"] == "/tmp/bin"
    assert env["LD_LIBRARY_PATH"] == "/tmp/lib"
    assert env["PYTORCH_CUDA_ALLOC_CONF"] == (
        "max_split_size_mb:128,expandable_segments:True"
    )
