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


def _make_session(decoder) -> pynv_decode._Session:
    return pynv_decode._Session(
        decoder, width=1, height=1, fps=24.0, num_frames=100
    )


def test_decode_window_retries_evicted_session(monkeypatch) -> None:
    """A session nulled between the pool lookup and its lock (LRU eviction)
    must be retried against a fresh session, never raise TypeError."""

    class FakeDecoder:
        def __getitem__(self, index: int) -> int:
            return index

    evicted = _make_session(None)  # already stopped
    alive = _make_session(FakeDecoder())
    sessions = [evicted, alive]

    class FakePool:
        @staticmethod
        def get(_path: str) -> pynv_decode._Session:
            return sessions.pop(0) if len(sessions) > 1 else sessions[0]

    class FakeGpuRgb:
        def cpu(self) -> "FakeGpuRgb":
            return self

        @staticmethod
        def numpy() -> np.ndarray:
            return np.zeros((1, 1, 3), dtype=np.uint8)

    monkeypatch.setattr(pynv_decode, "_POOL", FakePool())
    monkeypatch.setattr(pynv_decode, "_vram_pressure", lambda: (False, 0))
    monkeypatch.setattr(
        pynv_decode,
        "_native_frame_to_rgb_gpu",
        lambda frame, _width, _height: FakeGpuRgb(),
    )

    frames = pynv_decode.decode_window(
        "episode.mkv", start_ts=0.0, end_ts=0.5, max_frames=8, sample_frames=2
    )
    assert len(frames) == 2


def test_decode_window_gives_up_after_repeated_eviction(monkeypatch) -> None:
    class FakePool:
        @staticmethod
        def get(_path: str) -> pynv_decode._Session:
            return _make_session(None)

    monkeypatch.setattr(pynv_decode, "_POOL", FakePool())
    monkeypatch.setattr(pynv_decode, "_vram_pressure", lambda: (False, 0))

    try:
        pynv_decode.decode_window("episode.mkv", start_ts=0.0, end_ts=0.5)
    except pynv_decode.PyNvDecodeUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected PyNvDecodeUnavailable")


def test_decode_window_refuses_under_vram_pressure(monkeypatch) -> None:
    calls: list[str] = []

    class FakePool:
        @staticmethod
        def get(path: str) -> pynv_decode._Session:
            calls.append(path)
            raise AssertionError("pool must not be touched under pressure")

    monkeypatch.setattr(pynv_decode, "_POOL", FakePool())
    monkeypatch.setattr(pynv_decode, "_vram_pressure", lambda: (True, 600))

    try:
        pynv_decode.decode_window("episode.mkv", start_ts=0.0, end_ts=0.5)
    except pynv_decode.PyNvDecodeUnavailable:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected PyNvDecodeUnavailable")
    assert calls == []


def test_session_pool_stop_is_serialised_and_null_safe() -> None:
    """_stop nulls the decoder under the session lock and tolerates repeats."""
    sess = _make_session(object())
    pynv_decode._SessionPool._stop(sess)
    assert sess.decoder is None
    pynv_decode._SessionPool._stop(sess)  # idempotent


def test_session_pool_eviction_waits_for_inflight_decode() -> None:
    """Eviction (_stop) must block until an in-flight holder of sess.lock is
    done — the decoder is never nulled under a live decode."""
    import time

    sess = _make_session(object())
    order: list[str] = []

    def holder() -> None:
        with sess.lock:
            order.append("decode-start")
            time.sleep(0.2)
            assert sess.decoder is not None
            order.append("decode-end")

    t = threading.Thread(target=holder)
    t.start()
    time.sleep(0.05)
    pynv_decode._SessionPool._stop(sess)
    order.append("stopped")
    t.join()
    assert order == ["decode-start", "decode-end", "stopped"]
    assert sess.decoder is None


def test_should_fallback_to_cv2_predicate() -> None:
    class PyNvVCException(Exception):  # same name as the native error type
        pass

    assert pynv_decode.should_fallback_to_cv2(
        pynv_decode.PyNvDecodeUnavailable("gate")
    )
    assert pynv_decode.should_fallback_to_cv2(RuntimeError("CUDA out of memory"))
    assert pynv_decode.should_fallback_to_cv2(
        PyNvVCException("HandlePictureDisplay :\nError code : 208")
    )
    assert not pynv_decode.should_fallback_to_cv2(ValueError("unrelated"))
    assert not pynv_decode.should_fallback_to_cv2(TypeError("also unrelated"))
