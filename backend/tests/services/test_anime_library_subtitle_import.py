"""Tests for subtitle sidecar extraction during library import."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.anime_library import AnimeLibraryService, SourceMediaProbe, SourceMediaStream
from app.utils.subprocess_runner import CommandResult


def _make_probe(
    source_path: Path,
    *,
    suffix: str = ".mkv",
    video_codec: str = "h264",
    subtitle_streams: tuple[SourceMediaStream, ...] = (),
) -> SourceMediaProbe:
    return SourceMediaProbe(
        source_path=source_path,
        container_suffix=suffix,
        format_name="matroska,webm",
        video_codec=video_codec,
        audio_codec="aac",
        pix_fmt="yuv420p",
        fps=23.976,
        duration=1400.0,
        has_audio=True,
        audio_streams=(
            SourceMediaStream(
                index=1,
                stream_position=0,
                codec_type="audio",
                codec_name="aac",
                language="ja",
                raw_language="jpn",
                title=None,
                handler_name=None,
            ),
        ),
        subtitle_streams=subtitle_streams,
    )


def _ass_subtitle_stream() -> SourceMediaStream:
    return SourceMediaStream(
        index=2,
        stream_position=0,
        codec_type="subtitle",
        codec_name="ass",
        language="en",
        raw_language="eng",
        title="English",
        handler_name=None,
    )


def _pgs_subtitle_stream() -> SourceMediaStream:
    return SourceMediaStream(
        index=3,
        stream_position=1,
        codec_type="subtitle",
        codec_name="hdmv_pgs_subtitle",
        language="en",
        raw_language="eng",
        title="English (PGS)",
        handler_name=None,
    )


class TestSubtitleExtractionDuringImport(TestCase):
    """Test _prepare_single_source_for_library() subtitle extraction."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _setup_patches(
        self,
        source_path: Path,
        dest_dir: Path,
        *,
        codec: str = "h264",
        probe: SourceMediaProbe | None = None,
        existing_ready: bool = False,
        remux_returncode: int = 0,
    ):
        """Set up common mocks and return (patches_dict, mock_write_sidecar, mock_run_cmd)."""
        patches = {}
        mock_write_sidecar = AsyncMock()
        mock_run_cmd = AsyncMock(
            return_value=CommandResult(
                returncode=remux_returncode,
                stdout=b"",
                stderr=b"",
            )
        )

        patches["codec"] = patch.object(
            AnimeLibraryService,
            "get_primary_video_codec_sync",
            return_value=codec,
        )
        patches["existing"] = patch.object(
            AnimeLibraryService,
            "_source_matches_prepared_sync",
            return_value=existing_ready,
        )
        patches["probe"] = patch.object(
            AnimeLibraryService,
            "_probe_media_sync",
            return_value=probe,
        )
        patches["run_cmd"] = patch(
            "app.services.anime_library.run_command",
            mock_run_cmd,
        )
        patches["write_sidecar"] = patch.object(
            AnimeLibraryService,
            "_write_subtitle_sidecar",
            mock_write_sidecar,
        )
        patches["record_manifest"] = patch.object(
            AnimeLibraryService,
            "_record_source_import_manifest_sync",
        )
        return patches, mock_write_sidecar, mock_run_cmd

    def test_mkv_with_ass_subtitles_creates_sidecar(self) -> None:
        """MKV with ASS subtitles → sidecar extracted before source deletion."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        expected_mp4 = dest_dir / "episode.mp4"

        probe = _make_probe(
            source,
            subtitle_streams=(_ass_subtitle_stream(),),
        )
        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, probe=probe,
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"],
            patches["run_cmd"],
            patches["write_sidecar"],
            patches["record_manifest"],
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink"),
        ):
            actual, action, changed = self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        self.assertEqual(actual, expected_mp4)
        self.assertTrue(changed)
        mock_write.assert_awaited_once_with(
            source_path=source,
            normalized_target_path=expected_mp4,
            probe=probe,
        )

    def test_mkv_without_subtitles_skips_sidecar(self) -> None:
        """MKV with no subtitle streams → no sidecar extraction."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")

        probe = _make_probe(source, subtitle_streams=())
        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, probe=probe,
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"],
            patches["run_cmd"],
            patches["write_sidecar"],
            patches["record_manifest"],
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink"),
        ):
            self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        mock_write.assert_not_awaited()

    def test_sidecar_extraction_failure_does_not_block_import(self) -> None:
        """Subtitle extraction error → logged warning, import proceeds."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")

        probe = _make_probe(
            source,
            subtitle_streams=(_ass_subtitle_stream(),),
        )
        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, probe=probe,
        )
        mock_write.side_effect = RuntimeError("ffmpeg subtitle extraction failed")

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"],
            patches["run_cmd"],
            patches["write_sidecar"],
            patches["record_manifest"],
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink"),
            self.assertLogs("uvicorn.error", level="WARNING") as logs,
        ):
            actual, action, changed = self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        # Import succeeded despite sidecar error.
        self.assertTrue(changed)
        self.assertIn("Failed to extract subtitle sidecar", logs.output[0])

    def test_mp4_source_skips_subtitle_extraction(self) -> None:
        """Non-MKV (already MP4) → no probe for subtitles, no extraction."""
        source = Path("/tmp/test_src/episode.mp4")
        dest_dir = Path("/tmp/test_lib/Anime")

        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, codec="h264",
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"] as mock_probe,
            patches["write_sidecar"],
            patches["record_manifest"],
            patch.object(Path, "exists", return_value=True),
            patch("shutil.copy2"),
        ):
            self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        # _probe_media_sync should NOT be called for non-MKV.
        mock_probe.assert_not_called()
        mock_write.assert_not_awaited()

    def test_mkv_with_pgs_subtitles_extracts_without_render_windows(self) -> None:
        """MKV with PGS image subtitles → sidecar extracted, no PNG rendering."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        expected_mp4 = dest_dir / "episode.mp4"

        probe = _make_probe(
            source,
            subtitle_streams=(_pgs_subtitle_stream(),),
        )
        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, probe=probe,
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"],
            patches["run_cmd"],
            patches["write_sidecar"],
            patches["record_manifest"],
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink"),
        ):
            self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        # Sidecar written without subtitle_image_render_windows (no PNG rendering).
        mock_write.assert_awaited_once_with(
            source_path=source,
            normalized_target_path=expected_mp4,
            probe=probe,
        )

    def test_existing_ready_skips_everything(self) -> None:
        """Already-prepared episode → no probe, no extraction, no remux."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        expected_mp4 = dest_dir / "episode.mp4"

        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, existing_ready=True,
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"] as mock_probe,
            patches["run_cmd"],
            patches["write_sidecar"],
            patches["record_manifest"],
        ):
            actual, action, changed = self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        self.assertEqual(actual, expected_mp4)
        self.assertEqual(action, "Using existing")
        self.assertFalse(changed)
        mock_probe.assert_not_called()
        mock_write.assert_not_awaited()
        mock_cmd.assert_not_awaited()
