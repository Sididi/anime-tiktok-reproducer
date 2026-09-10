"""Narrator correction restores source words and never accumulates scene splits."""
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import raw_scenes, transcription as transcription_routes
from app.config import settings
from app.models import MatchList, Project, ProjectPhase, Scene, SceneList, SceneMatch, SceneTranscription, Transcription, Word
from app.models.raw_scene import DiarizationAnalysis
from app.services import narrator_service as module
from app.services.narrator_service import NarratorService as N, SOURCE_FILE, UNDO_FILE
from app.services.project_locks import ProjectLocks
from app.services.project_service import ProjectService as P
from app.services.raw_scene_detector import RawSceneDetectorService as R

STORY = "SPEAKER_00"
DIALOGUE = "SPEAKER_01"
BASE = "/projects/test/raw-scenes"


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    directory = tmp_path / "test"
    directory.mkdir()
    video = directory / "video.mp4"
    video.write_bytes(b"source video")
    (directory / "audio_16khz.wav").write_bytes(b"source audio")
    project = Project(id="test", phase=ProjectPhase.RAW_SCENE_VALIDATION, video_path=str(video))
    (directory / "project.json").write_text(project.model_dump_json())
    words = [Word(text=text, start=t, end=t + .4) for text, t in
             [("Once", 1), ("story", 2), ("dialogue", 6), ("speaking", 10), ("ending", 21)]]
    transcript = Transcription(language="en", scenes=[SceneTranscription(
        scene_index=0, start_time=0, end_time=24, text=" ".join(w.text for w in words), words=words,
    )], unrecovered_gaps=[(14, 17)])
    scenes = SceneList(scenes=[Scene(index=0, start_time=0, end_time=24)])
    matches = MatchList(matches=[SceneMatch(scene_index=0, episode="episode.mp4", start_time=100,
                                          end_time=124, speed_ratio=1, confidence=.99, confirmed=True)])
    analysis = DiarizationAnalysis(audio_duration=24,
                                  segments=[(0, 4, STORY), (4, 20, DIALOGUE), (20, 24, STORY)],
                                  centroids={STORY: [1, 0], DIALOGUE: [0, 1]})
    source = N.make_source(project, transcript, scenes, matches, analysis)
    N.publish_source("test", source)
    # All correction and restoration tests must work without any model calls.
    def forbidden(*args, **kwargs):
        raise AssertionError("Correction must not invoke model inference")
    monkeypatch.setattr(R, "_run_diarization", forbidden)
    monkeypatch.setattr(R, "_speaker_voice_profiles", forbidden)
    from app.services.transcriber import TranscriberService
    monkeypatch.setattr(TranscriberService, "_transcribe_sync", forbidden)
    ProjectLocks.reset()
    N.active_transcriptions.clear()
    app = FastAPI()
    app.include_router(raw_scenes.router)
    app.include_router(transcription_routes.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, directory, source
    ProjectLocks.reset()
    N.active_transcriptions.clear()


def select(client, speaker=STORY):
    revision = client.get(BASE).json()["narrator"]["revision"]
    response = client.put(BASE + "/narrator", json={"speaker_id": speaker, "expected_revision": revision})
    assert response.status_code == 200, response.text
    return response.json()


def test_short_storyteller_restored_without_inference_or_cumulative_splits(case):
    client, directory, _ = case
    automatic = client.get(BASE).json()
    assert automatic["detection"]["tts_speaker_id"] == DIALOGUE
    assert automatic["transcription"]["scenes"][0]["words"] == []
    source_bytes = (directory / SOURCE_FILE).read_bytes()
    for speaker in [STORY, DIALOGUE, STORY, None, STORY]:
        result = select(client, speaker)
        scenes = result["transcription"]["scenes"]
        assert len(scenes) == 3
        matches = P.load_matches("test").matches
        assert [(m.start_time, m.end_time) for m in matches] == [(100, 104), (104, 120), (120, 124)]
        assert [m.scene_index for m in matches] == [s["scene_index"] for s in scenes]
    assert [s["text"] for s in scenes] == ["Once story", "", "ending"]
    assert scenes[1]["words"] == [] and scenes[1]["is_raw"]
    assert result["transcription"]["unrecovered_gaps"] == [[14, 17]]
    assert result["detection"]["selection_origin"] == "manual"
    assert (directory / SOURCE_FILE).read_bytes() == source_bytes
    assert client.get(BASE).json()["detection"]["tts_speaker_id"] == STORY


def test_undo_survives_reload_and_restores_all_working_and_reset_files(case):
    client, _, _ = case
    before = N.read_state("test")
    select(client)
    loaded = client.get(BASE).json()
    assert loaded["narrator"]["undo_available"]
    response = client.post(BASE + "/narrator/undo", json={"expected_revision": loaded["narrator"]["revision"]})
    assert response.status_code == 200, response.text
    assert N.read_state("test") == before
    assert not response.json()["narrator"]["undo_available"]


def test_subsequent_text_edit_invalidates_undo(case):
    client, _, _ = case
    selected = select(client)
    response = client.put("/projects/test/transcription", json={"scenes": [{"scene_index": 0, "text": "Edited"}]})
    assert response.status_code == 200
    assert not client.get(BASE).json()["narrator"]["undo_available"]
    response = client.post(BASE + "/narrator/undo", json={"expected_revision": selected["narrator"]["revision"]})
    assert response.status_code == 409
    assert P.load_transcription("test").scenes[0].text == "Edited"


@pytest.mark.parametrize("replacement", [None, "Manual wording", ""])
def test_reject_raw_restores_leading_scene_and_preserves_explicit_text(case, replacement):
    client, _, _ = case
    assert client.get(BASE).json()["narrator"]["recoverable_text"]["0"] == "Once story"
    validation = {"scene_index": 0, "is_raw": False}
    if replacement is not None:
        validation["text"] = replacement
    response = client.post(BASE + "/validate", json={"validations": [validation]})
    assert response.status_code == 200, response.text
    scenes = response.json()["transcription"]["scenes"]
    assert len(scenes) == 3 and scenes[0]["start_time"] == 0
    assert scenes[0]["text"] == ("Once story" if replacement is None else replacement)
    assert bool(scenes[0]["words"]) == (replacement is None)
    assert not scenes[0]["is_raw"]


def test_reset_uses_current_narrator_and_restores_candidates(case):
    client, _, _ = case
    selected = select(client)
    assert client.post(BASE + "/validate", json={"validations": [{"scene_index": 1, "is_raw": False}]}).status_code == 200
    assert not client.get(BASE).json()["detection"]["has_raw_scenes"]
    assert client.post(BASE + "/reset").status_code == 200
    result = client.get(BASE).json()
    assert result["detection"] == selected["detection"]
    assert result["transcription"] == selected["transcription"]
    assert not result["narrator"]["undo_available"]


@pytest.mark.parametrize("problem", ["stale", "unknown", "active", "late_phase", "legacy", "audio_changed", "video_changed", "corrupt_source", "missing_audio"])
def test_unusable_or_stale_corrections_do_not_touch_working_files(case, problem):
    client, directory, _ = case
    request = {"speaker_id": STORY, "expected_revision": N.revision("test")}
    if problem == "stale": request["expected_revision"] = "old"
    elif problem == "unknown": request["speaker_id"] = "SPEAKER_99"
    elif problem == "active": N.active_transcriptions.add("test")
    elif problem == "late_phase":
        project = P.load("test")
        project.phase = ProjectPhase.SCRIPT_RESTRUCTURE
        (directory / "project.json").write_text(project.model_dump_json())
    elif problem == "legacy": (directory / SOURCE_FILE).unlink()
    elif problem == "audio_changed": (directory / "audio_16khz.wav").write_bytes(b"changed")
    elif problem == "video_changed": (directory / "video.mp4").write_bytes(b"changed")
    elif problem == "corrupt_source": (directory / SOURCE_FILE).write_text("{")
    elif problem == "missing_audio": (directory / "audio_16khz.wav").unlink()
    before = N.read_state("test")
    response = client.put(BASE + "/narrator", json=request)
    assert response.status_code == (422 if problem == "unknown" else 409), response.text
    assert N.read_state("test") == before
    if problem not in ("stale", "unknown"):
        assert not client.get(BASE).json()["narrator"]["correction_available"]


@pytest.mark.parametrize("filename", ["scenes.json", "raw_scene_detection.json", UNDO_FILE])
@pytest.mark.parametrize("selection", [{"speaker_id": None}, {"speaker_ids": [STORY, DIALOGUE]}])
def test_failed_apply_restores_every_file_and_previous_undo(case, monkeypatch, filename, selection):
    client, directory, _ = case
    select(client)
    before = N.read_state("test")
    previous_undo = (directory / UNDO_FILE).read_text()
    real_write = module.write_text_atomic
    failed = False
    def fail_once(path, content, **kwargs):
        nonlocal failed
        if path.name == filename and not failed:
            failed = True
            raise OSError("Simulated disk failure")
        return real_write(path, content, **kwargs)
    monkeypatch.setattr(module, "write_text_atomic", fail_once)
    response = client.put(BASE + "/narrator", json={**selection, "expected_revision": N.revision("test")})
    assert response.status_code == 500
    assert N.read_state("test") == before
    assert (directory / UNDO_FILE).read_text() == previous_undo


@pytest.mark.parametrize("language,pure", [("hi", True), ("en", False), ("es", False), ("fr", True)])
def test_cached_classification_matches_existing_automatic_behavior(case, language, pure):
    _, _, source = case
    source.transcription.language = language
    source.pure_mode = pure
    payload = N.derive(source, None)
    detection = json.loads(payload["raw_scene_detection.json"])
    assert detection["tts_speaker_id"] == DIALOGUE
    assert json.loads(payload["transcription.json"])["language"] == language
    assert len(detection["candidates"]) == 2


def test_single_voice_no_raw_candidates_still_exposes_voice_controls(case):
    client, _, source = case
    source.analysis.segments = [(0, 24, STORY)]
    N.publish_source("test", source)
    result = client.get(BASE).json()
    assert not result["detection"]["has_raw_scenes"]
    assert result["narrator"]["correction_available"]
    assert len(result["narrator"]["speakers"]) == 1


def test_missing_voice_profiles_allows_correction_and_exposes_warning(case):
    client, _, source = case
    source.analysis.centroids = {}
    source.analysis.warning = "Embedding model unavailable"
    N.publish_source("test", source)
    result = select(client)
    assert result["narrator"]["warning"]
    assert result["transcription"]["scenes"][0]["text"] == "Once story"


def test_diarization_failure_keeps_full_transcription_and_disables_correction(case):
    client, _, source = case
    source.analysis = DiarizationAnalysis(error="Model download failed")
    N.publish_source("test", source)
    result = client.get(BASE).json()
    assert result["detection"]["error"]
    assert not result["narrator"]["correction_available"]
    assert sum(len(s["words"]) for s in result["transcription"]["scenes"]) == 5


def test_legacy_leading_rejected_scene_and_raw_neighbor_are_preserved():
    scenes = [SceneTranscription(scene_index=i, start_time=i, end_time=i+1, is_raw=i == 1, text="")
              for i in range(3)]
    result = raw_scenes._merge_invalidated_scenes(scenes)
    assert len(result) == 3
    assert result[0].start_time == 0
    assert result[1].end_time == 2 and result[1].is_raw


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [False, True])
async def test_full_retranscription_publishes_new_source_and_resets_selection(case, monkeypatch, merged):
    client, _, source = case
    from app.services.transcriber import TranscriberService as T
    select_group(client, [STORY, DIALOGUE]) if merged else select(client)
    words = [w.model_dump() for s in source.transcription.scenes for w in s.words]
    monkeypatch.setattr(T, "_transcribe_sync", lambda *args: (words, "hi"))
    monkeypatch.setattr(T, "_extract_audio_for_whisper", lambda *args: None)
    monkeypatch.setattr(T, "unload_models", lambda: None)
    monkeypatch.setattr(T, "_last_unrecovered_gaps", [(14, 17)])
    monkeypatch.setattr(R, "analyze", lambda *args: source.analysis)
    progress = [item async for item in T._transcribe_impl("test", "hi")]
    assert progress[-1].status == "complete", progress[-1].error
    saved_source = N.load_source("test")
    assert saved_source.analysis_id != source.analysis_id
    assert sum(len(s.words) for s in saved_source.transcription.scenes) == 5
    result = client.get(BASE).json()
    assert result["detection"]["selection_origin"] == "automatic"
    assert not result["narrator"]["undo_available"]
    assert result["transcription"]["unrecovered_gaps"] == [[14, 17]]


