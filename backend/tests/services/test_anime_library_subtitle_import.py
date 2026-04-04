"""Tests for subtitle sidecar extraction during library import."""

from __future__ import annotations

import asyncio
import sys
import tempfile
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
    audio_streams: tuple[SourceMediaStream, ...] | None = None,
    subtitle_streams: tuple[SourceMediaStream, ...] = (),
) -> SourceMediaProbe:
    resolved_audio_streams = audio_streams
    if resolved_audio_streams is None:
        resolved_audio_streams = (_audio_stream(index=1, stream_position=0, language="ja"),)
    return SourceMediaProbe(
        source_path=source_path,
        container_suffix=suffix,
        format_name="matroska,webm",
        video_codec=video_codec,
        audio_codec="aac",
        pix_fmt="yuv420p",
        fps=23.976,
        duration=1400.0,
        has_audio=bool(resolved_audio_streams),
        audio_streams=resolved_audio_streams,
        subtitle_streams=subtitle_streams,
    )


def _audio_stream(
    *,
    index: int,
    stream_position: int,
    language: str | None,
    raw_language: str | None = None,
    is_default: bool = False,
) -> SourceMediaStream:
    return SourceMediaStream(
        index=index,
        stream_position=stream_position,
        codec_type="audio",
        codec_name="aac",
        channels=2,
        language=language,
        raw_language=raw_language or language,
        title=None,
        handler_name=None,
        is_default=is_default,
    )


def _ass_subtitle_stream() -> SourceMediaStream:
    return SourceMediaStream(
        index=2,
        stream_position=0,
        codec_type="subtitle",
        codec_name="ass",
        channels=None,
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
        channels=None,
        language="en",
        raw_language="eng",
        title="English (PGS)",
        handler_name=None,
    )


