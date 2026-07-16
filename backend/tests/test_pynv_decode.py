from __future__ import annotations

import threading

import numpy as np

from app.services import pynv_decode


def _legacy_indices(
    start_ts: float,
    end_ts: float,
    fps: float,
    num_frames: int,
    max_frames: int,
    sample_frames: int | None,
) -> list[tuple[int, float]]:
    start_ts = max(0.0, start_ts)
    candidates: list[tuple[int, float]] = []
    n = int(round(start_ts * fps))
    while len(candidates) < max_frames and n < num_frames:
        pos_ts = (n - 1) / fps
        if pos_ts > end_ts:
            break
        if pos_ts >= start_ts and n >= 0:
            candidates.append((n, pos_ts))
        n += 1
    if sample_frames is not None and len(candidates) > sample_frames:
        positions = np.linspace(
            0, len(candidates) - 1, sample_frames, dtype=np.int32
        )
        return [candidates[int(position)] for position in positions]
    return candidates


def test_window_frame_indices_match_post_decode_sampling() -> None:
    cases = [
        (0.0, 1.0, 24.0, 100, 48, None),
        (0.0, 1.0, 24.0, 100, 48, 12),
        (2.137, 4.981, 23.976, 500, 222, 35),
        (-1.0, 0.2, 60.0, 20, 8, 3),
        (9.8, 20.0, 25.0, 250, 100, 10),
        (0.0, 1.0, 24.0, 100, 0, 0),
    ]

    for args in cases:
        candidates = pynv_decode._window_candidate_indices(*args[:5])
        assert pynv_decode._sample_window_frame_indices(
            candidates, args[5]
        ) == _legacy_indices(*args)


def test_decode_window_only_converts_selected_frames(monkeypatch) -> None:
    requested: list[int] = []
    converted: list[int] = []

    class FakeDecoder:
        def __getitem__(self, index: int) -> int:
            requested.append(index)
            return index

    class FakeSession:
        fps = 24.0
        width = 1
        height = 1
        num_frames = 100
        lock = threading.Lock()
        decoder = FakeDecoder()

    class FakePool:
        @staticmethod
        def get(_path: str) -> FakeSession:
            return FakeSession()

    class FakeGpuRgb:
        def __init__(self, index: int) -> None:
            self.index = index

        def cpu(self) -> "FakeGpuRgb":
            return self

        def numpy(self) -> np.ndarray:
            return np.full((1, 1, 3), self.index, dtype=np.uint8)

    monkeypatch.setattr(pynv_decode, "_POOL", FakePool())
    monkeypatch.setattr(
        pynv_decode,
        "_native_frame_to_rgb_gpu",
        lambda frame, _width, _height: converted.append(frame) or FakeGpuRgb(frame),
    )

    frames = pynv_decode.decode_window(
        "episode.mkv",
        start_ts=0.0,
        end_ts=1.0,
        max_frames=24,
        sample_frames=4,
    )

    assert requested == list(range(1, 25))
    assert converted == [1, 8, 16, 24]
    assert [round(timestamp, 6) for timestamp, _image in frames] == [
        0.0,
        round(7 / 24, 6),
        round(15 / 24, 6),
        round(23 / 24, 6),
    ]
