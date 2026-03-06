from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.anime_library as anime_library_module
from app.services.anime_library import AnimeLibraryService
from app.utils.subprocess_runner import CommandResult


class _FakeReadableStream:
    def __init__(self, *, lines: list[bytes] | None = None, payload: bytes = b"") -> None:
        self._lines = list(lines or [])
        self._payload = payload
        self._read_done = False

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    async def read(self) -> bytes:
        if self._read_done:
            return b""
        self._read_done = True
        return self._payload


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout_lines: list[bytes] | None = None,
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeReadableStream(lines=stdout_lines)
        self.stderr = _FakeReadableStream(payload=stderr)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


async def _collect_progress(async_iterable) -> list:
    return [event async for event in async_iterable]


async def _fake_sleep(_seconds: float) -> None:
    return None


async def _fake_create_subprocess_exec(*args, **kwargs):
    return _FakeProcess(stdout_lines=[b"indexing episode\n"], returncode=0)


async def _fake_ensure_episode_manifest(*, force_refresh: bool = False):
    return {}


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    library_path: Path,
    searcher_path: Path,
    codec: str,
) -> None:
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        staticmethod(lambda: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_anime_searcher_path",
        staticmethod(lambda: searcher_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_primary_video_codec_sync",
        staticmethod(lambda _path: codec),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "ensure_episode_manifest",
        staticmethod(_fake_ensure_episode_manifest),
    )
    monkeypatch.setattr(anime_library_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        anime_library_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )


@pytest.mark.asyncio
async def test_index_anime_copies_av1_source_without_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_folder = tmp_path / "source"
    source_folder.mkdir()
    source_file = source_folder / "episode.mkv"
    source_file.write_bytes(b"av1-source")
    library_path = tmp_path / "library"
    searcher_path = tmp_path / "searcher"
    searcher_path.mkdir()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("run_command should not be used for AV1 import copies")

    _patch_common(
        monkeypatch,
        library_path=library_path,
        searcher_path=searcher_path,
        codec="av1",
    )
    monkeypatch.setattr(anime_library_module, "run_command", fail_if_called)

    progress = await _collect_progress(
        AnimeLibraryService.index_anime(source_folder=source_folder, anime_name="Mirai")
    )

    assert progress[-1].status == "complete"
    assert any(event.message == "Copying AV1 source episode.mkv" for event in progress)
    assert (library_path / "Mirai" / "episode.mkv").read_bytes() == b"av1-source"
    assert not (library_path / "Mirai" / "episode.mp4").exists()


@pytest.mark.asyncio
async def test_index_anime_remuxes_non_av1_mkv_with_stream_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_folder = tmp_path / "source"
    source_folder.mkdir()
    source_file = source_folder / "episode.mkv"
    source_file.write_bytes(b"source")
    library_path = tmp_path / "library"
    searcher_path = tmp_path / "searcher"
    searcher_path.mkdir()
    expected_dest = library_path / "Mirai" / "episode.mp4"
    calls: list[list[str]] = []

    async def fake_run_command(cmd, *, cwd=None, timeout_seconds=None):
        calls.append(list(cmd))
        assert list(cmd) == [
            "ffmpeg",
            "-y",
            "-i",
            str(source_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(expected_dest),
        ]
        expected_dest.parent.mkdir(parents=True, exist_ok=True)
        expected_dest.write_bytes(b"mp4-data")
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    _patch_common(
        monkeypatch,
        library_path=library_path,
        searcher_path=searcher_path,
        codec="h264",
    )
    monkeypatch.setattr(anime_library_module, "run_command", fake_run_command)

    progress = await _collect_progress(
        AnimeLibraryService.index_anime(source_folder=source_folder, anime_name="Mirai")
    )

    assert progress[-1].status == "complete"
    assert len(calls) == 1
    assert expected_dest.read_bytes() == b"mp4-data"
    assert not (library_path / "Mirai" / "episode.mkv").exists()


@pytest.mark.asyncio
async def test_index_anime_keeps_in_place_source_when_remux_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_folder = tmp_path / "Mirai"
    source_folder.mkdir()
    source_file = source_folder / "episode.mkv"
    source_file.write_bytes(b"original-mkv")
    searcher_path = tmp_path / "searcher"
    searcher_path.mkdir()

    async def fake_run_command(cmd, *, cwd=None, timeout_seconds=None):
        return CommandResult(returncode=1, stdout=b"", stderr=b"remux failed")

    _patch_common(
        monkeypatch,
        library_path=tmp_path,
        searcher_path=searcher_path,
        codec="h264",
    )
    monkeypatch.setattr(anime_library_module, "run_command", fake_run_command)

    progress = await _collect_progress(
        AnimeLibraryService.index_anime(source_folder=source_folder, anime_name="Mirai")
    )

    assert progress[-1].status == "complete"
    assert source_file.read_bytes() == b"original-mkv"
    assert not (source_folder / "episode.mp4").exists()
