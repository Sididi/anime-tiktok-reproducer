import asyncio
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.project import Project
from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.anime_library import AnimeLibraryService, SourceNormalizationResult
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


def test_build_authoritative_playback_timeline_inserts_silence_for_empty_scenes():
    transcription = Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text="Bonjour",
                words=[
                    Word(text="Bonjour", start=0.0, end=0.8, confidence=1.0),
                ],
                start_time=0.0,
                end_time=0.8,
            ),
            SceneTranscription(
                scene_index=1,
                text="",
                words=[],
                start_time=2.0,
                end_time=4.5,
            ),
        ],
    )

    transformed, segments = ProcessingService.build_authoritative_playback_timeline(
        transcription
    )

    assert [(segment.kind, round(segment.duration, 1)) for segment in segments] == [
        ("audio", 0.8),
        ("silence", 2.5),
    ]
    assert transformed.scenes[0].start_time == 0.0
    assert round(transformed.scenes[0].end_time, 1) == 0.8
    assert round(transformed.scenes[1].start_time, 1) == 0.8
    assert round(transformed.scenes[1].end_time, 1) == 3.3


def test_process_passes_project_output_language_to_source_normalization(
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

    async def _fake_normalize_source(
        cls,
        path: Path,
        *,
        preferred_audio_language: str | None = None,
    ) -> SourceNormalizationResult:
        calls.append((path, preferred_audio_language))
        return SourceNormalizationResult(
            action="noop",
            source_path=path,
            normalized_path=path,
            changed=False,
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

    async def _fake_collect_required_source_groups(cls, *_args, **_kwargs):
        return [(source_path, [])]

    monkeypatch.setattr(
        ProcessingService,
        "detect_first_source_fps",
        classmethod(_fake_detect_first_source_fps),
    )
    monkeypatch.setattr(
        ProcessingService,
        "resolve_scene_sources",
        classmethod(lambda cls, *_args, **_kwargs: {}),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_collect_required_source_groups",
        classmethod(_fake_collect_required_source_groups),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_update_absolute_match_episode_hints",
        classmethod(lambda cls, *_args, **_kwargs: False),
    )
    monkeypatch.setattr(
        ProcessingService,
        "_format_source_normalization_message",
        classmethod(lambda cls, **_kwargs: "normalized"),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "normalize_source_for_processing",
        classmethod(_fake_normalize_source),
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
        await anext(progress_iter)
        await anext(progress_iter)
        await progress_iter.aclose()

    asyncio.run(_run())

    assert calls == [(source_path, "fr")]


def test_render_jsx_from_template_only_uses_classic_subtitle_paths():
    jsx = ProcessingService._render_jsx_from_template(
        scenes=[],
        source_fps_num=24000,
        source_fps_den=1001,
        subtitle_timing_relative_path="subtitles/classic_timings.srt",
        music_filename="",
        music_gain_db=-24.0,
    )

    assert 'var SUBTITLE_SRT_PATH = ROOT_DIR + "/subtitles/classic_timings.srt";' in jsx
    assert "RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR" not in jsx
    assert "RAW_SCENE_TEXT_SUBTITLE_SRT_PATH" not in jsx


def test_export_collects_internal_subtitle_timing_files(tmp_path):
    subtitles_dir = tmp_path / "subtitles"
    subtitles_dir.mkdir(parents=True)
    timing_path = subtitles_dir / "subtitle_timings.srt"
    timing_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
    (subtitles_dir / "subtitle_1.mogrt").write_text("mogrt", encoding="utf-8")
    (subtitles_dir / "notes.txt").write_text("ignore", encoding="utf-8")

    assert ExportService._collect_subtitle_timing_files(tmp_path) == [timing_path]


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
