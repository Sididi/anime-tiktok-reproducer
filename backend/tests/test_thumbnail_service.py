"""Tests for thumbnail candidate computation and caching."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

from app.models.match import MatchList, SceneMatch
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


def _matches(specs: list[tuple[int, str, float, float, float]]) -> MatchList:
    return MatchList(matches=[
        SceneMatch(
            scene_index=i, episode=ep, start_time=s, end_time=e,
            confidence=1.0, speed_ratio=r,
        )
        for i, ep, s, e, r in specs
    ])


def test_seven_candidates_with_shift_and_mid():
    tr = _transcription([(0.0, 4.0), (4.0, 7.0), (7.0, 11.0)])
    cands = ThumbnailService.compute_candidates(tr, None)
    assert [c.index for c in cands] == [0, 1, 2, 3, 4, 5, 6]
    assert [(c.scene_index, c.position) for c in cands] == [
        (0, "start"), (0, "mid"), (0, "end"),
        (1, "start"), (2, "start"), (2, "end"), (1, "end"),
    ]
    assert cands[0].timestamp_seconds == pytest.approx(0.05)   # scene 1 start + shift
    assert cands[1].timestamp_seconds == pytest.approx(2.0)    # scene 1 mid, no shift
    assert cands[2].timestamp_seconds == pytest.approx(3.95)   # scene 1 end - shift
    assert cands[3].timestamp_seconds == pytest.approx(4.05)   # scene 2 start + shift
    assert cands[4].timestamp_seconds == pytest.approx(7.05)   # scene 3 start + shift
    assert cands[5].timestamp_seconds == pytest.approx(10.95)  # scene 3 end - shift
    assert cands[6].timestamp_seconds == pytest.approx(6.95)   # scene 2 end - shift
    assert cands[0].label == "Scène 1 · début"
    assert cands[1].label == "Scène 1 · milieu"
    assert cands[2].label == "Scène 1 · fin"
    assert cands[3].label == "Scène 2 · début"
    assert cands[4].label == "Scène 3 · début"
    assert cands[5].label == "Dernière scène · fin"
    assert cands[6].label == "Avant-dernière scène · fin"


def test_eleven_candidates_on_long_video():
    bounds = [(float(i), float(i) + 2.0) for i in range(9)]  # 9 scenes of 2s
    tr = _transcription(bounds)
    cands = ThumbnailService.compute_candidates(tr, None)
    assert len(cands) == 11
    assert [(c.scene_index, c.position) for c in cands] == [
        (0, "start"), (0, "mid"), (0, "end"),
        (1, "start"), (2, "start"), (3, "start"), (4, "start"), (5, "start"),
        (8, "end"), (7, "end"), (6, "end"),
    ]
    assert cands[0].label == "Scène 1 · début"
    assert cands[3].label == "Scène 2 · début"
    assert cands[8].label == "Dernière scène · fin"
    assert cands[9].label == "Avant-dernière scène · fin"
    assert cands[10].label == "Avant-avant-dernière scène · fin"
    assert [c.index for c in cands] == list(range(11))


def test_dedupe_on_short_videos():
    # 1 scene: only the scene-1 triple survives (last-scene ends dedupe onto (0,"end"))
    tr1 = _transcription([(0.0, 4.0)])
    assert [(c.scene_index, c.position) for c in ThumbnailService.compute_candidates(tr1, None)] == [
        (0, "start"), (0, "mid"), (0, "end"),
    ]
    # 4 scenes: starts of 1-4, ends of scenes 4,3,2
    tr4 = _transcription([(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0)])
    pairs = [(c.scene_index, c.position) for c in ThumbnailService.compute_candidates(tr4, None)]
    assert pairs == [
        (0, "start"), (0, "mid"), (0, "end"),
        (1, "start"), (2, "start"), (3, "start"),
        (3, "end"), (2, "end"), (1, "end"),
    ]


def test_source_coordinates_mapped_with_speed_ratio():
    tr = _transcription([(0.0, 2.0), (2.0, 4.0)])
    # scene 0: source 100..104 at speed_ratio 0.5 (output 2s from source 4s)
    matches = _matches([(0, "ep1.mkv", 100.0, 104.0, 0.5), (1, "ep2.mkv", 50.0, 52.0, 1.0)])
    cands = ThumbnailService.compute_candidates(tr, matches)
    by_key = {(c.scene_index, c.position): c for c in cands}
    shift_src = 0.05 / 0.5  # output shift / speed_ratio = 0.1s in source time
    assert by_key[(0, "start")].episode == "ep1.mkv"
    assert by_key[(0, "start")].source_timestamp_seconds == pytest.approx(100.0 + shift_src)
    assert by_key[(0, "mid")].source_timestamp_seconds == pytest.approx(102.0)
    assert by_key[(0, "end")].source_timestamp_seconds == pytest.approx(104.0 - shift_src)
    assert by_key[(1, "start")].source_timestamp_seconds == pytest.approx(50.05)
    # output timestamps unchanged from v1 math
    assert by_key[(0, "start")].timestamp_seconds == pytest.approx(0.05)


def test_source_shift_clamps_to_source_mid_and_bad_ratio_defaults():
    tr = _transcription([(0.0, 2.0)])
    # tiny source span: start+shift and end-shift clamp to source mid
    matches = _matches([(0, "ep.mkv", 10.0, 10.06, 1.0)])
    cands = ThumbnailService.compute_candidates(tr, matches)
    by_key = {(c.scene_index, c.position): c for c in cands}
    assert by_key[(0, "start")].source_timestamp_seconds == pytest.approx(10.03)
    assert by_key[(0, "end")].source_timestamp_seconds == pytest.approx(10.03)
    # zero/negative speed_ratio treated as 1.0
    matches2 = _matches([(0, "ep.mkv", 10.0, 20.0, 0.0)])
    c2 = {(c.scene_index, c.position): c for c in ThumbnailService.compute_candidates(tr, matches2)}
    assert c2[(0, "start")].source_timestamp_seconds == pytest.approx(10.05)


def test_scene_without_match_or_empty_episode_has_no_source():
    tr = _transcription([(0.0, 2.0), (2.0, 4.0)])
    matches = _matches([(0, "", 0.0, 4.0, 1.0)])  # scene 0 empty episode, scene 1 missing
    cands = ThumbnailService.compute_candidates(tr, matches)
    for c in cands:
        assert c.episode is None
        assert c.source_timestamp_seconds is None


def test_timestamp_ms_rounds():
    c = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=1.2345, scene_index=0, position="start"
    )
    assert c.timestamp_ms == 1234  # int(round(1.2345 * 1000)) == 1234 (banker's-free)


def test_fewer_scenes_yield_fewer_candidates():
    tr = _transcription([(0.0, 4.0)])
    cands = ThumbnailService.compute_candidates(tr, None)
    assert len(cands) == 3
    # 2 scenes: triple for scene 1 + (scene 2, start) + (scene 2, end) = 5
    tr2 = _transcription([(0.0, 4.0), (4.0, 7.0)])
    assert len(ThumbnailService.compute_candidates(tr2, None)) == 5


def test_tiny_scene_shift_clamped_to_mid():
    # Scene shorter than 2×shift: start+shift and end-shift both clamp to mid.
    tr = _transcription([(0.0, 0.06)])
    cands = ThumbnailService.compute_candidates(tr, None)
    mid = 0.03
    assert cands[0].timestamp_seconds == pytest.approx(mid)
    assert cands[2].timestamp_seconds == pytest.approx(mid)


def test_empty_or_degenerate_scenes_skipped():
    assert ThumbnailService.compute_candidates(_transcription([]), None) == []
    # zero-length scene is ignored entirely
    assert ThumbnailService.compute_candidates(_transcription([(2.0, 2.0)]), None) == []


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


def test_compose_vertical_cover_landscape_blurred_extend():
    frame = Image.new("RGB", (1920, 1080), (200, 30, 30))
    # paint a bright column so we can verify the foreground band placement
    for x in range(900, 1020):
        for y in range(0, 1080, 7):
            frame.putpixel((x, y), (0, 255, 0))
    cover = ThumbnailService.compose_vertical_cover(frame)
    assert cover.size == (1080, 1920)
    # foreground band: full-width 16:9 => height 607/608, vertically centered
    import numpy as np
    arr = np.asarray(cover)
    center_band = arr[930:990, :, :]
    top_band = arr[0:200, :, :]
    # background is darkened: top band strictly darker on average than center band
    assert float(top_band.mean()) < float(center_band.mean())


def test_compose_vertical_cover_portrait_passthrough_resize():
    frame = Image.new("RGB", (540, 960), (10, 10, 200))
    cover = ThumbnailService.compose_vertical_cover(frame)
    assert cover.size == (1080, 1920)


def test_extract_local_clean_frame_resolves_and_composes(monkeypatch, tmp_path):
    episode = tmp_path / "ep1.mkv"
    episode.write_bytes(b"\x00")
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeLibraryService.resolve_episode_path",
        classmethod(lambda cls, name, manifest=None, library_type=None: episode),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frame",
        classmethod(lambda cls, path, ts: Image.new("RGB", (1920, 1080), (5, 5, 5))),
    )
    cand = ThumbnailCandidate(
        index=0, label="Scène 1 · début", timestamp_seconds=0.05,
        scene_index=0, position="start",
        episode="ep1.mkv", source_timestamp_seconds=100.05,
    )
    frame = ThumbnailService._extract_local_clean_frame(cand, None)
    assert frame is not None and frame.size == (1920, 1080)


def test_extract_local_clean_frame_none_without_source_or_file(monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeLibraryService.resolve_episode_path",
        classmethod(lambda cls, name, manifest=None, library_type=None: None),
    )
    no_source = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
    )
    assert ThumbnailService._extract_local_clean_frame(no_source, None) is None
    with_source = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="gone.mkv", source_timestamp_seconds=1.0,
    )
    assert ThumbnailService._extract_local_clean_frame(with_source, None) is None
