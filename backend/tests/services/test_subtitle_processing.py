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
    calls: list[tuple[Path, str | None, dict[int, list[tuple[float, float]]] | None]] = []

    async def _fake_normalize_source(
        cls,
        path: Path,
        *,
        preferred_audio_language: str | None = None,
        subtitle_image_render_windows=None,
    ) -> SourceNormalizationResult:
        calls.append((path, preferred_audio_language, subtitle_image_render_windows))
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

    async def _fake_build_raw_scene_image_render_plan(cls, *_args, **_kwargs):
        return {source_path: {2: [(1.0, 2.0)]}}

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
        "_build_raw_scene_image_render_plan",
        classmethod(_fake_build_raw_scene_image_render_plan),
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

    assert calls == [(source_path, "fr", {2: [(1.0, 2.0)]})]


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
