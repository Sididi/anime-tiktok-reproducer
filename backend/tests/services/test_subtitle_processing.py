import asyncio
import json
import sys
import zipfile
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.models import SceneMatch
from app.models.project import Project
from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.anime_library import (
    AnimeLibraryService,
    SourceAudioSelectionPolicy,
    SourceMediaProbe,
    SourceMediaStream,
)
from app.services.export_service import ExportService
from app.services.forced_alignment import ForcedAlignmentService
from app.services.gap_resolution import GapResolutionService
from app.services.otio_timing import FrameRateInfo
from app.services.processing import ProcessingService, ResolvedSceneSource
from app.services.project_service import ProjectService
from app.services.script_automation_service import ScriptAutomationService
from app.services.voice_config_service import VoiceConfigService


def _build_sentence(prefix: str, *, repeat_count: int = 28) -> str:
    return f"{prefix} " + " ".join(["story"] * repeat_count) + "."


def _build_source_probe(
    source_path: Path,
    *,
    codec_name: str,
    channels: int,
) -> SourceMediaProbe:
    return SourceMediaProbe(
        source_path=source_path,
        container_suffix=".mp4",
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        audio_codec=codec_name,
        pix_fmt="yuv420p",
        fps=23.976,
        duration=1400.0,
        has_audio=True,
        audio_streams=(
            SourceMediaStream(
                index=1,
                stream_position=0,
                codec_type="audio",
                codec_name=codec_name,
                channels=channels,
                language="ja",
                raw_language="jpn",
                title=None,
                handler_name=None,
                is_default=True,
            ),
        ),
    )


def _capture_generated_scene_payload(
    monkeypatch: pytest.MonkeyPatch,
    transcription: Transcription,
    resolved_scene_sources: dict[int, ResolvedSceneSource],
) -> list[dict]:
    captured: dict[str, list[dict]] = {}

    def _fake_render(cls, **kwargs):
        captured["scenes"] = kwargs["scenes"]
        return "// jsx"

    monkeypatch.setattr(
        ProcessingService,
        "_render_jsx_from_template",
        classmethod(_fake_render),
    )

    jsx = ProcessingService.generate_jsx_script(
        Project(id="p-raw-timing", output_language="fr"),
        transcription,
        matches=[],
        resolved_scene_sources=resolved_scene_sources,
    )

    assert jsx == "// jsx"
    return captured["scenes"]


def test_normalize_external_subtitle_text_strips_markup_and_ass_controls():
    markup = '<font face="Trebuchet MS" size="22"><b>Je l\'ai rejoint,</b></font>'
    assert ProcessingService._normalize_external_subtitle_text(markup) == "Je l'ai rejoint,"

    ass_markup = r"{\an8}Texte\Nsuite &amp; fin"
    assert ProcessingService._normalize_external_subtitle_text(ass_markup) == "Texte suite & fin"


def test_resolve_source_reference_sanitizes_unresolved_absolute_episode_ref(
    monkeypatch: pytest.MonkeyPatch,
):
    stale_episode = (
        "/tmp/stale-library/"
        "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.04."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit].mp4"
    )

    monkeypatch.setattr(
        GapResolutionService,
        "resolve_episode_path",
        classmethod(lambda cls, *_args, **_kwargs: None),
    )

    resolved_path, clip_name = ProcessingService._resolve_source_reference(
        stale_episode,
        library_type="anime",
    )

    assert resolved_path == Path(stale_episode)
    assert (
        clip_name
        == "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.04."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit]"
    )
    assert "/" not in clip_name
    assert "\\" not in clip_name


def test_resolve_scene_sources_sanitizes_confirmed_no_match_absolute_episode_ref(
    monkeypatch: pytest.MonkeyPatch,
):
    stale_episode = (
        "/tmp/stale-library/"
        "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.01."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit].mp4"
    )

    monkeypatch.setattr(
        GapResolutionService,
        "resolve_episode_path",
        classmethod(lambda cls, *_args, **_kwargs: None),
    )

    match = SceneMatch(
        scene_index=37,
        episode=stale_episode,
        start_time=8.0,
        end_time=24.5,
        confidence=1.0,
        speed_ratio=0.503,
        confirmed=True,
        was_no_match=True,
        merged_from=[41, 42, 43, 44],
    )

    resolved = ProcessingService.resolve_scene_sources(
        [match],
        FrameRateInfo(timebase=24, ntsc=True),
        library_type="anime",
    )

    assert resolved[37].source_path == Path(stale_episode)
    assert (
        resolved[37].clip_name
        == "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.01."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit]"
    )
    assert "/" not in resolved[37].clip_name
    assert "\\" not in resolved[37].clip_name


def test_generate_jsx_script_sanitizes_path_like_clip_names_in_scene_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="hello",
                words=[
                    Word(
                        text="hello",
                        start=0.0,
                        end=0.5,
                        confidence=1.0,
                    )
                ],
                start_time=0.0,
                end_time=0.5,
                is_raw=False,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=Path("/tmp/source-episode.mp4"),
            clip_name=(
                "/tmp/stale-library/"
                "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.04."
                "[BD.1080p.Dual.Audio.x265.HEVC.10bit].mp4"
            ),
            source_in_frame=100,
            source_out_frame=112,
            source_in_seconds=100 * float(Fraction(1001, 24000)),
            source_out_seconds=112 * float(Fraction(1001, 24000)),
            source_duration_seconds=12 * float(Fraction(1001, 24000)),
            used_alternative=False,
        )
    }

    scenes = _capture_generated_scene_payload(
        monkeypatch,
        transcription,
        resolved_scene_sources,
    )

    assert len(scenes) == 1
    assert (
        scenes[0]["clipName"]
        == "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.04."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit]"
    )
    assert "/" not in scenes[0]["clipName"]
    assert "\\" not in scenes[0]["clipName"]


def test_tts_segmentation_preserves_scene_fragments_for_hard_split(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 20)

    scene_text = "forces, he was ready to pay the price. After this crazy race,"
    payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 35,
                "text": scene_text,
            }
        ],
    }

    segments = ScriptAutomationService._segment_scenes_for_tts_payload(payload)

    assert len(segments) == 2
    assert segments[0]["scene_indices"] == [35]
    assert segments[1]["scene_indices"] == [35]
    assert segments[0]["scene_fragments"][0]["scene_index"] == 35
    assert segments[1]["scene_fragments"][0]["scene_index"] == 35

    reconstructed = "".join(
        fragment["text"]
        for segment in segments
        for fragment in segment["scene_fragments"]
        if fragment["scene_index"] == 35
    )
    assert reconstructed == scene_text


def test_fragment_based_alignment_maps_one_scene_across_adjacent_segments(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 20)

    scene_text = "forces, he was ready to pay the price. After this crazy race,"
    script_payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 35,
                "text": scene_text,
            }
        ],
    }

    segments = ScriptAutomationService._segment_scenes_for_tts_payload(script_payload)
    combined_scene_words: list[Word] = []
    current_time = 0.0

    for segment in segments:
        segment_words: list[Word] = []
        for token in str(segment["text"]).split():
            segment_words.append(
                Word(
                    text=token,
                    start=current_time,
                    end=current_time + 0.1,
                    confidence=1.0,
                )
            )
            current_time += 0.12

        report, scene_payload = ForcedAlignmentService._map_segment_words_to_scenes(
            segment=segment,
            segment_words=segment_words,
            script_payload=script_payload,
        )

        assert report["coverage"] == 1.0
        assert report["scene_fragments"] == segment["scene_fragments"]
        combined_scene_words.extend(scene_payload[35])

    assert [word.text for word in combined_scene_words] == scene_text.split()


