from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Scene, SceneList
from app.services import scene_aligner as aligner_module
from app.services.scene_aligner import (
    Correspondence,
    MAX_EVIDENCE_SPEED,
    SceneAlignerService,
    SegmentHypothesis,
)


def _corr(
    sample_index: int,
    t_tiktok: float,
    t_source: float,
    episode: str = "episode-01.mkv",
    similarity: float = 0.82,
) -> Correspondence:
    return Correspondence(
        sample_index=sample_index,
        t_tiktok=t_tiktok,
        t_source=t_source,
        episode=episode,
        similarity=similarity,
        series="series",
        rank=0,
    )


def test_dense_sampler_uses_actual_pts_for_vfr(monkeypatch) -> None:
    class FakeCV2:
        CAP_PROP_FPS = 1
        CAP_PROP_POS_MSEC = 2
        COLOR_BGR2RGB = 3
        COLOR_BGR2GRAY = 4
        INTER_AREA = 5

        @staticmethod
        def resize(frame, size, interpolation):
            return frame

        @staticmethod
        def cvtColor(frame, code):
            if code == FakeCV2.COLOR_BGR2GRAY:
                return frame[:, :, 0]
            return frame

    class FakeCapture:
        pts = [0.0, 0.25, 0.5, 2.0, 2.25]

        def __init__(self) -> None:
            self.next_index = 0
            self.last_index: int | None = None

        def get(self, prop: int) -> float:
            if prop == FakeCV2.CAP_PROP_FPS:
                return 2.5
            if prop == FakeCV2.CAP_PROP_POS_MSEC:
                return (
                    self.pts[self.last_index] * 1000.0
                    if self.last_index is not None
                    else 0.0
                )
            return 0.0

        def read(self):
            if self.next_index >= len(self.pts):
                return False, None
            self.last_index = self.next_index
            self.next_index += 1
            frame = np.full((2, 2, 3), self.last_index, dtype=np.uint8)
            return True, frame

        def release(self) -> None:
            pass

    monkeypatch.setattr(
        FakeCV2,
        "VideoCapture",
        staticmethod(lambda path: FakeCapture()),
        raising=False,
    )
    from app.services.anime_matcher import AnimeMatcherService

    monkeypatch.setattr(
        AnimeMatcherService,
        "_require_cv2",
        classmethod(lambda cls: FakeCV2),
    )
    monkeypatch.setattr(aligner_module, "_presize_images", lambda images: images)
    monkeypatch.setattr(
        AnimeMatcherService,
        "_embed_pil_batch",
        classmethod(
            lambda cls, images: np.asarray(
                [[float(np.asarray(image)[0, 0, 0])] for image in images],
                dtype=np.float32,
            )
        ),
    )

    samples, diff_times, _ = SceneAlignerService.sample_query_video_with_diffs(
        Path("vfr.mp4")
    )
    by_time = {sample.t_tiktok: int(sample.embedding[0]) for sample in samples}

    # 1.5s is nearest the frame presented at 2.0s (value/index 3). The old
    # timestamp*average-FPS path selected frame 4 at 2.25s instead.
    assert by_time[1.5] == 3
    assert diff_times == [0.25, 0.5, 2.0, 2.25]


def test_segment_extraction_recovers_fast_affine_segment() -> None:
    scene = Scene(index=0, start_time=10.0, end_time=11.0)
    speed = 4.07
    offset = 120.0
    correspondences = [
        _corr(i, t, speed * t + offset)
        for i, t in enumerate([10.0, 10.25, 10.5, 10.75])
    ]
    correspondences.extend(
        [
            _corr(10, 10.25, 45.0, episode="episode-02.mkv", similarity=0.91),
            _corr(11, 10.50, 300.0, episode="episode-01.mkv", similarity=0.50),
        ]
    )

    segments = SceneAlignerService.extract_scene_segments(
        SceneList(scenes=[scene]),
        correspondences,
    )[0]

    assert segments
    assert segments[0].episode == "episode-01.mkv"
    assert abs(segments[0].a - speed) < 0.05
    assert segments[0].a < MAX_EVIDENCE_SPEED
    assert segments[0].inlier_count == 4


