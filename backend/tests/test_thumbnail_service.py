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


def _v2_project(tmp_path, monkeypatch, scenes=2, with_matches=True):
    projects = tmp_path / "projects"
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects)
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = projects / "p1" / "output"
    out_dir.mkdir(parents=True)
    scene_json = ",".join(
        f'{{"scene_index": {i}, "text": "", "words": [], "start_time": {i * 2}.0,'
        f' "end_time": {i * 2 + 2}.0, "is_raw": false}}'
        for i in range(scenes)
    )
    (out_dir / "transcription_timing.json").write_text(
        f'{{"language": "fr", "scenes": [{scene_json}]}}'
    )
    if with_matches:
        ml = MatchList(matches=[
            SceneMatch(scene_index=i, episode=f"ep{i}.mkv", start_time=10.0 * i,
                       end_time=10.0 * i + 4.0, confidence=1.0, speed_ratio=1.0)
            for i in range(scenes)
        ])
        (projects / "p1" / "matches.json").write_text(ml.model_dump_json())
    (projects / "p1" / "project.json").write_text(
        '{"id": "p1", "url": "https://example.com/v", "drive_folder_id": "folder-1"}'
    )
    return projects


def test_progressive_build_clean_then_pending_fallback(tmp_path, monkeypatch):
    _v2_project(tmp_path, monkeypatch, scenes=2)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: Image.new("RGB", (1920, 1080), (9, 9, 9))
                    if cand.scene_index == 0 else None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    # output video NOT cached yet
    monkeypatch.setattr(
        "app.services.upload_phase.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: None),
    )
    ThumbnailService._run_candidates_build("p1")  # synchronous internal for tests
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "partial"
    by_index = {c["index"]: c for c in snap["candidates"]}
    # scene-0 candidates clean, scene-1 candidates pending (no local, no drive, no output)
    assert by_index[0]["source"] == "clean"
    assert by_index[0]["image_url"].startswith("/project-manager/projects/p1/thumbnail-frame/0")
    scene1_pending = [c for c in snap["candidates"] if c["source"] == "pending"]
    assert snap["pending"] == len(scene1_pending) > 0


def test_run_candidates_build_resume_preserves_resolved_clean_candidate(tmp_path, monkeypatch):
    """A resume call (existing meta, pending > 0) must not discard already-
    resolved candidates: no re-extraction, no file rewrite, no flicker back
    to "pending" for concurrent status readers."""
    _v2_project(tmp_path, monkeypatch, scenes=2)
    local_calls: list[int] = []

    def _local_extract(cls, cand, lt):
        local_calls.append(cand.scene_index)
        if cand.scene_index == 0:
            return Image.new("RGB", (1920, 1080), (9, 9, 9))
        return None

    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame", classmethod(_local_extract)
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    # output video NOT cached on either call: scene-1 candidates stay pending
    monkeypatch.setattr(
        "app.services.upload_phase.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: None),
    )

    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "partial"
    frame = ThumbnailService.cached_frame_path("p1", 0)
    assert frame is not None
    mtime_before = frame.stat().st_mtime_ns
    assert 0 in local_calls  # scene-0 was attempted on the fresh build

    local_calls.clear()
    ThumbnailService._run_candidates_build("p1")  # resume: pending > 0

    frame_after = ThumbnailService.cached_frame_path("p1", 0)
    assert frame_after is not None
    assert frame_after.stat().st_mtime_ns == mtime_before  # not rewritten
    assert 0 not in local_calls  # not re-extracted on resume

    snap_after = ThumbnailService.candidates_status("p1")
    by_index = {c["index"]: c for c in snap_after["candidates"]}
    assert by_index[0]["source"] == "clean"  # never flipped back to "pending"


def test_progressive_build_completes_with_output_fallback(tmp_path, monkeypatch):
    _v2_project(tmp_path, monkeypatch, scenes=2)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: Image.new("RGB", (1920, 1080), (9, 9, 9))
                    if cand.scene_index == 0 else None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    video = tmp_path / "output.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(
        "app.services.upload_phase.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, path, ts: [Image.new("RGB", (1080, 1920)) for _ in ts]),
    )
    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "ready"
    assert snap["pending"] == 0
    sources = {c["index"]: c["source"] for c in snap["candidates"]}
    assert "output" in sources.values() and "clean" in sources.values()
    # every candidate has a served composed cover
    for c in snap["candidates"]:
        p = ThumbnailService.cached_frame_path("p1", c["index"])
        assert p is not None and p.exists()
        with Image.open(p) as img:
            assert img.size == (1080, 1920)


def test_candidates_status_error_without_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "error"
    assert snap["detail"]