def test_rejected_scene_after_raw_neighbor_gets_its_own_words(case):
    client, _, _ = case
    result = client.post(BASE + "/validate", json={"validations": [{"scene_index": 2, "is_raw": False}]}).json()
    scenes = result["transcription"]["scenes"]
    assert len(scenes) == 3 and scenes[2]["text"] == "ending"
    assert scenes[2]["start_time"] == 20


def test_manual_narrator_reuses_same_voice_merge_profiles(case):
    client, _, source = case
    source.analysis.segments[-1] = (20, 24, "SPEAKER_02")
    source.analysis.centroids["SPEAKER_02"] = [0.99, 0.01]
    N.publish_source("test", source)
    result = select(client)
    assert result["detection"]["merged_speaker_ids"] == ["SPEAKER_02"]
    assert result["transcription"]["scenes"][-1]["text"] == "ending"


def test_refreshing_hardlinked_audio_does_not_overwrite_original(tmp_path, monkeypatch):
    from app.services.transcriber import TranscriberService as T
    original = tmp_path / "mother.wav"
    original.write_bytes(b"original")
    duplicate = tmp_path / "duplicate.wav"
    duplicate.hardlink_to(original)
    monkeypatch.setattr(T, "_extract_audio_for_whisper", lambda video, path: path.write_bytes(b"new audio"))
    T._refresh_project_audio(tmp_path / "video.mp4", duplicate)
    assert original.read_bytes() == b"original"
    assert duplicate.read_bytes() == b"new audio"
    def fail(video, path):
        path.write_bytes(b"partial")
        raise RuntimeError("Extraction failed")
    monkeypatch.setattr(T, "_extract_audio_for_whisper", fail)
    with pytest.raises(RuntimeError):
        T._refresh_project_audio(tmp_path / "video.mp4", duplicate)
    assert duplicate.read_bytes() == b"new audio"
    assert not list(tmp_path.glob(".audio-refresh-*"))