def _run_segmentation_dp(scenes: SceneList, correspondences: list[Correspondence]):
    decode_segments = SceneAlignerService.extract_scene_segments(
        scenes, correspondences
    )
    _, remapped, _, _ = SceneAlignerService._segment_timeline_dp(
        Path("query.mp4"),
        scenes,
        decode_segments,
        correspondences,
        [],
        None,
        "video",
    )
    return remapped


def test_dp_keeps_non_monotonic_jump_when_emission_is_strong() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
        ]
    )
    correspondences = []
    for i, t in enumerate([0.0, 0.25, 0.5, 0.75]):
        correspondences.append(_corr(i, t, 200.0 + t, similarity=0.85))
    for i, t in enumerate([1.0, 1.25, 1.5, 1.75], start=4):
        correspondences.append(_corr(i, t, 118.0 + t, similarity=0.87))

    remapped = _run_segmentation_dp(scenes, correspondences)

    assert len(remapped) == 2
    first = remapped[0][1]
    second = remapped[1][1]
    assert first is not None and second is not None
    assert first.episode == second.episode
    assert second.source_at(1.0) < first.source_at(1.0)


def test_dp_allows_intruder_episode_with_coherent_support() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
            Scene(index=2, start_time=2.0, end_time=3.0),
        ]
    )
    correspondences = []
    for i, t in enumerate([0.0, 0.25, 0.5, 0.75]):
        correspondences.append(_corr(i, t, 50.0 + t, episode="episode-01.mkv"))
    for i, t in enumerate([1.0, 1.25, 1.5, 1.75], start=4):
        correspondences.append(_corr(i, t, 140.0 + t, episode="episode-03.mkv", similarity=0.90))
    for i, t in enumerate([2.0, 2.25, 2.5, 2.75], start=8):
        correspondences.append(_corr(i, t, 52.0 + t, episode="episode-01.mkv"))

    remapped = _run_segmentation_dp(scenes, correspondences)

    episodes = [
        segment.episode if segment is not None else ""
        for _, segment in remapped
    ]
    assert "episode-03.mkv" in episodes
    assert episodes[0] == "episode-01.mkv"
    assert episodes[-1] == "episode-01.mkv"


def test_edge_pair_hypothesis_uses_actual_sample_times() -> None:
    scene = Scene(index=0, start_time=10.0, end_time=12.0)
    # Edge samples are inside the scene. The fitted line must extrapolate back
    # to the true scene boundaries rather than treating the edge samples as the
    # boundaries themselves.
    correspondences = [
        _corr(0, 10.125, 100.125, similarity=0.9),
        _corr(1, 11.875, 101.875, similarity=0.9),
    ]

    segments = SceneAlignerService._edge_pair_hypotheses(scene, correspondences)

    assert segments
    start, end = segments[0].source_interval(scene)
    assert abs(start - 100.0) < 1e-6
    assert abs(end - 102.0) < 1e-6


# --- Stage 5 native arbitration toolkit (fabricated frames, known offsets) ---

_TRUE_QUERY_TO_SOURCE = 5.0  # fabricated ground truth: source time = tiktok + 5.0


class _QueryFrame:
    def __init__(self, t: float) -> None:
        self.t = t

    def convert(self, _mode: str) -> "_QueryFrame":
        return self


class _SourceFrame:
    def __init__(self, t: float) -> None:
        self.t = t

    def convert(self, _mode: str) -> "_SourceFrame":
        return self


def _source_emb(t: float, static: bool = False) -> np.ndarray:
    """Smooth unique curve on the unit sphere: nearby source times look
    similar, distant ones do not (no duplicates within a sweep window)."""
    if static:
        t = 0.0
    v = np.array(
        [np.cos(2.0 * t), np.sin(2.0 * t), np.cos(0.37 * t), np.sin(0.37 * t)]
    )
    return v / np.linalg.norm(v)


