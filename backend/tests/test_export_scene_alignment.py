"""Stale source scene numbering must never silently truncate Premiere video."""
from __future__ import annotations

import sys
import shutil
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import Project, Scene, SceneList
from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.processing import ProcessingService, ResolvedSceneSource
from app.services.project_service import ProjectService


def _transcription(bounds: list[tuple[float, float]]) -> Transcription:
    return Transcription(language="fr", scenes=[
        SceneTranscription(
            scene_index=i, text=f"Scene {i}", start_time=start, end_time=end,
            words=[Word(text="word", start=start, end=end)],
        )
        for i, (start, end) in enumerate(bounds)
    ])


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    (tmp_path / "test").mkdir()
    ProjectService.save_scenes("test", SceneList(scenes=[
        Scene(index=0, start_time=0, end_time=2),
        Scene(index=1, start_time=2, end_time=4),
    ]))
    return tmp_path / "test"


@pytest.mark.parametrize("bounds", [
    [(0, 1), (1, 2), (2, 4)],  # old split: extra tail indices
    [(0, 1), (1, 4)],  # same count/indices, different cut
])
def test_rejects_stale_original_transcription(project_dir, bounds):
    with pytest.raises(ValueError, match="Re-run transcription"):
        ProjectService.validate_transcription_scene_alignment("test", _transcription(bounds))


def test_accepts_current_bounds_despite_word_timing_differences(project_dir):
    transcription = _transcription([(0, 2), (2, 4)])
    transcription.scenes[0].words[0].start = 0.5
    transcription.scenes[0].words[0].end = 1.8
    ProjectService.validate_transcription_scene_alignment("test", transcription)


def test_script_entry_point_rejects_stale_transcript(project_dir):
    from fastapi import HTTPException
    from app.api.routes.processing import _load_transcription_for_script

    ProjectService.save_transcription("test", _transcription([(0, 1), (1, 4)]))
    with pytest.raises(HTTPException) as exc:
        _load_transcription_for_script("test")
    assert exc.value.status_code == 409
    assert "Re-run transcription" in exc.value.detail


@pytest.mark.parametrize("raw", [False, True])
def test_jsx_rejects_missing_video_for_narration_or_raw_scene(raw):
    transcription = _transcription([(0, 2), (2, 4)])
    transcription.scenes[1].is_raw = raw
    if raw:
        transcription.scenes[1].words = []
        transcription.scenes[1].text = ""
    source = ResolvedSceneSource(
        scene_index=0, source_path=Path("episode.mp4"), clip_name="episode",
        source_in_frame=0, source_out_frame=48,
        source_in_seconds=0, source_out_seconds=2.002,
        source_duration_seconds=2.002, used_alternative=False,
    )
    with pytest.raises(ValueError, match="Cannot export scene 1: no resolved video source"):
        ProcessingService.generate_jsx_script(
            Project(id="test"), transcription, [], resolved_scene_sources={0: source},
        )


def test_premiere_restores_clip_end_after_speed_change_and_rejects_failed_trim():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the ExtendScript duration check")
    template = ProcessingService.PREMIERE_JSX_TEMPLATE_PATH.read_text()
    setter = template.split("  function setTrackItemEndSeconds", 1)[1].split(
        "\n  function ", 1
    )[0]
    placement = template.split("      var sceneEndSec =", 1)[1].split(
        "      // 4. APPLY GEOMETRY", 1
    )[0]
    script = """
        const assert = require('node:assert/strict');
        var SEQ_FPS = 60;
        function snapSecondsToFrame(s) { return Math.round(s * 60) / 60; }
        function buildSequenceTimeFromSeconds(s) { return { seconds: s }; }
        function getTrackItemEndSeconds(item) {
            return typeof item.end === 'number' ? item.end : item.end.seconds;
        }
    """ + "function setTrackItemEndSeconds" + setter + """
        function finish(v3Item, v1Item) {
            var v3 = {}, v1 = {}, s = { scene_index: 2, end: 5 };
    """ + "var sceneEndSec =" + placement + """
        }
        // Model QE shortening the video clips after they were pre-resized.
        var main = { end: { seconds: 4 } };
        var background = { end: { seconds: 4.5 } };
        finish(main, background);
        assert.equal(main.end.seconds, 5);
        assert.equal(background.end.seconds, 5);
        var refusingClip = {};
        Object.defineProperty(refusingClip, 'end', {
            get: function () { return { seconds: 4 }; },
            set: function (_) {}
        });
        assert.throws(function () { finish(refusingClip, background); }, /Could not fill V3/);
        assert.throws(function () { finish(main, null); }, /Could not fill V1/);
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
