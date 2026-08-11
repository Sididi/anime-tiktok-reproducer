"""Tests for thumbnail candidate computation and caching."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

from app.models.transcription import SceneTranscription, Transcription
from app.services.thumbnail_service import ThumbnailCandidate, ThumbnailService


def _transcription(bounds: list[tuple[float, float]]) -> Transcription:
    return Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=i, text="", start_time=start, end_time=end
            )
            for i, (start, end) in enumerate(bounds)
        ],
    )


def test_five_candidates_with_shift_and_mid():
    tr = _transcription([(0.0, 4.0), (4.0, 7.0), (7.0, 11.0)])
    cands = ThumbnailService.compute_candidates(tr)
    assert [c.index for c in cands] == [0, 1, 2, 3, 4]
    assert cands[0].timestamp_seconds == pytest.approx(0.05)   # scene 1 start + shift
    assert cands[1].timestamp_seconds == pytest.approx(2.0)    # scene 1 mid, no shift
    assert cands[2].timestamp_seconds == pytest.approx(3.95)   # scene 1 end - shift
    assert cands[3].timestamp_seconds == pytest.approx(4.05)   # scene 2 start + shift
    assert cands[4].timestamp_seconds == pytest.approx(7.05)   # scene 3 start + shift
    assert cands[0].label == "Scène 1 · début"
    assert cands[1].label == "Scène 1 · milieu"
    assert cands[2].label == "Scène 1 · fin"
    assert cands[3].label == "Scène 2 · début"
    assert cands[4].label == "Scène 3 · début"


def test_timestamp_ms_rounds():
    c = ThumbnailCandidate(index=0, label="x", timestamp_seconds=1.2345)
    assert c.timestamp_ms == 1234  # int(round(1.2345 * 1000)) == 1234 (banker's-free)


def test_fewer_scenes_yield_fewer_candidates():
    tr = _transcription([(0.0, 4.0)])
    cands = ThumbnailService.compute_candidates(tr)
    assert len(cands) == 3
    tr2 = _transcription([(0.0, 4.0), (4.0, 7.0)])
    assert len(ThumbnailService.compute_candidates(tr2)) == 4


def test_tiny_scene_shift_clamped_to_mid():
    # Scene shorter than 2×shift: start+shift and end-shift both clamp to mid.
    tr = _transcription([(0.0, 0.06)])
    cands = ThumbnailService.compute_candidates(tr)
    mid = 0.03
    assert cands[0].timestamp_seconds == pytest.approx(mid)
    assert cands[2].timestamp_seconds == pytest.approx(mid)


def test_empty_or_degenerate_scenes_skipped():
    assert ThumbnailService.compute_candidates(_transcription([])) == []
    # zero-length scene is ignored entirely
    assert ThumbnailService.compute_candidates(_transcription([(2.0, 2.0)])) == []


def test_load_final_timeline_reads_output_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path
    )
    out_dir = tmp_path / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "t",'
        ' "words": [], "start_time": 0.0, "end_time": 3.0, "is_raw": false}]}'
    )
    tr = ThumbnailService.load_final_timeline("p1")
    assert tr is not None
    assert tr.scenes[0].end_time == 3.0
    assert ThumbnailService.load_final_timeline("missing") is None


@pytest.fixture
def fake_frames(monkeypatch):
    def _extract_frames(video_path, timestamps):
        return [Image.new("RGB", (4, 4), (255, 0, 0)) for _ in timestamps]

    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, video_path, timestamps: _extract_frames(video_path, timestamps)),
    )


def test_build_candidates_payload_caches_jpegs(tmp_path, monkeypatch, fake_frames):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = tmp_path / "projects" / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "",'
        ' "words": [], "start_time": 0.0, "end_time": 4.0, "is_raw": false}]}'
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)

    payload = ThumbnailService.build_candidates_payload("p1", video)
    assert payload["state"] == "ready"
    assert len(payload["candidates"]) == 3
    first = payload["candidates"][0]
    assert first["index"] == 0
    assert first["timestamp_ms"] == 50
    assert first["image_url"].startswith("/project-manager/projects/p1/thumbnail-frame/0")
    frame = ThumbnailService.cached_frame_path("p1", 0)
    assert frame is not None and frame.exists()


def test_build_candidates_payload_error_without_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)
    payload = ThumbnailService.build_candidates_payload("p1", video)
    assert payload["state"] == "error"
    assert payload["detail"]


def test_build_candidates_payload_drops_failed_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = tmp_path / "projects" / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "",'
        ' "words": [], "start_time": 0.0, "end_time": 4.0, "is_raw": false}]}'
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(
            lambda cls, video_path, timestamps: [
                None if i == 1 else Image.new("RGB", (4, 4)) for i in range(len(timestamps))
            ]
        ),
    )
    payload = ThumbnailService.build_candidates_payload("p1", video)
    assert payload["state"] == "ready"
    assert [c["index"] for c in payload["candidates"]] == [0, 2]


def test_build_candidates_payload_cache_hit_reuses_files(tmp_path, monkeypatch, fake_frames):
    """Second call for the same version must not re-rebuild (cache hit path)."""
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = tmp_path / "projects" / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "",'
        ' "words": [], "start_time": 0.0, "end_time": 4.0, "is_raw": false}]}'
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)

    first = ThumbnailService.build_candidates_payload("p1", video)
    assert first["state"] == "ready"
    frame_before = ThumbnailService.cached_frame_path("p1", 0)
    assert frame_before is not None
    mtime_before = frame_before.stat().st_mtime_ns

    second = ThumbnailService.build_candidates_payload("p1", video)
    assert second["state"] == "ready"
    assert second["version"] == first["version"]
    frame_after = ThumbnailService.cached_frame_path("p1", 0)
    assert frame_after is not None
    # Same file, untouched: rebuild was skipped on the cache-hit path.
    assert frame_after.stat().st_mtime_ns == mtime_before


def test_build_candidates_payload_concurrent_calls_do_not_collide(
    tmp_path, monkeypatch, fake_frames
):
    """Two threads racing build_candidates_payload for the same project must
    not collide (rmtree-during-write -> FileNotFoundError -> 500)."""
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = tmp_path / "projects" / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "",'
        ' "words": [], "start_time": 0.0, "end_time": 4.0, "is_raw": false}]}'
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)

    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _call():
        try:
            barrier.wait(timeout=5)
            results.append(ThumbnailService.build_candidates_payload("p1", video))
        except BaseException as exc:  # noqa: BLE001 - want to see any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert all(r["state"] == "ready" for r in results)


def test_extract_frame_image(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frame",
        classmethod(lambda cls, video_path, timestamp: Image.new("RGB", (4, 4))),
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00")
    dest = tmp_path / "thumb.jpg"
    result = ThumbnailService.extract_frame_image(video, 1.5, dest)
    assert result == dest and dest.exists()
    # failure path returns None
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frame",
        classmethod(lambda cls, video_path, timestamp: None),
    )
    assert ThumbnailService.extract_frame_image(video, 1.5, tmp_path / "t2.jpg") is None