def _install_stage5_fakes(monkeypatch, tmp_path, static: bool = False) -> None:
    from app.services.anime_library import AnimeLibraryService
    from app.services.anime_matcher import AnimeMatcherService

    episode_file = tmp_path / "episode-01.mkv"
    episode_file.touch()

    class _FakeCap:
        def release(self) -> None:
            pass

    class _FakeCv2:
        @staticmethod
        def VideoCapture(_path: str) -> "_FakeCap":
            return _FakeCap()

    monkeypatch.setattr(
        SceneAlignerService,
        "_zoom_crop",
        staticmethod(lambda image, zoom: image),
    )
    monkeypatch.setattr(
        SceneAlignerService,
        "_small_gray",
        staticmethod(lambda image, height=360: np.zeros((8, 8), dtype=np.float32)),
    )
    monkeypatch.setattr(
        SceneAlignerService,
        "_pan_zero_crossing",
        classmethod(lambda cls, edge_gray, frames: None),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "extract_frames",
        classmethod(lambda cls, video_path, ts: [_QueryFrame(t) for t in ts]),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_embed_pil_batch",
        classmethod(
            lambda cls, frames: np.stack(
                [
                    _source_emb(
                        f.t + _TRUE_QUERY_TO_SOURCE
                        if isinstance(f, _QueryFrame)
                        else f.t,
                        static=static,
                    )
                    for f in frames
                ]
            )
        ),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_collect_frames_in_window_from_capture",
        classmethod(
            lambda cls, cap, lo, hi, max_frames=48, sample_frames=None: [
                (float(t), _SourceFrame(float(t)))
                for t in np.arange(max(0.0, lo), hi, 1.0 / 24.0)
            ]
        ),
    )
    monkeypatch.setattr(
        AnimeMatcherService, "_require_cv2", staticmethod(lambda: _FakeCv2)
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(lambda cls, episode, **kwargs: episode_file),
    )


def _one_scene_setup(a: float, b: float):
    scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=2.0)])
    segment = SegmentHypothesis(
        id=-1,
        episode="episode-01.mkv",
        tiktok_start=0.0,
        tiktok_end=2.0,
        a=a,
        b=b,
        inlier_count=8,
        mean_similarity=0.6,
        score=1.0,
        scene_index=0,
    )
    remapped = [([0], segment)]
    raw = [(segment.source_at(0.0), segment.source_at(2.0))]
    return scenes, remapped, raw


def test_stage5_recovers_known_offset(monkeypatch, tmp_path) -> None:
    _install_stage5_fakes(monkeypatch, tmp_path)
    # fitted line is 0.3s early; edge-anchored sweep must recover +0.3
    scenes, remapped, raw = _one_scene_setup(a=1.0, b=_TRUE_QUERY_TO_SOURCE - 0.3)
    deltas, doubts, _splits = SceneAlignerService._stage5_refine(
        Path("query.mp4"), scenes, remapped, [0], raw, "video"
    )
    assert 0 in deltas
    assert abs(deltas[0][0] - 0.3) <= 0.07
    assert abs(deltas[0][1] - 0.3) <= 0.07
    assert "static_start" not in doubts.get(0, [])


def test_stage5_rate_arbitration_prefers_unit_slope(monkeypatch, tmp_path) -> None:
    _install_stage5_fakes(monkeypatch, tmp_path)
    # phantom 0.5x fit whose midpoint sits on the true unit-rate line
    scenes, remapped, raw = _one_scene_setup(a=0.5, b=_TRUE_QUERY_TO_SOURCE + 0.5)
    deltas, doubts, _splits = SceneAlignerService._stage5_refine(
        Path("query.mp4"), scenes, remapped, [0], raw, "video"
    )
    assert "rate_arbitrated" in doubts.get(0, [])
    start = raw[0][0] + deltas.get(0, (0.0, 0.0))[0]
    end = raw[0][1] + deltas.get(0, (0.0, 0.0))[1]
    assert abs(start - _TRUE_QUERY_TO_SOURCE) <= 0.07
    assert abs(end - (2.0 + _TRUE_QUERY_TO_SOURCE)) <= 0.07