def test_tts_segmentation_uses_smaller_v3_target_while_preserving_sentence_boundaries():
    script_payload = {
        "language": "fr",
        "scenes": [
            {"scene_index": 1, "text": _build_sentence("One")},
            {"scene_index": 2, "text": _build_sentence("Two")},
            {"scene_index": 3, "text": _build_sentence("Three")},
            {"scene_index": 4, "text": _build_sentence("Four")},
            {"scene_index": 5, "text": _build_sentence("Five")},
            {"scene_index": 6, "text": _build_sentence("Six")},
            {"scene_index": 7, "text": _build_sentence("Seven")},
            {"scene_index": 8, "text": _build_sentence("Eight")},
        ],
    }

    v2_segments = ScriptAutomationService._segment_scenes_for_tts_payload(
        script_payload,
        model_id="eleven_multilingual_v2",
    )
    v3_segments = ScriptAutomationService._segment_scenes_for_tts_payload(
        script_payload,
        model_id="eleven_v3",
    )

    assert len(v2_segments) == 2
    assert v2_segments[0]["scene_indices"] == [1, 2, 3, 4]
    assert v2_segments[1]["scene_indices"] == [5, 6, 7, 8]
    assert len(v3_segments) == 3
    assert v3_segments[0]["scene_indices"] == [1, 2, 3]
    assert v3_segments[1]["scene_indices"] == [4, 5, 6]
    assert v3_segments[2]["scene_indices"] == [7, 8]
    assert all(str(segment["text"]).endswith(".") for segment in [*v2_segments, *v3_segments])


def test_tts_segmentation_v3_target_does_not_close_before_minimum():
    script_payload = {
        "language": "fr",
        "scenes": [
            {"scene_index": 1, "text": _build_sentence("One")},
            {"scene_index": 2, "text": _build_sentence("Two")},
            {"scene_index": 3, "text": _build_sentence("Three")},
        ],
    }

    segments = ScriptAutomationService._segment_scenes_for_tts_payload(
        script_payload,
        model_id="eleven_v3",
    )

    assert len(segments) == 1
    assert segments[0]["scene_indices"] == [1, 2, 3]


def test_tts_segmentation_v3_hard_cap_still_prefers_sentence_boundary(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 120)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 120)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 60)
    monkeypatch.setattr(ScriptAutomationService, "TTS_V3_TARGET", 60)

    first_sentence = _build_sentence("One", repeat_count=8)
    second_sentence = _build_sentence("Two", repeat_count=8)
    third_sentence = _build_sentence("Three", repeat_count=8)
    scene_text = f"{first_sentence} {second_sentence} {third_sentence}"
    payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 35,
                "text": scene_text,
            }
        ],
    }

    segments = ScriptAutomationService._segment_scenes_for_tts_payload(
        payload,
        model_id="eleven_v3",
    )

    assert len(segments) == 2
    assert segments[0]["text"] == f"{first_sentence} {second_sentence}"
    assert segments[1]["text"] == third_sentence
    assert segments[0]["text"].endswith(".")
    assert segments[1]["text"].endswith(".")


def test_validate_manifest_upgrades_legacy_segment_indices_when_text_matches(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 35)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 35)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 18)

    script_payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 1,
                "text": "Intro line",
            },
            {
                "scene_index": 2,
                "text": "Second scene keeps going well past the segment boundary.",
            },
        ],
    }

    expected_manifest = ForcedAlignmentService.build_single_audio_manifest(script_payload=script_payload)
    legacy_manifest = {
        "version": 1,
        "mode": "audio_parts",
        "language": "fr",
        "segments": [
            {
                **segment,
                "scene_indices": [1] if segment["id"] == 1 else list(segment["scene_indices"]),
            }
            for segment in expected_manifest["segments"]
        ],
    }
    for segment in legacy_manifest["segments"]:
        segment.pop("scene_fragments", None)

    validated = ForcedAlignmentService._validate_manifest_against_script(
        manifest=legacy_manifest,
        script_payload=script_payload,
    )

    assert validated["segments"][0]["scene_indices"] == [1, 2]
    assert any(
        fragment["scene_index"] == 2
        for fragment in validated["segments"][0]["scene_fragments"]
    )


def test_save_upload_manifest_uses_project_voice_model_for_segment_count(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(settings, "elevenlabs_model_id", "eleven_multilingual_v2")
    monkeypatch.setattr(
        VoiceConfigService,
        "get_voice",
        lambda voice_key: SimpleNamespace(
            elevenlabs_voice_id="voice-id",
            voice_settings={},
            model_id="eleven_v3",
        ),
    )

    project = Project(id="proj-v3-manifest", voice_key="voice-v3")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)

    script_payload = {
        "language": "fr",
        "scenes": [
            {"scene_index": 1, "text": _build_sentence("One")},
            {"scene_index": 2, "text": _build_sentence("Two")},
            {"scene_index": 3, "text": _build_sentence("Three")},
            {"scene_index": 4, "text": _build_sentence("Four")},
            {"scene_index": 5, "text": _build_sentence("Five")},
            {"scene_index": 6, "text": _build_sentence("Six")},
            {"scene_index": 7, "text": _build_sentence("Seven")},
            {"scene_index": 8, "text": _build_sentence("Eight")},
        ],
    }

    manifest = ForcedAlignmentService.save_upload_manifest(
        project.id,
        script_payload=script_payload,
        mode="audio_parts",
        stored_part_paths=[
            "tts_parts/part_0001.wav",
            "tts_parts/part_0002.wav",
            "tts_parts/part_0003.wav",
        ],
    )

    assert len(manifest["segments"]) == 3


