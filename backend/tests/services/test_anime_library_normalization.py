import json
import subprocess
import sys
import asyncio
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.anime_library import AnimeLibraryService, SourceMediaProbe, SourceMediaStream


def _audio_stream(
    *,
    index: int,
    stream_position: int,
    language: str | None,
    codec_name: str = "aac",
    is_default: bool = False,
) -> SourceMediaStream:
    return SourceMediaStream(
        index=index,
        stream_position=stream_position,
        codec_type="audio",
        codec_name=codec_name,
        language=language,
        raw_language=language,
        title=None,
        handler_name=None,
        is_default=is_default,
    )


def _make_probe(
    source_path: Path,
    *,
    suffix: str = ".mkv",
    video_codec: str = "h264",
    audio_streams: tuple[SourceMediaStream, ...] = (),
    selected_audio_stream_index: int | None = None,
) -> SourceMediaProbe:
    audio_codec = None
    if selected_audio_stream_index is not None:
        for stream in audio_streams:
            if stream.index == selected_audio_stream_index:
                audio_codec = stream.codec_name
                break
    elif audio_streams:
        audio_codec = audio_streams[0].codec_name
    return SourceMediaProbe(
        source_path=source_path,
        container_suffix=suffix,
        format_name="matroska,webm" if suffix == ".mkv" else "mov,mp4,m4a,3gp,3g2,mj2",
        video_codec=video_codec,
        audio_codec=audio_codec,
        pix_fmt="yuv420p",
        fps=23.976,
        duration=1400.0,
        has_audio=bool(audio_streams),
        audio_streams=audio_streams,
        selected_audio_stream_index=selected_audio_stream_index,
    )