def test_stage5_static_plateau_yields_doubt_not_shift(monkeypatch, tmp_path) -> None:
    _install_stage5_fakes(monkeypatch, tmp_path, static=True)
    scenes, remapped, raw = _one_scene_setup(a=1.0, b=_TRUE_QUERY_TO_SOURCE - 0.3)
    deltas, doubts, _splits = SceneAlignerService._stage5_refine(
        Path("query.mp4"), scenes, remapped, [0], raw, "video"
    )
    assert "static_start" in doubts.get(0, [])
    assert "static_end" in doubts.get(0, [])
    # no confident lock and no frame-change peak: the line must not move
    applied = deltas.get(0, (0.0, 0.0))
    assert abs(applied[0]) <= 1e-9 and abs(applied[1]) <= 1e-9


def _segment(episode: str, a: float, b: float, t0: float, t1: float, index: int):
    return SegmentHypothesis(
        id=-1,
        episode=episode,
        tiktok_start=t0,
        tiktok_end=t1,
        a=a,
        b=b,
        inlier_count=8,
        mean_similarity=0.6,
        score=1.0,
        scene_index=index,
    )


def test_stage5_global_assignment_moves_duplicate_onto_continuous_instance(
    monkeypatch, tmp_path
) -> None:
    _install_stage5_fakes(monkeypatch, tmp_path)
    # two long anchor chains sit on the true line (source = t + 5); the short
    # middle scene picked a duplicate instance 45s later whose index support
    # is only slightly stronger — global chronology must pull it back, and
    # the pixel veto must confirm (fabricated pixels follow the true line)
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=7.0),
            Scene(index=1, start_time=7.0, end_time=9.0),
            Scene(index=2, start_time=9.0, end_time=16.0),
        ]
    )
    remapped = [
        ([0], _segment("episode-01.mkv", 1.0, _TRUE_QUERY_TO_SOURCE, 0.0, 7.0, 0)),
        ([1], _segment("episode-01.mkv", 1.0, 50.0, 7.0, 9.0, 1)),
        ([2], _segment("episode-01.mkv", 1.0, _TRUE_QUERY_TO_SOURCE, 9.0, 16.0, 2)),
    ]
    raw = [
        (5.0, 12.0),
        (57.0, 59.0),
        (14.0, 21.0),
    ]
    correspondences = [
        _corr(0, 7.25, 57.25, similarity=0.58),
        _corr(1, 8.25, 58.25, similarity=0.57),
        _corr(2, 7.25, 12.25, similarity=0.53),
        _corr(3, 8.25, 13.25, similarity=0.53),
    ]
    deltas, doubts, _splits = SceneAlignerService._stage5_refine(
        Path("query.mp4"),
        scenes,
        remapped,
        [0, 1, 2],
        raw,
        "video",
        scene_segments={0: [], 1: [], 2: []},
        correspondences=correspondences,
    )
    assert "duplicate_rerank" in doubts.get(1, [])
    assert abs(raw[1][0] - (7.0 + _TRUE_QUERY_TO_SOURCE)) <= 0.3
    assert remapped[1][1].episode == "episode-01.mkv"


def test_stage5_global_assignment_keeps_static_content_isolated_scene(
    monkeypatch, tmp_path
) -> None:
    # static content: zoom-SSCD margins are zero, no chronology anchors —
    # the index-dominant current instance must be kept (no evidence basis
    # for a switch)
    _install_stage5_fakes(monkeypatch, tmp_path, static=True)
    scenes, remapped, raw = _one_scene_setup(a=1.0, b=50.0)
    correspondences = [
        _corr(0, 0.25, 50.25, similarity=0.58),
        _corr(1, 1.25, 51.25, similarity=0.57),
        _corr(2, 0.25, 5.25, similarity=0.52),
        _corr(3, 1.25, 6.25, similarity=0.53),
    ]
    deltas, doubts, _splits = SceneAlignerService._stage5_refine(
        Path("query.mp4"),
        scenes,
        remapped,
        [0],
        raw,
        "video",
        scene_segments={0: []},
        correspondences=correspondences,
    )
    assert "duplicate_rerank" not in doubts.get(0, [])
    assert abs(raw[0][0] - 50.0) <= 0.7

