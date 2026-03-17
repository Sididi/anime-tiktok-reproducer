import asyncio
import json

from app.models.project import Project
from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.anime_library import AnimeLibraryService
from app.services.export_service import ExportService
from app.services.forced_alignment import ForcedAlignmentService
from app.services.processing import ProcessingService, ResolvedSceneSource
from app.services.script_automation_service import ScriptAutomationService


def test_normalize_external_subtitle_text_strips_markup_and_ass_controls():
    markup = '<font face="Trebuchet MS" size="22"><b>Je l\'ai rejoint,</b></font>'
    assert ProcessingService._normalize_external_subtitle_text(markup) == "Je l'ai rejoint,"

    ass_markup = r"{\an8}Texte\Nsuite &amp; fin"
    assert ProcessingService._normalize_external_subtitle_text(ass_markup) == "Texte suite & fin"


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
    assert [(entry.start, entry.end, entry.text) for entry in text_entries] == [
        (5.0, 5.4, "Avant"),
        (5.6, 6.2, "Pendant"),
        (6.9, 7.0, "Fin"),
    ]


def test_render_jsx_from_template_includes_dedicated_raw_scene_text_subtitle_paths():
    jsx = ProcessingService._render_jsx_from_template(
        scenes=[],
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


def test_export_collects_internal_subtitle_timing_files(tmp_path):
    subtitles_dir = tmp_path / "subtitles"
    subtitles_dir.mkdir(parents=True)
    timing_path = subtitles_dir / "subtitle_timings.srt"
    timing_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
    (subtitles_dir / "subtitle_1.mogrt").write_text("mogrt", encoding="utf-8")
    (subtitles_dir / "notes.txt").write_text("ignore", encoding="utf-8")

    assert ExportService._collect_subtitle_timing_files(tmp_path) == [timing_path]