class TestAnimeLibraryNormalization(TestCase):
    def test_probe_media_prefers_av_duration_over_longer_data_stream_tail(self) -> None:
        source_path = Path("/tmp/fake-episode.mp4")

        payload = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "pix_fmt": "yuv420p10le",
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                    "duration": "1430.011000",
                    "tags": {"language": "und", "handler_name": "VideoHandler"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "0/0",
                    "duration": "1430.069705",
                    "tags": {"language": "jpn", "handler_name": "SoundHandler"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "data",
                    "codec_name": "bin_data",
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "0/0",
                    "duration": "1430.440000",
                    "tags": {"language": "eng", "handler_name": "SubtitleHandler"},
                    "disposition": {"default": 0},
                },
            ],
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "1430.440000",
            },
        }

        with patch.object(
            subprocess,
            "run",
            side_effect=lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            ),
        ):
            probe = AnimeLibraryService._probe_media_sync(source_path)

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertEqual(probe.duration, 1430.069705)

    def test_select_preferred_audio_stream_prefers_japanese_then_target_then_english_then_default(self) -> None:
        source_path = Path("/tmp/fake-episode.mkv")

        self.assertEqual(
            AnimeLibraryService.select_preferred_audio_stream(
                _make_probe(
                    source_path,
                    audio_streams=(
                        _audio_stream(index=1, stream_position=0, language="fr"),
                        _audio_stream(index=2, stream_position=1, language="en"),
                    ),
                ),
                target_language="fr",
            ).language,
            "fr",
        )
        self.assertEqual(
            AnimeLibraryService.select_preferred_audio_stream(
                _make_probe(
                    source_path,
                    audio_streams=(
                        _audio_stream(index=1, stream_position=0, language="fr"),
                        _audio_stream(index=2, stream_position=1, language="ja"),
                    ),
                ),
                target_language="fr",
            ).language,
            "ja",
        )
        self.assertEqual(
            AnimeLibraryService.select_preferred_audio_stream(
                _make_probe(
                    source_path,
                    audio_streams=(
                        _audio_stream(index=1, stream_position=0, language="de"),
                        _audio_stream(index=2, stream_position=1, language="en"),
                    ),
                ),
                target_language="fr",
            ).language,
            "en",
        )
        selected = AnimeLibraryService.select_preferred_audio_stream(
            _make_probe(
                source_path,
                audio_streams=(
                    _audio_stream(index=4, stream_position=0, language="de"),
                    _audio_stream(index=6, stream_position=1, language="it", is_default=True),
                ),
            ),
            target_language="fr",
        )
        assert selected is not None
        self.assertEqual(selected.language, "it")
        self.assertTrue(selected.is_default)

    def test_build_source_normalization_plan_uses_target_language_audio_preference(self) -> None:
        source_path = Path("/tmp/fake-episode.mkv")
        source_probe = _make_probe(
            source_path,
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="fr"),
                _audio_stream(index=2, stream_position=1, language="en"),
            ),
        )

        with patch.object(
            AnimeLibraryService,
            "_probe_media_sync",
            return_value=source_probe,
        ):
            plan = AnimeLibraryService._build_source_normalization_plan_sync(
                source_path,
                preferred_audio_language="fr",
            )

        self.assertEqual(plan.probe.selected_audio_stream_index, 1)
        self.assertEqual(plan.probe.audio_codec, "aac")

    def test_is_valid_normalized_probe_rejects_mismatched_selected_audio_language(self) -> None:
        reference_probe = _make_probe(
            Path("/tmp/source.mkv"),
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="ja"),
                _audio_stream(index=2, stream_position=1, language="en"),
            ),
            selected_audio_stream_index=1,
        )
        normalized_probe = _make_probe(
            Path("/tmp/source.mp4"),
            suffix=".mp4",
            video_codec="h264",
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="en"),
            ),
            selected_audio_stream_index=1,
        )

        self.assertFalse(
            AnimeLibraryService._is_valid_normalized_probe(
                normalized_probe,
                reference_probe=reference_probe,
            )
        )

    def test_normalize_source_for_processing_rebuilds_from_original_import_when_audio_policy_changes(self) -> None:
        async def _run() -> None:
            source_path = Path("/tmp/library/episode.mp4")
            original_source_path = Path("/tmp/torrents/episode.mkv")
            source_path.parent.mkdir(parents=True, exist_ok=True)
            original_source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(b"normalized")
            original_source_path.write_bytes(b"original")
            AnimeLibraryService._record_source_import_manifest_sync(original_source_path, source_path)

            current_probe = _make_probe(
                source_path,
                suffix=".mp4",
                video_codec="h264",
                audio_streams=(
                    _audio_stream(index=1, stream_position=0, language="en"),
                ),
                selected_audio_stream_index=1,
            )
            original_probe = _make_probe(
                original_source_path,
                audio_streams=(
                    _audio_stream(index=1, stream_position=0, language="ja"),
                    _audio_stream(index=2, stream_position=1, language="en"),
                ),
                selected_audio_stream_index=1,
            )
            tmp_output_path = source_path.with_name(f"{source_path.stem}.normalize.tmp.mp4")
            normalized_probe = _make_probe(
                tmp_output_path,
                suffix=".mp4",
                video_codec="h264",
                audio_streams=(
                    _audio_stream(index=1, stream_position=0, language="ja"),
                ),
                selected_audio_stream_index=1,
            )

            def _probe_side_effect(path: Path):
                if path == source_path:
                    return current_probe
                if path == original_source_path:
                    return original_probe
                if path == tmp_output_path:
                    return normalized_probe
                raise AssertionError(f"Unexpected probe path: {path}")

            async def _fake_run_command(cmd, *, timeout_seconds):
                tmp_output_path.write_bytes(b"tmp-output")
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 0,
                        "stdout": b"",
                        "stderr": b"",
                    },
                )()

            with (
                patch.object(
                    AnimeLibraryService,
                    "_probe_media_sync",
                    side_effect=_probe_side_effect,
                ),
                patch.object(
                    AnimeLibraryService,
                    "_write_subtitle_sidecar",
                    AsyncMock(),
                ),
                patch.object(
                    AnimeLibraryService,
                    "_postprocess_source_normalization_commit",
                    AsyncMock(),
                ),
                patch.object(
                    AnimeLibraryService,
                    "_run_normalization_command",
                    side_effect=_fake_run_command,
                ) as mock_run_command,
            ):
                result = await AnimeLibraryService.normalize_source_for_processing(
                    source_path,
                    preferred_audio_language="fr",
                )

            self.assertTrue(result.changed)
            self.assertEqual(result.normalized_path, source_path)
            cmd = mock_run_command.await_args.args[0]
            self.assertEqual(cmd[3], str(original_source_path))
            self.assertTrue(original_source_path.exists())

        asyncio.run(_run())