def test_native_tug_moves_boundary_to_content_transition(monkeypatch, tmp_path) -> None:
    from app.services.anime_library import AnimeLibraryService
    from app.services.anime_matcher import AnimeMatcherService

    episode_file = tmp_path / "episode-01.mkv"
    episode_file.touch()
    TRUE_CUT = 1.0

    class _FakeCap:
        def release(self) -> None:
            pass

    class _FakeCv2:
        @staticmethod
        def VideoCapture(_path: str) -> "_FakeCap":
            return _FakeCap()

    def embed(frames):
        out = []
        for f in frames:
            if isinstance(f, _QueryFrame):
                # query content follows the LEFT line before the true cut and
                # the RIGHT line after it
                src = f.t + 5.0 if f.t < TRUE_CUT else f.t + 100.0
            else:
                src = f.t
            out.append(_source_emb(src))
        return np.stack(out)

    monkeypatch.setattr(
        AnimeMatcherService,
        "extract_frames",
        classmethod(lambda cls, video_path, ts: [_QueryFrame(t) for t in ts]),
    )
    monkeypatch.setattr(
        AnimeMatcherService, "_embed_pil_batch", classmethod(lambda cls, fr: embed(fr))
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_collect_frames_in_window_from_capture",
        classmethod(
            lambda cls, cap, lo, hi, max_frames=48, sample_frames=None: [
                (float(t), _SourceFrame(float(t)))
                for t in np.arange(max(0.0, lo), hi, 1.0 / 24.0)
            ]
        ),
    )
    monkeypatch.setattr(
        AnimeMatcherService, "_require_cv2", staticmethod(lambda: _FakeCv2)
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(lambda cls, episode, **kwargs: episode_file),
    )

    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.3),
            Scene(index=1, start_time=1.3, end_time=2.5),
        ]
    )
    remapped = [
        ([0], _segment("episode-01.mkv", 1.0, 5.0, 0.0, 1.3, 0)),
        ([1], _segment("episode-01.mkv", 1.0, 100.0, 1.3, 2.5, 1)),
    ]
    result = SceneAlignerService._native_tug_boundaries(
        Path("query.mp4"), scenes, remapped, "video"
    )
    assert abs(result.scenes[0].end_time - TRUE_CUT) <= 0.06
    assert result.scenes[1].start_time == result.scenes[0].end_time

def test_pan_zero_crossing_localizes_translating_shot() -> None:
    from PIL import Image as PILImage

    rng = np.random.default_rng(3)
    texture = rng.integers(0, 255, (360, 1400), dtype=np.uint8)

    def frame_at(n: int) -> PILImage.Image:
        x0 = 4 * n  # 4 px/frame right-to-left pan
        return PILImage.fromarray(texture[:, x0 : x0 + 640])

    frames = [(n / 24.0, frame_at(n)) for n in range(62)]
    t_true = 30 / 24.0
    edge_gray = SceneAlignerService._small_gray(frame_at(30))

    t0 = SceneAlignerService._pan_zero_crossing(edge_gray, frames)

    assert t0 is not None
    assert abs(t0 - t_true) <= 0.06