def test_run_candidates_build_cache_hit_skips_rebuild(tmp_path, monkeypatch):
    """Second call for the same version, once complete, must not re-rebuild."""
    _v2_project(tmp_path, monkeypatch, scenes=1, with_matches=False)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    video = tmp_path / "output.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(
        "app.services.upload_phase.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, path, ts: [Image.new("RGB", (1080, 1920)) for _ in ts]),
    )
    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "ready"
    frame_before = ThumbnailService.cached_frame_path("p1", 0)
    assert frame_before is not None
    mtime_before = frame_before.stat().st_mtime_ns

    ThumbnailService._run_candidates_build("p1")  # cache hit: pending == 0
    frame_after = ThumbnailService.cached_frame_path("p1", 0)
    assert frame_after is not None
    assert frame_after.stat().st_mtime_ns == mtime_before


def test_run_candidates_build_drops_candidates_when_output_fallback_fails(tmp_path, monkeypatch):
    """Mirrors v1's dropped-frame behavior: output-fallback failure while the
    video exists removes the candidate from meta entirely."""
    _v2_project(tmp_path, monkeypatch, scenes=1, with_matches=False)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    video = tmp_path / "output.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(
        "app.services.upload_phase.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, path, ts: [
            None if i == 1 else Image.new("RGB", (1080, 1920)) for i in range(len(ts))
        ]),
    )
    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "ready"
    assert [c["index"] for c in snap["candidates"]] == [0, 2]


def test_run_candidates_build_concurrent_calls_do_not_collide(tmp_path, monkeypatch):
    """Two threads racing _run_candidates_build for the same project must
    not collide (rmtree-during-write -> FileNotFoundError)."""
    _v2_project(tmp_path, monkeypatch, scenes=1, with_matches=False)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    video = tmp_path / "output.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(
        "app.services.upload_phase.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, path, ts: [Image.new("RGB", (1080, 1920)) for _ in ts]),
    )

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _call():
        try:
            barrier.wait(timeout=5)
            ThumbnailService._run_candidates_build("p1")
        except BaseException as exc:  # noqa: BLE001 - want to see any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "ready"
    assert snap["pending"] == 0


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


def test_drive_clean_frame_builds_range_fetch_command(monkeypatch, tmp_path):
    from app.services.thumbnail_service import ThumbnailService

    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.find_subfolder",
        classmethod(lambda cls, parent, name: "srcfolder" if name == "sources" else None),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.list_children_named",
        classmethod(lambda cls, folder_id, filename, drive=None: [{"id": "fid123"}]),
    )

    class _Creds:
        token = "tok-abc"

    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.credentials",
        classmethod(lambda cls: _Creds()),
    )
    captured: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # produce the output file the command would have written
        out = Path(cmd[-1])
        Image.new("RGB", (1920, 1080), (1, 2, 3)).save(out, "JPEG")
        return _Result()

    monkeypatch.setattr(
        "app.services.thumbnail_service.rewrite_media_command", lambda cmd: list(cmd)
    )
    monkeypatch.setattr("app.services.thumbnail_service.subprocess.run", fake_run)
    cand = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="/library/Anime/ep1.mkv", source_timestamp_seconds=63.25,
    )
    frame = ThumbnailService._extract_drive_clean_frame(cand, "folder-1")
    assert frame is not None and frame.size == (1920, 1080)
    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "63.250"
    assert any("fid123" in part and "alt=media" in part for part in cmd)
    assert any("Bearer tok-abc" in part for part in cmd)
    assert "-frames:v" in cmd
    # episode ref reduced to basename for the Drive lookup
    # (list_children_named stub above only matches; assert via captured lookup is implicit)


def test_drive_clean_frame_none_when_missing(monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.find_subfolder",
        classmethod(lambda cls, parent, name: None),
    )
    cand = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="ep1.mkv", source_timestamp_seconds=1.0,
    )
    assert ThumbnailService._extract_drive_clean_frame(cand, "folder-1") is None


def test_drive_clean_frame_none_on_ffmpeg_failure(monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.find_subfolder",
        classmethod(lambda cls, parent, name: "srcfolder"),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.list_children_named",
        classmethod(lambda cls, folder_id, filename, drive=None: [{"id": "fid123"}]),
    )

    class _Creds:
        token = "tok"

    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.credentials",
        classmethod(lambda cls: _Creds()),
    )

    class _Fail:
        returncode = 1

    monkeypatch.setattr(
        "app.services.thumbnail_service.rewrite_media_command", lambda cmd: list(cmd)
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.subprocess.run", lambda cmd, **kw: _Fail()
    )
    cand = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="ep1.mkv", source_timestamp_seconds=1.0,
    )
    assert ThumbnailService._extract_drive_clean_frame(cand, "folder-1") is None