class TestSubtitleExtractionDuringImport(TestCase):
    """Test _prepare_single_source_for_library() subtitle extraction."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _setup_patches(
        self,
        source_path: Path,
        dest_dir: Path,
        *,
        codec: str = "h264",
        probe: SourceMediaProbe | None = None,
        existing_ready: bool = False,
        remux_returncode: int = 0,
        prepared_stem: str | None = None,
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
        resolved_probe = probe or _make_probe(source_path)
        prepared_stem = prepared_stem or source_path.stem
        prepared_probe = _make_probe(
            dest_dir / f"{prepared_stem}.import.tmp.mp4",
            suffix=".mp4",
            video_codec=resolved_probe.video_codec or ("h264" if codec not in {"h264", "hevc"} else codec),
            audio_streams=resolved_probe.audio_streams,
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
            side_effect=[resolved_probe, prepared_probe],
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
        patches["replace"] = patch.object(Path, "replace")
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
            patches["replace"],
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
            patches["replace"],
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
            patches["replace"],
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
        """Readable MP4 source is copied without subtitle sidecar extraction."""
        source = Path("/tmp/test_src/episode.mp4")
        dest_dir = Path("/tmp/test_lib/Anime")
        probe = _make_probe(source, suffix=".mp4", subtitle_streams=())

        patches, mock_write, mock_cmd = self._setup_patches(
            source, dest_dir, codec="h264", probe=probe,
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"] as mock_probe,
            patches["write_sidecar"],
            patches["record_manifest"],
            patches["replace"],
            patch.object(Path, "exists", return_value=True),
            patch("shutil.copy2"),
        ):
            self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        mock_probe.assert_called_once_with(source)
        mock_write.assert_not_awaited()

    def test_unreadable_source_is_rejected_before_fallback_copy(self) -> None:
        """Unreadable source files fail fast instead of being copied as fallback artifacts."""
        source = Path("/tmp/test_src/broken.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        mock_run_cmd = AsyncMock()

        with (
            patch.object(
                AnimeLibraryService,
                "get_primary_video_codec_sync",
                return_value=None,
            ),
            patch.object(
                AnimeLibraryService,
                "_source_matches_prepared_sync",
                return_value=False,
            ),
            patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                return_value=None,
            ),
            patch(
                "app.services.anime_library.run_command",
                mock_run_cmd,
            ),
            patch("shutil.copy2") as mock_copy2,
        ):
            with self.assertRaisesRegex(RuntimeError, "Source file is unreadable: broken.mkv"):
                self._run(
                    AnimeLibraryService._prepare_single_source_for_library(
                        source_path=source,
                        dest_dir=dest_dir,
                    )
                )

        mock_copy2.assert_not_called()
        mock_run_cmd.assert_not_awaited()

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
            patches["replace"],
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
            patches["replace"],
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

    def test_prepare_single_source_for_library_normalizes_unsafe_episode_name(self) -> None:
        source = Path(
            "/tmp/test_src/【English subtitles】Special Animation ‶DEATH HALL＂ [Kw7AZkrvuKc].mkv"
        )
        dest_dir = Path("/tmp/test_lib/Shiyakusho (Death Hall)")
        expected_mp4 = (
            dest_dir
            / "[English subtitles] Special Animation DEATH HALL [Kw7AZkrvuKc].mp4"
        )

        probe = _make_probe(source, subtitle_streams=())
        patches, mock_write, _mock_cmd = self._setup_patches(
            source,
            dest_dir,
            probe=probe,
            prepared_stem=expected_mp4.stem,
        )

        with (
            patches["codec"],
            patches["existing"],
            patches["probe"],
            patches["run_cmd"],
            patches["write_sidecar"],
            patches["record_manifest"] as record_manifest,
            patches["replace"],
            patch.object(Path, "exists", return_value=True),
            patch.object(AnimeLibraryService, "_existing_prepared_library_stems_sync", return_value=set()),
            patch.object(Path, "unlink"),
        ):
            actual, _action, changed = self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        self.assertEqual(actual, expected_mp4)
        self.assertTrue(changed)
        mock_write.assert_not_awaited()
        record_manifest.assert_called_once()
        self.assertEqual(record_manifest.call_args.args[1], expected_mp4)

    def test_mkv_remux_maps_primary_video_and_all_audio_streams_only(self) -> None:
        """Import remux keeps one video stream and every audio stream, not subtitles/data."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        probe = _make_probe(
            source,
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="ja"),
                _audio_stream(index=2, stream_position=1, language="en"),
            ),
            subtitle_streams=(_ass_subtitle_stream(),),
        )
        output_probe = _make_probe(
            dest_dir / "episode.import.tmp.mp4",
            suffix=".mp4",
            audio_streams=probe.audio_streams,
        )
        mock_write_sidecar = AsyncMock()
        mock_run_cmd = AsyncMock(
            return_value=CommandResult(
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
        )

        with (
            patch.object(
                AnimeLibraryService,
                "get_primary_video_codec_sync",
                return_value="h264",
            ),
            patch.object(
                AnimeLibraryService,
                "_source_matches_prepared_sync",
                return_value=False,
            ),
            patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                side_effect=[probe, output_probe],
            ),
            patch(
                "app.services.anime_library.run_command",
                mock_run_cmd,
            ),
            patch.object(
                AnimeLibraryService,
                "_write_subtitle_sidecar",
                mock_write_sidecar,
            ),
            patch.object(
                AnimeLibraryService,
                "_record_source_import_manifest_sync",
            ),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink"),
            patch.object(Path, "replace"),
        ):
            self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        cmd = mock_run_cmd.await_args.args[0]
        self.assertIn("-map", cmd)
        self.assertIn("0:v:0", cmd)
        self.assertIn("0:a?", cmd)
        self.assertIn("-sn", cmd)
        self.assertIn("-dn", cmd)
        self.assertNotIn("0:1", cmd)
        self.assertNotIn("0:2", cmd)

    def test_av1_source_transcodes_to_h264_mp4(self) -> None:
        """Unsupported codecs are transcoded directly to H.264/AAC MP4."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        expected_mp4 = dest_dir / "episode.mp4"
        source_probe = _make_probe(source, video_codec="av1")
        output_probe = _make_probe(
            dest_dir / "episode.import.tmp.mp4",
            suffix=".mp4",
            video_codec="h264",
            audio_streams=source_probe.audio_streams,
        )
        tmp_output_path = dest_dir / "episode.import.tmp.mp4"
        mock_run_cmd = AsyncMock(
            return_value=CommandResult(
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
        )

        def _probe_side_effect(path: Path):
            if path == source:
                return source_probe
            if path == tmp_output_path:
                return output_probe
            raise AssertionError(f"Unexpected probe path: {path}")

        with (
            patch.object(
                AnimeLibraryService,
                "get_primary_video_codec_sync",
                return_value="av1",
            ),
            patch.object(
                AnimeLibraryService,
                "_source_matches_prepared_sync",
                return_value=False,
            ),
            patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                side_effect=_probe_side_effect,
            ),
            patch(
                "app.services.anime_library.run_command",
                mock_run_cmd,
            ),
            patch.object(
                AnimeLibraryService,
                "_write_subtitle_sidecar",
                AsyncMock(),
            ) as mock_write_sidecar,
            patch.object(
                AnimeLibraryService,
                "_record_source_import_manifest_sync",
            ),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink"),
            patch.object(Path, "replace"),
        ):
            actual, action, changed = self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        self.assertEqual(actual, expected_mp4)
        self.assertEqual(action, "Transcoding to H.264 MP4")
        self.assertTrue(changed)
        cmd = mock_run_cmd.await_args.args[0]
        self.assertIn("h264_nvenc", cmd)
        self.assertIn("aac", cmd)
        mock_write_sidecar.assert_not_awaited()

    def test_library_import_h264_mp4_commands_encode_audio_to_aac(self) -> None:
        """Library MP4 transcodes must normalize audio to AAC."""
        source = Path("/tmp/test_src/episode.mkv")
        output = Path("/tmp/test_lib/Anime/episode.mp4")
        probe = _make_probe(source, video_codec="av1")

        gpu_cmd = AnimeLibraryService._build_gpu_library_import_h264_cmd(
            source,
            output,
            source_codec="av1",
            probe=probe,
        )
        cpu_cmd = AnimeLibraryService._build_cpu_library_import_h264_cmd(
            source,
            output,
            probe=probe,
        )

        self.assertEqual(gpu_cmd[gpu_cmd.index("-c:v") + 1], "av1_cuvid")
        self.assertEqual(gpu_cmd[gpu_cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cpu_cmd[cpu_cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cpu_cmd[cpu_cmd.index("-c:a") + 1], "aac")
        self.assertIn(AnimeLibraryService.SOURCE_NORMALIZATION_AUDIO_RATE, gpu_cmd)
        self.assertIn(AnimeLibraryService.SOURCE_NORMALIZATION_AUDIO_RATE, cpu_cmd)

    def test_remux_validation_failure_falls_back_to_h264_mp4_transcode(self) -> None:
        """An unreadable remux retries as a full H.264/AAC MP4 transcode."""
        source = Path("/tmp/test_src/episode.mkv")
        dest_dir = Path("/tmp/test_lib/Anime")
        expected_mp4 = dest_dir / "episode.mp4"
        source_probe = _make_probe(
            source,
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="ja"),
                _audio_stream(index=2, stream_position=1, language="en"),
            ),
            subtitle_streams=(_ass_subtitle_stream(),),
        )
        invalid_output_probe = _make_probe(
            dest_dir / "episode.import.tmp.mp4",
            suffix=".mp4",
            video_codec="h264",
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="ja"),
            ),
        )
        transcoded_output_probe = _make_probe(
            dest_dir / "episode.import.tmp.mp4",
            suffix=".mp4",
            video_codec="h264",
            audio_streams=source_probe.audio_streams,
        )
        tmp_output_path = dest_dir / "episode.import.tmp.mp4"
        tmp_probe_calls = 0
        mock_write_sidecar = AsyncMock()
        mock_run_cmd = AsyncMock(
            side_effect=[
                CommandResult(
                    returncode=0,
                    stdout=b"",
                    stderr=b"",
                ),
                CommandResult(
                    returncode=0,
                    stdout=b"",
                    stderr=b"",
                ),
            ]
        )

        def _probe_side_effect(path: Path):
            nonlocal tmp_probe_calls
            if path == source:
                return source_probe
            if path == tmp_output_path:
                tmp_probe_calls += 1
                if tmp_probe_calls == 1:
                    return invalid_output_probe
                return transcoded_output_probe
            raise AssertionError(f"Unexpected probe path: {path}")

        with (
            patch.object(
                AnimeLibraryService,
                "get_primary_video_codec_sync",
                return_value="h264",
            ),
            patch.object(
                AnimeLibraryService,
                "_source_matches_prepared_sync",
                return_value=False,
            ),
            patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                side_effect=_probe_side_effect,
            ),
            patch(
                "app.services.anime_library.run_command",
                mock_run_cmd,
            ),
            patch.object(
                AnimeLibraryService,
                "_write_subtitle_sidecar",
                mock_write_sidecar,
            ),
            patch.object(
                AnimeLibraryService,
                "_record_source_import_manifest_sync",
            ),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink") as mock_unlink,
            patch.object(Path, "replace") as mock_replace,
            patch("shutil.copy2") as mock_copy2,
        ):
            actual, action, changed = self._run(
                AnimeLibraryService._prepare_single_source_for_library(
                    source_path=source,
                    dest_dir=dest_dir,
                )
            )

        self.assertEqual(actual, expected_mp4)
        self.assertEqual(action, "Transcoding to H.264 MP4")
        self.assertTrue(changed)
        mock_copy2.assert_not_called()
        self.assertEqual(mock_run_cmd.await_count, 2)
        mock_replace.assert_called_once()
        self.assertGreaterEqual(mock_unlink.call_count, 1)
        mock_write_sidecar.assert_awaited_once_with(
            source_path=source,
            normalized_target_path=expected_mp4,
            probe=source_probe,
        )

    def test_failed_h264_mp4_transcode_cleans_up_tmp_mp4_and_raises(self) -> None:
        """A failed H.264 MP4 transcode removes leaked temp output and aborts import."""
        with tempfile.TemporaryDirectory() as tmp_dir_raw:
            tmp_dir = Path(tmp_dir_raw)
            source = tmp_dir / "source" / "episode.mkv"
            dest_dir = tmp_dir / "library" / "Anime"
            source.parent.mkdir(parents=True, exist_ok=True)
            dest_dir.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"source-video")

            source_probe = _make_probe(
                source,
                video_codec="av1",
                subtitle_streams=(),
            )
            tmp_dest = dest_dir / "episode.import.tmp.mp4"
            run_count = 0

            async def _fake_run_command(_cmd, *, timeout_seconds):
                nonlocal run_count
                run_count += 1
                tmp_dest.write_bytes(f"partial-{run_count}".encode("utf-8"))
                return CommandResult(returncode=1, stdout=b"", stderr=b"transcode failed")

            with (
                patch.object(
                    AnimeLibraryService,
                    "get_primary_video_codec_sync",
                    return_value="av1",
                ),
                patch.object(
                    AnimeLibraryService,
                    "_source_matches_prepared_sync",
                    return_value=False,
                ),
                patch.object(
                    AnimeLibraryService,
                    "_probe_media_sync",
                    return_value=source_probe,
                ),
                patch(
                    "app.services.anime_library.run_command",
                    side_effect=_fake_run_command,
                ),
                patch.object(
                    AnimeLibraryService,
                    "_record_source_import_manifest_sync",
                ) as mock_record_manifest,
            ):
                with self.assertRaisesRegex(RuntimeError, "Failed to transcode source to H.264 MP4"):
                    self._run(
                        AnimeLibraryService._prepare_single_source_for_library(
                            source_path=source,
                            dest_dir=dest_dir,
                        )
                    )

            self.assertEqual(run_count, 2)
            self.assertFalse(tmp_dest.exists())
            mock_record_manifest.assert_not_called()

    def test_source_matches_prepared_rejects_unprobeable_file(self) -> None:
        """Prepared-file reuse requires both manifest match and a readable video probe."""
        with tempfile.TemporaryDirectory() as tmp_dir_raw:
            tmp_dir = Path(tmp_dir_raw)
            source = tmp_dir / "episode.mkv"
            prepared = tmp_dir / "episode.mp4"
            source.write_bytes(b"source")
            prepared.write_bytes(b"prepared")
            AnimeLibraryService._record_source_import_manifest_sync(source, prepared)

            with patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                return_value=None,
            ):
                self.assertFalse(
                    AnimeLibraryService._source_matches_prepared_sync(source, prepared)
                )

    def test_source_matches_prepared_rejects_legacy_mov_output(self) -> None:
        """Prepared-file reuse only accepts MP4 library outputs."""
        with tempfile.TemporaryDirectory() as tmp_dir_raw:
            tmp_dir = Path(tmp_dir_raw)
            source = tmp_dir / "episode.mkv"
            prepared = tmp_dir / "episode.mov"
            source.write_bytes(b"source")
            prepared.write_bytes(b"prepared")
            AnimeLibraryService._record_source_import_manifest_sync(source, prepared)

            with patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                return_value=_make_probe(prepared, suffix=".mov", video_codec="h264"),
            ):
                self.assertFalse(
                    AnimeLibraryService._source_matches_prepared_sync(source, prepared)
                )