def test_footprint_rect_recovers_offcenter_crop() -> None:
    from PIL import Image as PILImage

    rng = np.random.default_rng(7)
    # blocky texture gives ORB corners at every scale
    source = np.kron(
        rng.integers(0, 255, (36, 64), dtype=np.uint8), np.ones((10, 10), np.uint8)
    )
    sh, sw = source.shape  # 360 x 640
    # off-center full-height vertical crop: x in [0.30, 0.62)
    x0, x1 = int(0.30 * sw), int(0.62 * sw)
    query = np.asarray(
        PILImage.fromarray(source[:, x0:x1]).resize((202, 360))
    ).astype(np.float32)

    rect = SceneAlignerService._footprint_rect(query, source.astype(np.float32))

    assert rect is not None
    rx0, ry0, rx1, ry1 = rect
    assert abs(rx0 - 0.30) <= 0.04 and abs(rx1 - 0.62) <= 0.04
    assert ry0 <= 0.04 and ry1 >= 0.96


def test_zoom_crop_rect_mode_crops_fractional_footprint() -> None:
    from PIL import Image as PILImage

    img = PILImage.fromarray(np.zeros((360, 640), dtype=np.uint8))

    out = SceneAlignerService._zoom_crop(img, (0.25, 0.0, 0.75, 1.0))

    assert out.size == (320, 360)


def test_index_duplicate_recall_finds_unretrieved_instance(monkeypatch) -> None:
    class _Meta:
        def __init__(self, episode: str, timestamp: float) -> None:
            self.episode = episode
            self.timestamp = timestamp

    rng = np.random.default_rng(11)
    shot_a = rng.normal(size=512).astype(np.float32)
    shot_a /= np.linalg.norm(shot_a)
    other = rng.normal(size=512).astype(np.float32)
    other /= np.linalg.norm(other)

    # index grid: current instance at 100-101s, duplicate at 400-401s,
    # unrelated content elsewhere
    entries = {
        0: (_Meta("ep1", 100.0), shot_a),
        1: (_Meta("ep1", 100.5), shot_a),
        2: (_Meta("ep1", 101.0), shot_a),
        3: (_Meta("ep1", 400.0), shot_a),
        4: (_Meta("ep1", 400.5), shot_a),
        5: (_Meta("ep1", 401.0), shot_a),
        6: (_Meta("ep1", 250.0), other),
    }

    class _Index:
        def reconstruct(self, fid: int) -> np.ndarray:
            return entries[fid][1]

        def search(self, v: np.ndarray, k: int):
            sims = np.array([[float(v[0] @ e[1]) for e in entries.values()]])
            order = np.argsort(-sims[0])[:k]
            return sims[:, order], order[None, :]

    class _Manager:
        series_metadata = {"s": {fid: m for fid, (m, _) in entries.items()}}
        series_indices = {"s": _Index()}

    from app.services.anime_matcher import AnimeMatcherService

    monkeypatch.setattr(AnimeMatcherService, "_index_manager", _Manager())
    SceneAlignerService._episode_grid_cache.clear()

    # chain line sits on the 100s instance; recall must surface 400s
    cands = SceneAlignerService._index_duplicate_recall(
        "ep1", lambda t: 100.0 + (t - 10.0), [10.2, 10.5, 10.8]
    )

    assert cands, "duplicate instance not recalled"
    best = cands[0]
    assert best["episode"] == "ep1"
    t_mid = 10.5
    pos = float(best["a"]) * t_mid + float(best["b"])
    assert abs(pos - 400.5) <= 1.5


