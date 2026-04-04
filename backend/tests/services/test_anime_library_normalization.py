import json
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.anime_library import AnimeLibraryService, SourceMediaProbe, SourceMediaStream
from app.utils.subprocess_runner import CommandResult


def _audio_stream(
    *,
    index: int,
    stream_position: int,
    language: str | None,
    codec_name: str = "aac",
    channels: int | None = 2,
    is_default: bool = False,
    duration: float | None = None,
) -> SourceMediaStream:
    return SourceMediaStream(
        index=index,
        stream_position=stream_position,
        codec_type="audio",
        codec_name=codec_name,
        channels=channels,
        language=language,
        raw_language=language,
        title=None,
        handler_name=None,
        is_default=is_default,
        duration=duration,
    )


def _make_probe(
    source_path: Path,
    *,
    suffix: str = ".mkv",
    video_codec: str = "h264",
    audio_streams: tuple[SourceMediaStream, ...] = (),
    selected_audio_stream_index: int | None = None,
    duration: float = 1400.0,
    video_duration: float | None = None,
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
        duration=duration,
        has_audio=bool(audio_streams),
        audio_streams=audio_streams,
        selected_audio_stream_index=selected_audio_stream_index,
        video_duration=duration if video_duration is None else video_duration,
    )


class TestAnimeLibraryNormalization(TestCase):
    def test_normalize_indexed_episode_stem_transliterates_problematic_unicode(self) -> None:
        stem = "【English subtitles】Special Animation ‶DEATH HALL＂ [Kw7AZkrvuKc]"

        normalized = AnimeLibraryService.normalize_indexed_episode_stem(stem)

        self.assertEqual(
            normalized,
            "[English subtitles] Special Animation DEATH HALL [Kw7AZkrvuKc]",
        )

    def test_normalize_indexed_episode_stem_unique_appends_hash_on_collision(self) -> None:
        normalized = AnimeLibraryService.normalize_indexed_episode_stem_unique(
            "épisode",
            reserved_stems={"episode"},
        )

        self.assertEqual(normalized, "episode__4e236bb2")

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
                    "channels": 2,
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

    def test_build_source_audio_selection_policy_prefers_japanese_and_computes_channel_offset(self) -> None:
        source_path = Path("/tmp/fake-episode.mkv")
        source_probe = _make_probe(
            source_path,
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="fr", channels=2),
                _audio_stream(index=2, stream_position=1, language="ja", channels=2),
            ),
        )

        with patch.object(
            AnimeLibraryService,
            "_probe_media_sync",
            return_value=source_probe,
        ):
            policy = AnimeLibraryService.build_source_audio_selection_policy(
                source_path,
                target_language="fr",
            )

        self.assertEqual(policy.selected_stream_index, 2)
        self.assertEqual(policy.selected_stream_position, 1)
        self.assertEqual(policy.selected_language, "ja")
        self.assertEqual(policy.selected_channel_count, 2)
        self.assertEqual(policy.selected_channel_offset, 2)
        self.assertEqual(policy.channel_type, "stereo")

    def test_build_source_audio_selection_policy_uses_original_audio_metadata_when_languages_are_unknown(self) -> None:
        source_path = Path("/tmp/library/episode-unknown.mp4")
        original_source_path = Path("/tmp/torrents/episode-unknown.mkv")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        original_source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"normalized")
        original_source_path.write_bytes(b"original")

        original_probe = _make_probe(
            original_source_path,
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language="en", channels=2),
                _audio_stream(index=2, stream_position=1, language="ja", channels=2),
            ),
        )
        AnimeLibraryService._record_source_import_manifest_sync(
            original_source_path,
            source_path,
            source_probe=original_probe,
        )

        current_probe = _make_probe(
            source_path,
            suffix=".mp4",
            audio_streams=(
                _audio_stream(index=1, stream_position=0, language=None, channels=2),
                _audio_stream(index=2, stream_position=1, language=None, channels=2),
            ),
        )

        try:
            with patch.object(
                AnimeLibraryService,
                "_probe_media_sync",
                return_value=current_probe,
            ):
                policy = AnimeLibraryService.build_source_audio_selection_policy(
                    source_path,
                    target_language="fr",
                )
        finally:
            source_path.unlink(missing_ok=True)
            original_source_path.unlink(missing_ok=True)
            AnimeLibraryService.get_source_import_manifest_path(source_path).unlink(
                missing_ok=True,
            )

        self.assertEqual(policy.selected_stream_index, 2)
        self.assertEqual(policy.selected_stream_position, 1)
        self.assertEqual(policy.selected_language, "ja")
        self.assertEqual(policy.selected_channel_count, 2)
        self.assertEqual(policy.selected_channel_offset, 2)

    def test_format_media_failure_prefers_meaningful_stderr_tail_over_ffmpeg_banner(self) -> None:
        result = CommandResult(
            returncode=1,
            stdout=b"",
            stderr=(
                b"ffmpeg version n8.1 Copyright (c) 2000-2026 the FFmpeg developers\n"
                b"built with gcc 15.2.1 (GCC) 20260209\n"
                b"configuration: --prefix=/usr\n"
                b"[hevc_metadata @ 0x123] No start code is found.\n"
                b"Error opening output files: Invalid data found when processing input\n"
            ),
        )

        formatted = AnimeLibraryService._format_media_failure(result)

        self.assertIn("Error opening output files", formatted)
        self.assertNotIn("ffmpeg version", formatted)

    def test_format_media_failure_keeps_mp4_muxer_support_error_when_present(self) -> None:
        result = CommandResult(
            returncode=1,
            stdout=b"",
            stderr=(
                b"Stream mapping:\n"
                b"  Stream #0:0 -> #0:0 (av1 (libdav1d) -> h264 (h264_nvenc))\n"
                b"  Stream #0:1 -> #0:1 (aac (native) -> aac (native))\n"
                b"[mp4 @ 0x123] track 1: codec frame size is not set.\n"
                b"[out#0/mp4 @ 0x456] Could not write header (incorrect codec parameters ?): Invalid argument\n"
                b"[vf#0:0 @ 0x789] Terminating thread with return code -22 (Invalid argument)\n"
                b"[out#0/mp4 @ 0x456] Nothing was written into output file, because at least one of its streams received no packets.\n"
                b"Conversion failed!\n"
            ),
        )

        formatted = AnimeLibraryService._format_media_failure(result)

        self.assertIn("Could not write header", formatted)
        self.assertIn("Nothing was written into output file", formatted)