def test_raw_scene_image_render_plan_prefers_library_sidecar_without_probe(
    monkeypatch,
    tmp_path,
):
    source_path = tmp_path / "episode.mkv"
    normalized_source_path = source_path.with_suffix(".mp4")
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(normalized_source_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(normalized_source_path),
                "generated_from": str(source_path),
                "subtitle_streams": [
                    {
                        "stream_index": 5,
                        "stream_position": 3,
                        "codec_name": "hdmv_pgs_subtitle",
                        "language": "fr",
                        "raw_language": "fre",
                        "title": "French",
                        "kind": "image",
                        "asset_filename": "subtitle_stream_03_fr.sup",
                        "cue_manifest_filename": "subtitle_stream_03_fr.cues.json",
                        "status": "ok",
                        "error": None,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    def _probe_should_not_run(_source_path):
        raise AssertionError("probe_source_media_sync should not run when a sidecar exists")

    monkeypatch.setattr(AnimeLibraryService, "probe_source_media_sync", _probe_should_not_run)

    project = Project(id="p-sidecar", output_language="fr")
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=0.0,
                end_time=2.0,
                is_raw=True,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=source_path,
            clip_name="episode",
            source_in_frame=30,
            source_out_frame=66,
            source_in_seconds=1.25,
            source_out_seconds=2.75,
            source_duration_seconds=1.5,
        )
    }

    render_plan = asyncio.run(
        ProcessingService._build_raw_scene_image_render_plan(
            project,
            transcription,
            resolved_scene_sources,
        )
    )

    assert render_plan == {
        source_path: {
            3: [(1.25, 2.75)],
        }
    }


def test_collect_raw_scene_source_subtitles_returns_text_entries_for_text_sidecar(
    tmp_path,
):
    source_path = tmp_path / "episode.mkv"
    normalized_source_path = source_path.with_suffix(".mp4")
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(normalized_source_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(normalized_source_path),
                "generated_from": str(source_path),
                "subtitle_streams": [
                    {
                        "stream_index": 2,
                        "stream_position": 0,
                        "codec_name": "subrip",
                        "language": "fr",
                        "raw_language": "fre",
                        "title": "French",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_00_fr.srt",
                        "status": "ok",
                        "error": None,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_00_fr.srt").write_text(
        "\n".join(
            [
                "1",
                "00:00:09,500 --> 00:00:10,400",
                "Avant",
                "",
                "2",
                "00:00:10,600 --> 00:00:11,200",
                "Pendant",
                "",
                "3",
                "00:00:11,900 --> 00:00:12,300",
                "<b>Fin</b>",
                "",
            ]
        ),
        encoding="utf-8",
    )

    project = Project(id="p-text-sidecar", output_language="fr")
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=5.0,
                end_time=7.0,
                is_raw=True,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=source_path,
            clip_name="episode",
            source_in_frame=240,
            source_out_frame=288,
            source_in_seconds=10.0,
            source_out_seconds=12.0,
            source_duration_seconds=2.0,
        )
    }

    text_entries, image_entries = asyncio.run(
        ProcessingService._collect_raw_scene_source_subtitles(
            project,
            transcription,
            resolved_scene_sources,
            tmp_path / "output",
        )
    )

    assert image_entries == []
    assert [(round(entry.start, 1), round(entry.end, 1), entry.text) for entry in text_entries] == [
        (5.0, 5.4, "Avant"),
        (5.6, 6.2, "Pendant"),
        (6.9, 7.0, "Fin"),
    ]


def test_collect_raw_scene_source_subtitles_prefers_overlapping_dialogue_over_non_overlapping_signs(
    tmp_path,
):
    source_path = tmp_path / "episode.mkv"
    normalized_source_path = source_path.with_suffix(".mp4")
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(normalized_source_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(normalized_source_path),
                "generated_from": str(source_path),
                "subtitle_streams": [
                    {
                        "stream_index": 2,
                        "stream_position": 0,
                        "codec_name": "ass",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Signs",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_00_en.srt",
                        "status": "ok",
                        "error": None,
                    },
                    {
                        "stream_index": 3,
                        "stream_position": 1,
                        "codec_name": "ass",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Dialogue",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_01_en.srt",
                        "status": "ok",
                        "error": None,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_00_en.srt").write_text(
        "1\n00:00:08,000 --> 00:00:08,400\nSIGN\n",
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_01_en.srt").write_text(
        "1\n00:00:10,200 --> 00:00:10,800\nDialogue\n",
        encoding="utf-8",
    )

    project = Project(id="p-dialogue-sidecar", output_language="de")
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=5.0,
                end_time=6.0,
                is_raw=True,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=source_path,
            clip_name="episode",
            source_in_frame=240,
            source_out_frame=264,
            source_in_seconds=10.0,
            source_out_seconds=11.0,
            source_duration_seconds=1.0,
        )
    }

    text_entries, image_entries = asyncio.run(
        ProcessingService._collect_raw_scene_source_subtitles(
            project,
            transcription,
            resolved_scene_sources,
            tmp_path / "output",
        )
    )

    assert image_entries == []
    assert [(round(entry.start, 1), round(entry.end, 1), entry.text) for entry in text_entries] == [
        (5.2, 5.8, "Dialogue"),
    ]


def test_collect_raw_scene_source_subtitles_merges_same_language_tracks_with_priority_and_dedup(
    tmp_path,
):
    source_path = tmp_path / "episode.mkv"
    normalized_source_path = source_path.with_suffix(".mp4")
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(normalized_source_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(normalized_source_path),
                "generated_from": str(source_path),
                "subtitle_streams": [
                    {
                        "stream_index": 2,
                        "stream_position": 0,
                        "codec_name": "ass",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Dialogue",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_00_en.srt",
                        "status": "ok",
                        "error": None,
                    },
                    {
                        "stream_index": 3,
                        "stream_position": 1,
                        "codec_name": "ass",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English SDH",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_01_en.srt",
                        "status": "ok",
                        "error": None,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_00_en.srt").write_text(
        "\n".join(
            [
                "1",
                "00:00:10,000 --> 00:00:10,800",
                "Hello",
                "",
                "2",
                "00:00:11,100 --> 00:00:11,400",
                "Next",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_01_en.srt").write_text(
        "\n".join(
            [
                "1",
                "00:00:10,000 --> 00:00:10,800",
                "Hello",
                "",
                "2",
                "00:00:10,400 --> 00:00:11,200",
                "[Door slams]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    project = Project(id="p-fusion-sidecar", output_language="de")
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=5.0,
                end_time=6.5,
                is_raw=True,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=source_path,
            clip_name="episode",
            source_in_frame=240,
            source_out_frame=276,
            source_in_seconds=10.0,
            source_out_seconds=11.5,
            source_duration_seconds=1.5,
        )
    }

    text_entries, image_entries = asyncio.run(
        ProcessingService._collect_raw_scene_source_subtitles(
            project,
            transcription,
            resolved_scene_sources,
            tmp_path / "output",
        )
    )

    assert image_entries == []
    assert [(round(entry.start, 1), round(entry.end, 1), entry.text) for entry in text_entries] == [
        (5.0, 5.8, "Hello"),
        (5.8, 6.1, "[Door slams]"),
        (6.1, 6.4, "Next"),
    ]


def test_collect_raw_scene_source_subtitles_prefers_text_over_overlapping_image_cues(
    tmp_path,
):
    source_path = tmp_path / "episode.mkv"
    normalized_source_path = source_path.with_suffix(".mp4")
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(normalized_source_path)
    cue_dir = sidecar_dir / "subtitle_stream_01_en_cues"
    cue_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(normalized_source_path),
                "generated_from": str(source_path),
                "subtitle_streams": [
                    {
                        "stream_index": 2,
                        "stream_position": 0,
                        "codec_name": "ass",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Dialogue",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_00_en.srt",
                        "status": "ok",
                        "error": None,
                    },
                    {
                        "stream_index": 3,
                        "stream_position": 1,
                        "codec_name": "hdmv_pgs_subtitle",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Signs",
                        "kind": "image",
                        "asset_filename": "subtitle_stream_01_en.sup",
                        "cue_manifest_filename": "subtitle_stream_01_en.cues.json",
                        "status": "ok",
                        "error": None,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_00_en.srt").write_text(
        "1\n00:00:10,200 --> 00:00:10,600\nText\n",
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_01_en.sup").write_bytes(b"sup")
    cue_asset = cue_dir / "cue_0001.png"
    cue_asset.write_bytes(b"png")
    (sidecar_dir / "subtitle_stream_01_en.cues.json").write_text(
        json.dumps(
            {
                "cues": [
                    {
                        "cue_index": 1,
                        "start": 10.0,
                        "end": 11.0,
                        "asset_filename": "subtitle_stream_01_en_cues/cue_0001.png",
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    project = Project(id="p-text-image-sidecar", output_language="de")
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=5.0,
                end_time=6.0,
                is_raw=True,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=source_path,
            clip_name="episode",
            source_in_frame=240,
            source_out_frame=264,
            source_in_seconds=10.0,
            source_out_seconds=11.0,
            source_duration_seconds=1.0,
        )
    }

    text_entries, image_entries = asyncio.run(
        ProcessingService._collect_raw_scene_source_subtitles(
            project,
            transcription,
            resolved_scene_sources,
            tmp_path / "output",
        )
    )

    assert [(round(entry.start, 1), round(entry.end, 1), entry.text) for entry in text_entries] == [
        (5.2, 5.6, "Text"),
    ]
    assert [(round(entry.start, 1), round(entry.end, 1)) for entry in image_entries] == [
        (5.0, 5.2),
        (5.6, 6.0),
    ]
    assert len({entry.relative_asset_path for entry in image_entries}) == 1
    assert (tmp_path / "output" / "raw_scene_subtitles" / "manifest.json").exists()


def test_build_raw_scene_image_render_plan_uses_only_overlapping_image_tracks_from_selected_language(
    tmp_path,
):
    source_path = tmp_path / "episode.mkv"
    normalized_source_path = source_path.with_suffix(".mp4")
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(normalized_source_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(normalized_source_path),
                "generated_from": str(source_path),
                "subtitle_streams": [
                    {
                        "stream_index": 2,
                        "stream_position": 0,
                        "codec_name": "hdmv_pgs_subtitle",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Signs",
                        "kind": "image",
                        "asset_filename": "subtitle_stream_00_en.sup",
                        "cue_manifest_filename": "subtitle_stream_00_en.cues.json",
                        "status": "ok",
                        "error": None,
                    },
                    {
                        "stream_index": 3,
                        "stream_position": 1,
                        "codec_name": "hdmv_pgs_subtitle",
                        "language": "en",
                        "raw_language": "eng",
                        "title": "English Dialogue",
                        "kind": "image",
                        "asset_filename": "subtitle_stream_01_en.sup",
                        "cue_manifest_filename": "subtitle_stream_01_en.cues.json",
                        "status": "ok",
                        "error": None,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_00_en.cues.json").write_text(
        json.dumps({"cues": [{"cue_index": 1, "start": 3.0, "end": 3.4}]}, indent=2),
        encoding="utf-8",
    )
    (sidecar_dir / "subtitle_stream_01_en.cues.json").write_text(
        json.dumps({"cues": [{"cue_index": 1, "start": 1.5, "end": 2.0}]}, indent=2),
        encoding="utf-8",
    )

    project = Project(id="p-image-plan", output_language="de")
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=0.0,
                end_time=2.0,
                is_raw=True,
            )
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=source_path,
            clip_name="episode",
            source_in_frame=30,
            source_out_frame=66,
            source_in_seconds=1.25,
            source_out_seconds=2.75,
            source_duration_seconds=1.5,
        )
    }

    render_plan = asyncio.run(
        ProcessingService._build_raw_scene_image_render_plan(
            project,
            transcription,
            resolved_scene_sources,
        )
    )

    assert render_plan == {
        source_path: {
            1: [(1.25, 2.75)],
        }
    }


def test_process_passes_project_output_language_to_source_audio_policy_builder(
    monkeypatch,
    tmp_path,
):
    output_dir = tmp_path / "project-output"
    output_dir.mkdir(parents=True)
    edited_audio_path = output_dir / "tts_edited.wav"
    edited_audio_path.write_bytes(b"wav")
    transcription_path = output_dir / "transcription_timing.json"
    transcription_path.write_text(
        json.dumps({"language": "fr", "scenes": []}),
        encoding="utf-8",
    )
    (output_dir / "processing_state.json").write_text(
        json.dumps(
            {
                "edited_audio_path": str(edited_audio_path),
                "transcription_path": str(transcription_path),
            }
        ),
        encoding="utf-8",
    )

    source_path = tmp_path / "episode.mp4"
    source_path.write_bytes(b"video")
    calls: list[tuple[Path, str | None]] = []

    def _fake_build_source_audio_policy(
        cls,
        path: Path,
        *,
        target_language: str | None = None,
    ) -> SourceAudioSelectionPolicy:
        calls.append((path, target_language))
        return SourceAudioSelectionPolicy(
            selected_stream_index=1,
            selected_stream_position=0,
            selected_language="fr",
            selected_channel_count=2,
            selected_channel_offset=0,
            channel_type="stereo",
        )

    monkeypatch.setattr(
        ProcessingService,
        "get_output_dir",
        classmethod(lambda cls, _project_id: output_dir),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_has_saved_state",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_gaps_resolved",
        classmethod(lambda cls, _project_id: True),
    )

    async def _fake_detect_first_source_fps(cls, *_args, **_kwargs):
        return 23.976

    async def _fake_build_raw_scene_image_render_plan(cls, *_args, **_kwargs):
        return {source_path: {2: [(1.0, 2.0)]}}

    monkeypatch.setattr(
        ProcessingService,
        "detect_first_source_fps",
        classmethod(_fake_detect_first_source_fps),
    )
    monkeypatch.setattr(
        ProcessingService,
        "generate_jsx_script",
        classmethod(lambda cls, *_args, **_kwargs: "// jsx"),
    )
    monkeypatch.setattr(
        ProcessingService,
        "resolve_scene_sources",
        classmethod(
            lambda cls, *_args, **_kwargs: {
                0: ResolvedSceneSource(
                    scene_index=0,
                    source_path=source_path,
                    clip_name="episode",
                    source_in_frame=0,
                    source_out_frame=24,
                    source_in_seconds=0.0,
                    source_out_seconds=1.0,
                    source_duration_seconds=1.0,
                )
            }
        ),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_build_raw_scene_image_render_plan",
        classmethod(_fake_build_raw_scene_image_render_plan),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "build_source_audio_selection_policy",
        classmethod(_fake_build_source_audio_policy),
    )

    project = Project(id="p-audio-pref", output_language="fr")
    reference_transcription = Transcription(language="fr", scenes=[])

    async def _run() -> None:
        progress_iter = ProcessingService.process(
            project,
            new_script={"language": "fr", "scenes": []},
            audio_path=tmp_path / "tts.wav",
            matches=[],
            reference_transcription=reference_transcription,
        )
        while not calls:
            await anext(progress_iter)
        await progress_iter.aclose()

    asyncio.run(_run())

    assert calls == [(source_path, "fr")]


def test_process_does_not_rewrite_match_paths_or_save_matches_for_source_audio_policy(
    monkeypatch,
    tmp_path,
):
    output_dir = tmp_path / "project-output"
    output_dir.mkdir(parents=True)
    edited_audio_path = output_dir / "tts_edited.wav"
    edited_audio_path.write_bytes(b"wav")
    transcription_path = output_dir / "transcription_timing.json"
    transcription_path.write_text(
        json.dumps({"language": "fr", "scenes": []}),
        encoding="utf-8",
    )
    (output_dir / "processing_state.json").write_text(
        json.dumps(
            {
                "edited_audio_path": str(edited_audio_path),
                "transcription_path": str(transcription_path),
            }
        ),
        encoding="utf-8",
    )

    source_path = tmp_path / "library" / "episode.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"video")

    match = SceneMatch(
        scene_index=0,
        episode="episode-1",
        start_time=0.0,
        end_time=1.0,
        confidence=0.9,
        speed_ratio=1.0,
    )
    policy_calls: list[Path] = []

    def _fake_build_source_audio_policy(
        cls,
        path: Path,
        *,
        target_language: str | None = None,
    ) -> SourceAudioSelectionPolicy:
        policy_calls.append(path)
        return SourceAudioSelectionPolicy(
            selected_stream_index=1,
            selected_stream_position=0,
            selected_language=target_language,
            selected_channel_count=2,
            selected_channel_offset=0,
            channel_type="stereo",
        )

    monkeypatch.setattr(
        ProcessingService,
        "get_output_dir",
        classmethod(lambda cls, _project_id: output_dir),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_has_saved_state",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_gaps_resolved",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "detect_first_source_fps",
        classmethod(lambda cls, *_args, **_kwargs: asyncio.sleep(0, result=23.976)),
    )
    monkeypatch.setattr(
        ProcessingService,
        "generate_jsx_script",
        classmethod(lambda cls, *_args, **_kwargs: "// jsx"),
    )
    monkeypatch.setattr(
        ProcessingService,
        "resolve_scene_sources",
        classmethod(
            lambda cls, *_args, **_kwargs: {
                0: ResolvedSceneSource(
                    scene_index=0,
                    source_path=source_path,
                    clip_name="episode",
                    source_in_frame=0,
                    source_out_frame=24,
                    source_in_seconds=0.0,
                    source_out_seconds=1.0,
                    source_duration_seconds=1.0,
                )
            }
        ),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_build_raw_scene_image_render_plan",
        classmethod(lambda cls, *_args, **_kwargs: asyncio.sleep(0, result={})),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "build_source_audio_selection_policy",
        classmethod(_fake_build_source_audio_policy),
    )
    monkeypatch.setattr(
        ProjectService,
        "save_matches",
        classmethod(lambda cls, *_args, **_kwargs: pytest.fail("save_matches should not be called")),
    )

    project = Project(id="p-save-local", anime_name="Demo Anime", output_language="fr")
    reference_transcription = Transcription(language="fr", scenes=[])

    async def _run() -> None:
        progress_iter = ProcessingService.process(
            project,
            new_script={"language": "fr", "scenes": []},
            audio_path=tmp_path / "tts.wav",
            matches=[match],
            reference_transcription=reference_transcription,
        )
        while not policy_calls:
            await anext(progress_iter)
        await progress_iter.aclose()

    asyncio.run(_run())

    assert policy_calls == [source_path]
    assert match.episode == "episode-1"


def test_process_repairs_premiere_incompatible_audio_before_building_source_audio_policy(
    monkeypatch,
    tmp_path,
):
    output_dir = tmp_path / "project-output"
    output_dir.mkdir(parents=True)
    edited_audio_path = output_dir / "tts_edited.wav"
    edited_audio_path.write_bytes(b"wav")
    transcription_path = output_dir / "transcription_timing.json"
    transcription_path.write_text(
        json.dumps({"language": "fr", "scenes": []}),
        encoding="utf-8",
    )
    (output_dir / "processing_state.json").write_text(
        json.dumps(
            {
                "edited_audio_path": str(edited_audio_path),
                "transcription_path": str(transcription_path),
            }
        ),
        encoding="utf-8",
    )

    source_path = tmp_path / "episode.mp4"
    source_path.write_bytes(b"video")
    source_probe = _build_source_probe(source_path, codec_name="opus", channels=2)
    call_order: list[str] = []
    fixed_paths: list[Path] = []

    async def _fake_fix_audio(cls, path: Path, *, probe, library_type=None):
        assert probe == source_probe
        fixed_paths.append(path)
        call_order.append("fix")

    def _fake_build_source_audio_policy(
        cls,
        path: Path,
        *,
        target_language: str | None = None,
    ) -> SourceAudioSelectionPolicy:
        call_order.append("policy")
        return SourceAudioSelectionPolicy(
            selected_stream_index=1,
            selected_stream_position=0,
            selected_language=target_language,
            selected_channel_count=2,
            selected_channel_offset=0,
            channel_type="stereo",
        )

    monkeypatch.setattr(
        ProcessingService,
        "get_output_dir",
        classmethod(lambda cls, _project_id: output_dir),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_has_saved_state",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_gaps_resolved",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "detect_first_source_fps",
        classmethod(lambda cls, *_args, **_kwargs: asyncio.sleep(0, result=23.976)),
    )
    monkeypatch.setattr(
        ProcessingService,
        "generate_jsx_script",
        classmethod(lambda cls, *_args, **_kwargs: "// jsx"),
    )
    monkeypatch.setattr(
        ProcessingService,
        "resolve_scene_sources",
        classmethod(
            lambda cls, *_args, **_kwargs: {
                0: ResolvedSceneSource(
                    scene_index=0,
                    source_path=source_path,
                    clip_name="episode",
                    source_in_frame=0,
                    source_out_frame=24,
                    source_in_seconds=0.0,
                    source_out_seconds=1.0,
                    source_duration_seconds=1.0,
                )
            }
        ),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_build_raw_scene_image_render_plan",
        classmethod(lambda cls, *_args, **_kwargs: asyncio.sleep(0, result={})),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_fix_premiere_incompatible_audio_in_place",
        classmethod(_fake_fix_audio),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "_probe_media_sync",
        classmethod(lambda cls, _path: source_probe),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "build_source_audio_selection_policy",
        classmethod(_fake_build_source_audio_policy),
    )

    project = Project(id="p-audio-repair", output_language="fr")
    reference_transcription = Transcription(language="fr", scenes=[])

    async def _run() -> None:
        progress_iter = ProcessingService.process(
            project,
            new_script={"language": "fr", "scenes": []},
            audio_path=tmp_path / "tts.wav",
            matches=[],
            reference_transcription=reference_transcription,
        )
        while "policy" not in call_order:
            await anext(progress_iter)
        await progress_iter.aclose()

    asyncio.run(_run())

    assert fixed_paths == [source_path]
    assert call_order == ["fix", "policy"]


def test_process_skips_audio_repair_for_premiere_safe_source_audio(
    monkeypatch,
    tmp_path,
):
    output_dir = tmp_path / "project-output"
    output_dir.mkdir(parents=True)
    edited_audio_path = output_dir / "tts_edited.wav"
    edited_audio_path.write_bytes(b"wav")
    transcription_path = output_dir / "transcription_timing.json"
    transcription_path.write_text(
        json.dumps({"language": "fr", "scenes": []}),
        encoding="utf-8",
    )
    (output_dir / "processing_state.json").write_text(
        json.dumps(
            {
                "edited_audio_path": str(edited_audio_path),
                "transcription_path": str(transcription_path),
            }
        ),
        encoding="utf-8",
    )

    source_path = tmp_path / "episode.mp4"
    source_path.write_bytes(b"video")
    source_probe = _build_source_probe(source_path, codec_name="aac", channels=2)
    call_order: list[str] = []

    async def _unexpected_fix_audio(cls, path: Path, *, probe, library_type=None):
        pytest.fail(f"unexpected audio repair for {path}")

    def _fake_build_source_audio_policy(
        cls,
        path: Path,
        *,
        target_language: str | None = None,
    ) -> SourceAudioSelectionPolicy:
        call_order.append("policy")
        return SourceAudioSelectionPolicy(
            selected_stream_index=1,
            selected_stream_position=0,
            selected_language=target_language,
            selected_channel_count=2,
            selected_channel_offset=0,
            channel_type="stereo",
        )

    monkeypatch.setattr(
        ProcessingService,
        "get_output_dir",
        classmethod(lambda cls, _project_id: output_dir),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_has_saved_state",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "check_gaps_resolved",
        classmethod(lambda cls, _project_id: True),
    )
    monkeypatch.setattr(
        ProcessingService,
        "detect_first_source_fps",
        classmethod(lambda cls, *_args, **_kwargs: asyncio.sleep(0, result=23.976)),
    )
    monkeypatch.setattr(
        ProcessingService,
        "generate_jsx_script",
        classmethod(lambda cls, *_args, **_kwargs: "// jsx"),
    )
    monkeypatch.setattr(
        ProcessingService,
        "resolve_scene_sources",
        classmethod(
            lambda cls, *_args, **_kwargs: {
                0: ResolvedSceneSource(
                    scene_index=0,
                    source_path=source_path,
                    clip_name="episode",
                    source_in_frame=0,
                    source_out_frame=24,
                    source_in_seconds=0.0,
                    source_out_seconds=1.0,
                    source_duration_seconds=1.0,
                )
            }
        ),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_build_raw_scene_image_render_plan",
        classmethod(lambda cls, *_args, **_kwargs: asyncio.sleep(0, result={})),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_fix_premiere_incompatible_audio_in_place",
        classmethod(_unexpected_fix_audio),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "_probe_media_sync",
        classmethod(lambda cls, _path: source_probe),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "build_source_audio_selection_policy",
        classmethod(_fake_build_source_audio_policy),
    )

    project = Project(id="p-audio-safe", output_language="fr")
    reference_transcription = Transcription(language="fr", scenes=[])

    async def _run() -> None:
        progress_iter = ProcessingService.process(
            project,
            new_script={"language": "fr", "scenes": []},
            audio_path=tmp_path / "tts.wav",
            matches=[],
            reference_transcription=reference_transcription,
        )
        while "policy" not in call_order:
            await anext(progress_iter)
        await progress_iter.aclose()

    asyncio.run(_run())

    assert call_order == ["policy"]


def test_format_source_audio_policy_message_reports_selected_stream():
    source_path = Path("/tmp/library/episode.mp4")
    message = ProcessingService._format_source_audio_policy_message(
        current=1,
        total=1,
        source_path=source_path,
        policy=SourceAudioSelectionPolicy(
            selected_stream_index=2,
            selected_stream_position=1,
            selected_language="ja",
            selected_channel_count=2,
            selected_channel_offset=2,
            channel_type="stereo",
        ),
    )

    assert message == "Selected source audio (1/1): episode.mp4 -> ja stream 2 (stereo)"


def test_render_jsx_from_template_includes_dedicated_raw_scene_text_subtitle_paths():
    jsx = ProcessingService._render_jsx_from_template(
        project_id="project-123",
        scenes=[],
        source_audio_policies={
            "episode": {
                "selected_stream_index": 2,
                "selected_stream_position": 1,
                "selected_language": "ja",
                "selected_channel_count": 2,
                "selected_channel_offset": 2,
                "channel_type": "stereo",
            }
        },
        source_fps_num=24000,
        source_fps_den=1001,
        subtitle_timing_relative_path="subtitles/classic_timings.srt",
        raw_scene_subtitle_timing_relative_path="raw_scene_subtitles/raw_text.srt",
        raw_scene_subtitle_mogrt_relative_dir="raw_scene_subtitles/raw_text_mogrts",
        music_filename="",
        music_gain_db=-24.0,
    )

    assert 'var SUBTITLE_SRT_PATH = ROOT_DIR + "/subtitles/classic_timings.srt";' in jsx
    assert (
        'var RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR = ROOT_DIR + "/raw_scene_subtitles/raw_text_mogrts";'
        in jsx
    )
    assert (
        'var RAW_SCENE_TEXT_SUBTITLE_SRT_PATH = ROOT_DIR + "/raw_scene_subtitles/raw_text.srt";'
        in jsx
    )
    assert "RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR" in jsx
    assert "RAW_SCENE_TEXT_SUBTITLE_SRT_PATH" in jsx
    assert 'var PROJECT_ID = "project-123";' in jsx
    assert 'var BATCH_SEQUENCE_NAME = "ATR_BATCH__project-123";' in jsx
    assert 'var PROJECT_BIN_NAME = "__ATR_PROJECT__" + PROJECT_ID;' in jsx
    assert '"episode": {' in jsx
    assert '"selected_channel_offset": 2' in jsx
    assert 'var seqName = BATCH_SEQUENCE_NAME;' in jsx
    assert "deleteSequenceByName(seqName);" in jsx
    assert "function deleteSequenceByName(sequenceName)" in jsx
    assert "function ensureProjectBin()" in jsx
    assert "moveItemToProjectBin(sequence.projectItem);" in jsx
    assert "app.project.importFiles(importPaths, true, targetBin, false);" in jsx
    assert 'app.project.importFiles([f.fsName], true, targetBin, false);' in jsx
    assert 'log("Purging project to start fresh...");' not in jsx
    assert "if (!purgeProjectCompletely())" not in jsx
    assert "Script Complete (v7.7 Layered - Presets + External Subtitle MOGRTs)." not in jsx


def _render_raw_audio_jsx(source_audio_policies: dict[str, dict] | None = None) -> str:
    return ProcessingService._render_jsx_from_template(
        project_id="project-raw-a4",
        scenes=[
            {
                "scene_index": 7,
                "start": 335.183333,
                "end": 338.185667,
                "text": "",
                "clipName": "S01E03-Our First Date [AA9843FE]",
                "source_in_frame": 12804,
                "source_out_frame": 12876,
                "source_in": 534.0335,
                "source_out": 537.0365,
                "target_duration": 3.002334,
                "is_raw": True,
            }
        ],
        source_audio_policies=source_audio_policies or {},
        source_fps_num=24000,
        source_fps_den=1001,
        subtitle_timing_relative_path="subtitles/classic_timings.srt",
        raw_scene_subtitle_timing_relative_path="raw_scene_subtitles/raw_text.srt",
        raw_scene_subtitle_mogrt_relative_dir="raw_scene_subtitles/raw_text_mogrts",
        music_filename="",
        music_gain_db=-24.0,
    )


@pytest.mark.parametrize(
    ("raw_start_time", "raw_source_frame_count"),
    [
        pytest.param(10.915, 48, id="raw-scene-gap-regression"),
        pytest.param(20.905, 41, id="raw-scene-overlap-regression"),
    ],
)
def test_generate_jsx_script_snaps_raw_scene_bounds_to_next_scene_start(
    monkeypatch: pytest.MonkeyPatch,
    raw_start_time: float,
    raw_source_frame_count: int,
):
    source_frame_seconds = float(Fraction(1001, 24000))
    raw_source_duration = raw_source_frame_count * source_frame_seconds
    raw_end_time = raw_start_time + raw_source_duration
    next_end_time = raw_end_time + 1.25

    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="",
                words=[],
                start_time=raw_start_time,
                end_time=raw_end_time,
                is_raw=True,
            ),
            SceneTranscription(
                scene_index=1,
                text="next scene",
                words=[
                    Word(
                        text="next",
                        start=raw_end_time,
                        end=raw_end_time + 0.6,
                        confidence=1.0,
                    )
                ],
                start_time=raw_end_time,
                end_time=next_end_time,
                is_raw=False,
            ),
        ],
    )
    resolved_scene_sources = {
        0: ResolvedSceneSource(
            scene_index=0,
            source_path=Path("/tmp/raw-scene-source.mp4"),
            clip_name="raw-scene-source",
            source_in_frame=100,
            source_out_frame=100 + raw_source_frame_count,
            source_in_seconds=100 * source_frame_seconds,
            source_out_seconds=(100 + raw_source_frame_count) * source_frame_seconds,
            source_duration_seconds=raw_source_duration,
            used_alternative=False,
        ),
        1: ResolvedSceneSource(
            scene_index=1,
            source_path=Path("/tmp/next-scene-source.mp4"),
            clip_name="next-scene-source",
            source_in_frame=240,
            source_out_frame=312,
            source_in_seconds=240 * source_frame_seconds,
            source_out_seconds=312 * source_frame_seconds,
            source_duration_seconds=(312 - 240) * source_frame_seconds,
            used_alternative=False,
        ),
    }

    scenes = _capture_generated_scene_payload(
        monkeypatch,
        transcription,
        resolved_scene_sources,
    )

    assert [scene["scene_index"] for scene in scenes] == [0, 1]

    raw_scene = scenes[0]
    next_scene = scenes[1]
    expected_raw_start = int(raw_start_time * 60) / 60
    expected_raw_end = int(raw_end_time * 60) / 60

    assert raw_scene["start"] == pytest.approx(expected_raw_start, abs=1e-6)
    assert raw_scene["end"] == pytest.approx(expected_raw_end, abs=1e-6)
    assert raw_scene["end"] == pytest.approx(next_scene["start"], abs=1e-6)
    assert raw_scene["target_duration"] == pytest.approx(
        round(expected_raw_end - expected_raw_start, 4),
        abs=1e-4,
    )
    assert raw_scene["clip_duration"] == pytest.approx(
        round(raw_source_duration, 4),
        abs=1e-4,
    )
    assert raw_scene["source_in_frame"] == 100
    assert raw_scene["source_out_frame"] == 100 + raw_source_frame_count
    assert raw_scene["speed_ratio"] == pytest.approx(1.0)
    assert raw_scene["effective_speed"] == pytest.approx(1.0)
    assert raw_scene["is_raw"] is True


def test_render_jsx_from_template_clears_a4_before_raw_audio_rebuild():
    jsx = _render_raw_audio_jsx()

    assert "clearRawAudioZone(sequence, RAW_AUDIO_TRACK_START_INDEX, rawAudioZoneWidth);" in jsx
    assert "duplicateRawSceneAudioToTrack(" in jsx
    assert "rawAudioZoneWidth," in jsx
    assert "RAW_AUDIO_SUBCLIP_PREFIX =" in jsx
    assert "var RAW_AUDIO_TRACK_START_INDEX = 3;" in jsx
    assert "function getSourceAudioChannelTypeName(" in jsx
    assert "function getSourceAudioSelectedChannelCount(" in jsx
    assert "function getRawAudioBlockWidth(" in jsx
    assert "function getRequiredRawAudioZoneWidth(" in jsx
    assert "function getRequiredRawAudioZoneWidthForScenes(" in jsx
    assert "function describeRawAudioPolicy(" in jsx
    assert "getOrCreateRawAudioSubclip(scene)" in jsx
    assert "buildRawTimeFromSourceFrame(scene.source_in_frame, false);" in jsx
    assert "buildRawTimeFromSourceFrame(safeOutFrame, false);" in jsx
    assert "buildRawTimeFromSeconds(scene.source_in);" in jsx
    assert "buildRawTimeFromSeconds(safeOutSeconds);" in jsx
    assert "var hasHardBoundaries = 0;" in jsx
    assert "var takeVideo = 0;" in jsx
    assert "var takeAudio = 1;" in jsx
    assert "var rawAudioZoneWidth = getRequiredRawAudioZoneWidthForScenes(scenes);" in jsx
    assert "var rawAudioTrackDesiredCount = Math.max(" in jsx
    assert "ensureAudioTracks(sequence, rawAudioTrackDesiredCount);" in jsx
    assert "function collectRawAudioLinkedTrackItems(" in jsx
    assert "function mergeRawAudioTrackEntries(" in jsx
    assert "function collectRawAudioPlacedTrackItemsAcrossSequence(" in jsx
    assert "anchorItem.getLinkedItems()" in jsx
    assert "function buildRawAudioBlocks(" in jsx
    assert "function formatRawAudioBlockList(" in jsx
    assert "function hasRequiredCompleteRawAudioBlocks(" in jsx
    assert "function resolveRawAudioBlocksForRepair(" in jsx
    assert "function moveQEAudioTrackItem(" in jsx
    assert "qeItem.moveToTrack(toTrackIndex);" in jsx
    assert "function moveRawAudioBlockToTrack(" in jsx
    assert "function repairRawAudioPlacement(" in jsx
    assert "var sequenceEntries = collectRawAudioPlacedTrackItemsAcrossSequence(" in jsx
    assert "var blockResolution = resolveRawAudioBlocksForRepair(" in jsx
    assert "var blocks = blockResolution ? blockResolution.blocks || [] : [];" in jsx
    assert "var desiredBlockIndex = selectedStreamPosition;" in jsx
    assert "var keepTrackIndexes = {};" in jsx
    assert "repairRawAudioPlacement(" in jsx
    assert "function clearTrackClips(track)" in jsx
    assert "function clearRawAudioZone(" in jsx
    assert "clip.remove(false, true);" in jsx
    assert "removeSiblingAudioClips(" not in jsx
    assert "function removeTrackItem(clip)" not in jsx
    assert "validateRawAudioTrack(a4, scenes, sequenceEndSec);" not in jsx
    assert "waitForTrackItemAtExactStart(" not in jsx
    assert 'var clipKey = normalizeLooseName(scene.clipName || "");' not in jsx
    assert "a4.overwriteClip(subclip, startSec);" in jsx
    assert "a4.overwriteClip(sourceItem, startSec);" not in jsx
    assert "a4.overwriteClip(subclip, secondsToTicks(startSec).toString());" not in jsx
    assert "track.setTargeted(" not in jsx
    assert "function shouldApplySourceAudioPolicyToRawSubclip(" not in jsx
    assert "function validateRawAudioSubclip(" not in jsx
    assert "applySourceAudioPolicy(subclip, scene.clipName)" not in jsx
    assert "applySourceAudioPolicy(existingSubclip, scene.clipName)" not in jsx
    assert "var desiredTrackIndex = a4Index + selectedStreamPosition;" not in jsx


def test_render_jsx_from_template_includes_scene_retry_hooks_for_terminal_raw_scenes():
    jsx = _render_raw_audio_jsx()

    assert "function logSceneClipFailure(" in jsx
    assert "function resolveSceneClipForRetry(" in jsx
    assert "function resolveSceneTrackItemWithRetry(" in jsx
    assert "resolveSceneTrackItemWithRetry(" in jsx
    assert "function validateAndRepairRawSceneVideoPlacement(" in jsx
    assert "validateAndRepairRawSceneVideoPlacement(v1, v3, scenes);" in jsx
    assert '"missing before presets; retrying"' in jsx
    assert '"raw-scene retry clip lookup failed"' in jsx
    assert '"retry failed before presets"' in jsx


def test_render_jsx_from_template_raw_audio_zone_supports_default_stereo_policy():
    jsx = _render_raw_audio_jsx(
        {
            "S01E03-Our First Date [AA9843FE]": {
                "selected_stream_index": 1,
                "selected_stream_position": 0,
                "selected_language": None,
                "selected_channel_count": 2,
                "selected_channel_offset": 0,
                "channel_type": "stereo",
            }
        }
    )

    assert '"selected_stream_position": 0' in jsx
    assert '"selected_channel_count": 2' in jsx
    assert '"channel_type": "stereo"' in jsx
    assert "return Math.max(1, (selectedStreamPosition + 1) * blockWidth);" in jsx


def test_render_jsx_from_template_raw_audio_zone_supports_shifted_stereo_policy():
    jsx = _render_raw_audio_jsx(
        {
            "S01E03-Our First Date [AA9843FE]": {
                "selected_stream_index": 2,
                "selected_stream_position": 1,
                "selected_language": "ja",
                "selected_channel_count": 2,
                "selected_channel_offset": 2,
                "channel_type": "stereo",
            }
        }
    )

    assert '"selected_stream_position": 1' in jsx
    assert '"selected_channel_offset": 2' in jsx
    assert '"channel_type": "stereo"' in jsx
    assert "requested stream block " in jsx


def test_render_jsx_from_template_raw_audio_zone_supports_observed_51_layout_resolution():
    jsx = _render_raw_audio_jsx(
        {
            "S01E03-Our First Date [AA9843FE]": {
                "selected_stream_index": 1,
                "selected_stream_position": 0,
                "selected_language": "ja",
                "selected_channel_count": 6,
                "selected_channel_offset": 0,
                "channel_type": "51",
            }
        }
    )

    assert '"selected_stream_position": 0' in jsx
    assert '"selected_channel_count": 6' in jsx
    assert '"channel_type": "51"' in jsx
    assert "found placed clips outside the reserved zone; using sequence-wide placement " in jsx
    assert "using observed 5.1 multichannel grouping (" in jsx
    assert "falling back to per-track grouping for 5.1 policy because placed clips " in jsx
    assert "leaving kept 5.1 block on " in jsx
    assert "do not form " in jsx
    assert 'if (channelTypeName === "51" && channelCount > 1) return channelCount;' not in jsx


def test_export_collects_internal_subtitle_timing_files(tmp_path):
    subtitles_dir = tmp_path / "subtitles"
    subtitles_dir.mkdir(parents=True)
    timing_path = subtitles_dir / "subtitle_timings.srt"
    timing_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
    (subtitles_dir / "subtitle_1.mogrt").write_text("mogrt", encoding="utf-8")
    (subtitles_dir / "notes.txt").write_text("ignore", encoding="utf-8")

    assert ExportService._collect_subtitle_timing_files(tmp_path) == [timing_path]


def test_export_build_manifest_includes_original_library_episode_sources(monkeypatch, tmp_path):
    project = Project(
        id="p-export-local",
        anime_name="Demo Anime",
        output_language="fr",
    )
    episode_path = tmp_path / "library" / "Demo Anime" / "episode.mp4"
    episode_path.parent.mkdir(parents=True)
    episode_path.write_bytes(b"video")

    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "import_project.jsx").write_text("// jsx", encoding="utf-8")
    (output_dir / "tts_edited.wav").write_bytes(b"wav")
    (output_dir / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")

    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    for asset_name in ExportService.get_required_import_assets():
        (assets_dir / asset_name).write_text(asset_name, encoding="utf-8")

    monkeypatch.setattr(ExportService, "get_output_dir", classmethod(lambda cls, _project_id: output_dir))
    monkeypatch.setattr(ExportService, "get_assets_dir", classmethod(lambda cls: assets_dir))
    monkeypatch.setattr(
        ExportService,
        "_resolve_selected_music_path",
        classmethod(lambda cls, _project: None),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(lambda cls, episode_name, manifest=None, *, library_type=None: episode_path),
    )

    match = SceneMatch(
        scene_index=0,
        episode="episode-1",
        start_time=0.0,
        end_time=1.0,
        confidence=0.9,
        speed_ratio=1.0,
    )

    folder, entries = ExportService.build_manifest(project, [match])

    source_entries = [
        entry
        for entry in entries
        if entry.relative_path == f"{folder}/sources/{episode_path.name}"
    ]
    assert len(source_entries) == 1
    assert source_entries[0].source_path == episode_path


def test_export_collect_episode_sources_fails_for_unresolved_library_refs(monkeypatch, tmp_path):
    project = Project(
        id="p-export-strict",
        anime_name="Demo Anime",
        output_language="fr",
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(lambda cls, episode_name, manifest=None, *, library_type=None: None),
    )

    match = SceneMatch(
        scene_index=0,
        episode="episode-1",
        start_time=0.0,
        end_time=1.0,
        confidence=0.9,
        speed_ratio=1.0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        ExportService._collect_episode_sources(project, [match])

    assert "could not be resolved to library sources" in str(exc_info.value)
    assert "hydrated library" in str(exc_info.value)


def test_export_build_manifest_archives_subtitles(monkeypatch, tmp_path):
    project = Project(
        id="p-export",
        anime_name="Demo Anime",
        output_language="fr",
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "import_project.jsx").write_text("// jsx", encoding="utf-8")
    (output_dir / "tts_edited.wav").write_bytes(b"wav")
    (output_dir / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")

    subtitles_dir = output_dir / "subtitles"
    subtitles_dir.mkdir()
    (subtitles_dir / "subtitle_0001.mogrt").write_text("mogrt1", encoding="utf-8")
    (subtitles_dir / "subtitle_0002.mogrt").write_text("mogrt2", encoding="utf-8")
    (subtitles_dir / "subtitle_timings.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n",
        encoding="utf-8",
    )

    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    for asset_name in ExportService.get_required_import_assets():
        (assets_dir / asset_name).write_text(asset_name, encoding="utf-8")
    (assets_dir / "run_in_premiere.bat").write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setattr(ExportService, "get_output_dir", classmethod(lambda cls, _project_id: output_dir))
    monkeypatch.setattr(ExportService, "get_assets_dir", classmethod(lambda cls: assets_dir))
    monkeypatch.setattr(
        ExportService,
        "_collect_episode_sources",
        classmethod(lambda cls, _project, _matches: []),
    )
    monkeypatch.setattr(
        ExportService,
        "_resolve_selected_music_path",
        classmethod(lambda cls, _project: None),
    )

    folder, entries = ExportService.build_manifest(project, [])

    archive_entry = next(
        entry
        for entry in entries
        if entry.relative_path == f"{folder}/subtitles/{ExportService.SUBTITLES_ARCHIVE_FILENAME}"
    )
    assert archive_entry.inline_content is not None
    assert archive_entry.mime_type == "application/zip"
    assert all(
        not entry.relative_path.endswith(".mogrt")
        for entry in entries
        if "/subtitles/" in entry.relative_path
    )
    assert all(
        not entry.relative_path.endswith("subtitle_timings.srt")
        for entry in entries
        if "/subtitles/" in entry.relative_path
    )

    with zipfile.ZipFile(BytesIO(archive_entry.inline_content), "r") as archive:
        assert sorted(archive.namelist()) == [
            "subtitle_0001.mogrt",
            "subtitle_0002.mogrt",
            "subtitle_timings.srt",
        ]
