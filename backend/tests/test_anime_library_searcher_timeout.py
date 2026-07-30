import asyncio
import json
from pathlib import Path

import pytest

from app.services.anime_library import AnimeLibraryService


class _DelayedStdout:
    def __init__(self, items: list[tuple[float, bytes]]) -> None:
        self._items = iter(items)

    async def readline(self) -> bytes:
        delay, payload = next(self._items)
        await asyncio.sleep(delay)
        return payload


class _EmptyStderr:
    async def read(self, _size: int) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self, stdout: _DelayedStdout) -> None:
        self.stdout = stdout
        self.stderr = _EmptyStderr()
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_searcher_timeout_resets_after_each_progress_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An active command may exceed the timeout in total without being killed."""
    lines = [
        (
            0.0,
            (
                json.dumps(
                    {
                        "event": "file_progress",
                        "progress": 0.1,
                        "current_file": "episode-1.mp4",
                    }
                )
                + "\n"
            ).encode(),
        ),
        (
            0.04,
            (
                json.dumps(
                    {
                        "event": "file_progress",
                        "progress": 0.2,
                        "current_file": "episode-1.mp4",
                    }
                )
                + "\n"
            ).encode(),
        ),
        (0.04, b""),
    ]
    process = _FakeProcess(_DelayedStdout(lines))

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(AnimeLibraryService, "INDEX_TIMEOUT_SECONDS", 0.07)

    progress = [
        item
        async for item in AnimeLibraryService._stream_searcher_command(
            cmd=["anime-searcher"],
            cwd=tmp_path,
            total_files=1,
            status="indexing",
            progress_start=0.0,
            progress_span=1.0,
        )
    ]

    assert len(progress) == 2
    assert all(item.status != "error" for item in progress)