def select_group(client, speakers):
    response = client.put(BASE + "/narrator", json={
        "speaker_ids": speakers, "expected_revision": N.revision("test"),
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("pure", [False, True])
def test_multiple_narrators_restore_both_voices_keep_character_and_cinematic_raw(case, pure):
    client, directory, source = case
    character = "SPEAKER_02"
    source.pure_mode = pure
    source.analysis.segments = [(0, 4, STORY), (4, 12, DIALOGUE), (12, 16, character), (20, 24, STORY)]
    source.analysis.centroids = {}  # Group selection also works without profiles.
    source.transcription.scenes[0].words.insert(4, Word(text="villain", start=14, end=14.4))
    N.publish_source("test", source)
    original = (directory / SOURCE_FILE).read_bytes()
    for voices in [[STORY, DIALOGUE], [STORY], None, [DIALOGUE, STORY, STORY]]:
        result = select_group(client, voices)
    scenes = result["transcription"]["scenes"]
    assert result["detection"]["selected_narrator_ids"] == [STORY, DIALOGUE]
    assert result["detection"]["selection_origin"] == "manual"
    assert result["detection"]["merged_speaker_ids"] == []
    assert [s["text"] for s in scenes] == ["Once story dialogue speaking", "", "ending"]
    assert [(s["start_time"], s["end_time"], s["is_raw"]) for s in scenes] == [
        (0, 12, False), (12, 20, True), (20, 24, False),
    ]
    assert [(m.start_time, m.end_time) for m in P.load_matches("test").matches] == [(100, 112), (112, 120), (120, 124)]
    assert (directory / SOURCE_FILE).read_bytes() == original
    # Selecting all voices still preserves the wordless movie/music interval.
    all_voices = select_group(client, [STORY, DIALOGUE, character])
    assert [(c["start_time"], c["end_time"]) for c in all_voices["detection"]["candidates"]] == [(16, 20)]
    assert "villain" in " ".join(s["text"] for s in all_voices["transcription"]["scenes"])


def test_group_survives_reload_reset_and_undo_then_can_be_unmerged(case):
    client, _, _ = case
    group = select_group(client, [STORY, DIALOGUE])
    assert not group["detection"]["has_raw_scenes"]
    assert client.get(BASE).json()["detection"]["selected_narrator_ids"] == [STORY, DIALOGUE]
    before = N.read_state("test")
    select_group(client, [STORY])
    response = client.post(BASE + "/narrator/undo", json={"expected_revision": N.revision("test")})
    assert response.status_code == 200
    assert N.read_state("test") == before
    assert client.put("/projects/test/transcription", json={"scenes": [{"scene_index": 0, "text": "Edit"}]}).status_code == 200
    assert client.post(BASE + "/reset").status_code == 200
    assert client.get(BASE).json()["detection"] == group["detection"]
    assert client.get(BASE).json()["transcription"] == group["transcription"]
    automatic = select_group(client, None)
    assert automatic["detection"]["selection_origin"] == "automatic"
    assert automatic["detection"]["selected_narrator_ids"] == [DIALOGUE]
    assert automatic["transcription"]["scenes"][0]["words"] == []


def test_same_voice_merge_applies_to_each_selected_narrator(case):
    client, _, source = case
    source.analysis.segments = [(0, 4, STORY), (4, 12, DIALOGUE), (12, 20, "SPEAKER_03"), (20, 24, "SPEAKER_02")]
    source.analysis.centroids.update({"SPEAKER_02": [.99, .01], "SPEAKER_03": [.01, .99]})
    N.publish_source("test", source)
    result = select_group(client, [DIALOGUE, STORY])
    assert result["detection"]["selected_narrator_ids"] == [STORY, DIALOGUE]
    assert set(result["detection"]["merged_speaker_ids"]) == {"SPEAKER_02", "SPEAKER_03"}
    assert not result["detection"]["has_raw_scenes"]
    assert sum(len(s["words"]) for s in result["transcription"]["scenes"]) == 5


@pytest.mark.parametrize("selection", [
    {"speaker_ids": []}, {"speaker_ids": [STORY, "unknown"]},
    {"speaker_ids": [STORY], "speaker_id": DIALOGUE},
    {"speaker_ids": None, "speaker_id": STORY}, {"speaker_ids": [None]},
])
def test_invalid_group_does_not_change_state(case, selection):
    client, _, _ = case
    before = N.read_state("test")
    response = client.put(BASE + "/narrator", json={**selection, "expected_revision": N.revision("test")})
    assert response.status_code == 422
    assert N.read_state("test") == before