def test_episode_grid_cache_is_bounded_generation_scoped_and_does_not_pin_index(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(SceneAlignerService, "EPISODE_GRID_CACHE_MAX_ENTRIES", 2)
    SceneAlignerService.clear_index_caches()
    manager_one = object()

    for ordinal, episode in enumerate(("ep1", "ep2", "ep3")):
        metadata = {
            ordinal: SimpleNamespace(timestamp=float(ordinal), episode=episode)
        }
        grid = SceneAlignerService._episode_grid(
            manager_one, "Series", episode, metadata
        )
        assert grid is not None

    assert len(SceneAlignerService._episode_grid_cache) == 2
    assert all(
        len(value) == 2 for value in SceneAlignerService._episode_grid_cache.values()
    )
    assert all(
        key[0] == id(manager_one)
        for key in SceneAlignerService._episode_grid_cache
    )

    manager_two = object()
    metadata = {9: SimpleNamespace(timestamp=9.0, episode="ep3")}
    SceneAlignerService._episode_grid(manager_two, "Series", "ep3", metadata)
    assert any(key[0] == id(manager_two) for key in SceneAlignerService._episode_grid_cache)


def test_zoom_rate_measures_progressive_zoom_and_static() -> None:
    """D3 scale-velocity (v144-v155): a progressive zoom-out reads as a
    negative log-scale slope; a static shot reads ~0."""
    import cv2

    rng = np.random.default_rng(11)
    base = np.kron(
        rng.integers(0, 255, (48, 72), dtype=np.uint8),
        np.ones((10, 10), np.uint8),
    ).astype(np.float32)  # 480 x 720
    h, w = base.shape

    def zoomed(scale: float) -> np.ndarray:
        ch, cw = int(h / scale), int(w / scale)
        y0, x0 = (h - ch) // 2, (w - cw) // 2
        crop = base[y0 : y0 + ch, x0 : x0 + cw]
        return cv2.resize(crop, (640, 360)).astype(np.float32)

    # zoom-out at -8%/s over 1.5s: content scale factor exp(-0.08 t)
    frames_zoom = [
        (t, zoomed(1.3 * float(np.exp(-0.08 * t))))
        for t in (0.0, 0.5, 1.0, 1.5)
    ]
    rate_zoom = SceneAlignerService._zoom_rate(frames_zoom)
    assert rate_zoom is not None
    assert -0.12 <= rate_zoom <= -0.04

    frames_static = [(t, zoomed(1.3)) for t in (0.0, 0.5, 1.0, 1.5)]
    rate_static = SceneAlignerService._zoom_rate(frames_static)
    assert rate_static is not None
    assert abs(rate_static) <= 0.01

    # unmeasurable: too short a span
    assert (
        SceneAlignerService._zoom_rate(frames_static[:1]) is None
    )


def test_fadeout_tail_joins_previous_line_and_bright_tail_stays(monkeypatch, tmp_path) -> None:
    """The edit's final fadeout-to-black (owner round-8, 411f#51) joins the
    previous chain's line; a bright trailing no-match (appended non-anime
    content, 5e85#45) stays an honest no-match."""
    from PIL import Image as PILImage

    from app.services.anime_matcher import AnimeMatcherService

    scenes = [
        Scene(index=0, start_time=0.0, end_time=2.0),
        Scene(index=1, start_time=2.0, end_time=2.6),
    ]
    seg = SegmentHypothesis(
        id=1, episode="ep-01.mkv", tiktok_start=0.0, tiktok_end=2.0,
        a=1.0, b=100.0, inlier_count=10, mean_similarity=0.8, score=1.0,
        scene_index=0,
    )

    def run_with(tail_level: int):
        remapped = [([0], seg), ([1], None)]
        raw = [(100.0, 102.0), None]
        doubts: dict[int, list[str]] = {}
        img = PILImage.new("RGB", (64, 64), (tail_level,) * 3)
        monkeypatch.setattr(
            AnimeMatcherService,
            "extract_frames",
            classmethod(lambda cls, video_path, ts: [img for _ in ts]),
        )
        SceneAlignerService._recover_no_match(
            tmp_path / "v.mp4", scenes, raw, remapped, {}, [],
            cache=None, trusted_floor=0.75, doubts=doubts,
        )
        return raw, remapped, doubts

    raw, remapped, doubts = run_with(tail_level=2)  # black fadeout
    assert raw[1] == (102.0, 102.6)
    assert remapped[1][1] is not None and remapped[1][1].episode == "ep-01.mkv"
    assert "fadeout_tail" in doubts.get(1, [])

    raw, remapped, doubts = run_with(tail_level=140)  # bright real content
    assert raw[1] is None and remapped[1][1] is None
