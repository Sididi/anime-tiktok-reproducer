"""GPU source decode for the matcher's window primitive (FAST MODE / F1).

Wired copy of the proven-ready recipe from
``backend/scripts/diagnostics/pynv_decode.py`` (GOAL v5.3, unwired reference,
kept intact). Persistent, seekable, in-process NVDEC via NVIDIA's
PyNvVideoCodec: a single ``SimpleDecoder`` session per source file, LRU-bounded,
serving scattered index windows without per-access subprocess spawn (the v168
verdict killed the subprocess route). Decode runs on the GPU decoder engine;
the host CPU un-throttles (v167/v169: ~37% of one core vs cv2's ~630%).

Interchangeable with a ``cv2.VideoCapture`` at the one dispatch call site,
``AnimeMatcherService._collect_frames_in_window_from_capture``: a
:class:`PyNvCap` carries only the source path and the primitive routes it to
:func:`decode_window`, which reproduces cv2's ``CAP_PROP_POS_MSEC`` window
selection exactly for CFR sources.

Enablement: fast mode (``ATR_FAST_MATCHING`` on, the default in this branch)
implies GPU decode; the legacy ``ATR_PYNV_DECODE`` flag still forces it on
independently. Either way availability is re-checked per file — import failure,
no CUDA, or a decoder that will not open the actual file all fall back
transparently to cv2, logged, with mainline behaviour intact.

Reconstruction (recipe §0.4a): the NATIVE dlpack descriptor mis-reports the
P016 column stride as elements when it is bytes; the buffer is contiguous full
P016/NV12 so we rebuild with ``as_strided``. 10-bit codes live in the MSBs
(value/64 → 8-bit is value/256). Colour: swscale treats these untagged sources
as BT.601 limited and we convert ourselves (PyNv's built-in RGB matrix drifts
~2.9). Alignment: ``decoder[i] == cv2.set(POS_FRAMES, i)`` for both streams.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# BT.601 limited-range constants (Kr=0.299, Kb=0.114).
_KR_R = 1.402
_KG_U = 0.344136
_KG_V = 0.714136
_KB_B = 1.772

# At most this many decoder sessions stay open at once (recipe §0.4d: ~412 MiB
# each, 8 GB shared with the SSCD embedder). LRU-evicted sessions are stopped so
# their VRAM is returned. Under fast-mode concurrency (§4) two matchings share
# this process-global pool, so the ceiling bounds decoder VRAM across both.
_MAX_SESSIONS = 3

_ENV_FLAG = "ATR_PYNV_DECODE"

_import_lock = threading.Lock()
_nvc = None  # cached module handle
_import_failed = False


def enabled() -> bool:
    """Whether the PyNv decode path is *requested* for this process (F1).

    Requested when fast-mode GPU decode is on (``fast_matching.decode_enabled``,
    the branch default) OR the legacy ``ATR_PYNV_DECODE`` opt-in is set.
    ``ATR_FAST_MATCHING=0`` forces the exact mainline cv2 path unless
    ``ATR_PYNV_DECODE`` explicitly re-enables it; ``ATR_FAST_DECODE=0`` isolates
    GPU decode out while keeping the fp16/TF32 embedder. Availability (import +
    a live decoder on the actual file) is checked per-file in
    :func:`open_capture`.
    """
    legacy = os.environ.get(_ENV_FLAG, "").strip().lower()
    if legacy in {"1", "auto", "on", "true", "yes"}:
        return True
    if legacy in {"0", "off", "false", "no"}:
        return False
    from . import fast_matching

    return fast_matching.decode_enabled()


def _load_nvc():
    global _nvc, _import_failed
    if _nvc is not None or _import_failed:
        return _nvc
    with _import_lock:
        if _nvc is not None or _import_failed:
            return _nvc
        try:
            import PyNvVideoCodec as nvc  # noqa: N813

            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("CUDA unavailable")
            _nvc = nvc
        except Exception as exc:  # pragma: no cover - env-dependent
            logger.warning("PyNvVideoCodec unavailable, falling back to cv2: %s", exc)
            _import_failed = True
    return _nvc


class PyNvCap:
    """Lightweight stand-in for a ``cv2.VideoCapture``.

    Holds only the source path; the heavy ``SimpleDecoder`` session lives in the
    shared :class:`_SessionPool` (bounded VRAM), serialised by that session's
    lock. Dispatched on type at the single window-primitive call site.
    ``release()`` is a no-op (the pool owns lifecycle) so existing capture
    bookkeeping — including the ``finally: cap.release()`` in every caller — is
    untouched.
    """

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def release(self) -> None:  # cv2 API parity; the pool owns lifecycle
        return None

    def get(self, *_args, **_kwargs):  # defensive: nothing should call this
        return 0.0


class _Session:
    __slots__ = ("decoder", "lock", "width", "height", "fps", "num_frames")

    def __init__(self, decoder, width, height, fps, num_frames) -> None:
        self.decoder = decoder
        self.lock = threading.Lock()
        self.width = width
        self.height = height
        self.fps = fps
        self.num_frames = num_frames


class _SessionPool:
    """Process-global LRU of open ``SimpleDecoder`` sessions."""

    def __init__(self, max_sessions: int = _MAX_SESSIONS) -> None:
        self._max = max_sessions
        self._pool: "OrderedDict[str, _Session]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: str) -> _Session:
        with self._lock:
            sess = self._pool.get(path)
            if sess is not None:
                self._pool.move_to_end(path)
                return sess
        # Build outside the pool lock (decoder init ~0.23s): a concurrent
        # builder for the same path just means one extra transient session,
        # resolved by the re-check below.
        sess = self._build(path)
        with self._lock:
            existing = self._pool.get(path)
            if existing is not None:
                self._pool.move_to_end(path)
                self._stop(sess)
                return existing
            self._pool[path] = sess
            self._pool.move_to_end(path)
            while len(self._pool) > self._max:
                _old_path, old = self._pool.popitem(last=False)
                self._stop(old)
            return sess

    def _build(self, path: str) -> _Session:
        nvc = _load_nvc()
        if nvc is None:
            raise RuntimeError("PyNvVideoCodec unavailable")
        decoder = nvc.SimpleDecoder(
            path,
            gpu_id=0,
            use_device_memory=True,
            output_color_type=nvc.OutputColorType.NATIVE,
        )
        md = decoder.get_stream_metadata()
        return _Session(decoder, md.width, md.height, md.average_fps, md.num_frames)

    @staticmethod
    def _stop(sess: _Session) -> None:
        try:
            stop = getattr(sess.decoder, "stop", None)
            if callable(stop):
                stop()
        except Exception:  # pragma: no cover
            pass
        sess.decoder = None

    def close_all(self) -> None:
        with self._lock:
            while self._pool:
                _p, s = self._pool.popitem(last=False)
                self._stop(s)

    def session_count(self) -> int:
        with self._lock:
            return len(self._pool)


_POOL = _SessionPool()


def close_pool() -> None:
    """Release all decoder sessions (VRAM). Called on matcher teardown so a
    fast run does not leave NVDEC sessions pinned for the next queue task."""
    _POOL.close_all()


def _probe(path: str) -> bool:
    """Create (and cache) a session for ``path``; True if the GPU path is live."""
    try:
        _POOL.get(path)
        return True
    except Exception as exc:
        logger.warning("PyNv decode unavailable for %s, using cv2: %s", path, exc)
        return False


def open_capture(path: str):
    """Return a :class:`PyNvCap` when the GPU path is requested AND live for
    this file, else ``None`` so the caller opens a cv2 capture. Transparent,
    per-file, logged."""
    if not enabled():
        return None
    if _load_nvc() is None:
        return None
    if not _probe(str(path)):
        return None
    return PyNvCap(str(path))


import torch  # noqa: E402  (after the optional-import guard above)


def _native_frame_to_rgb_gpu(frame, width: int, height: int) -> torch.Tensor:
    """One P016/NV12 NATIVE device frame -> HxWx3 uint8 RGB (BT.601 limited),
    left on the GPU. Every op after ``from_dlpack`` allocates a fresh tensor, so
    the returned tensor is independent of the decoder's internal buffer — safe
    to accumulate across the window before a single host transfer (the decoder
    may recycle the buffer that ``frame`` views).
    """
    t = torch.from_dlpack(frame)
    hf = (height * 3) // 2
    plane = t.as_strided((hf, width), (width, 1)).to(torch.float32)
    if t.dtype == torch.uint16:
        plane = plane / 256.0  # 10-bit MSB codes (/64) to 8-bit (/4)
    y = plane[:height, :width]
    uv = plane[height:height + height // 2, :width].reshape(
        height // 2, width // 2, 2
    )
    u = uv[..., 0].repeat_interleave(2, 0).repeat_interleave(2, 1)
    v = uv[..., 1].repeat_interleave(2, 0).repeat_interleave(2, 1)
    c = (y - 16.0) * (255.0 / 219.0)
    d = (u - 128.0) * (255.0 / 224.0)
    e = (v - 128.0) * (255.0 / 224.0)
    r = c + _KR_R * e
    g = c - _KG_U * d - _KG_V * e
    b = c + _KB_B * d
    rgb = torch.stack([r, g, b], dim=-1).clamp_(0, 255)
    return torch.round(rgb).to(torch.uint8)


def decode_window(
    path: str,
    start_ts: float,
    end_ts: float,
    max_frames: int = 48,
    sample_frames: int | None = None,
) -> list[tuple[float, Image.Image]]:
    """PyNv equivalent of ``_collect_frames_in_window_from_capture``.

    Reproduces cv2's ``CAP_PROP_POS_MSEC`` window semantics exactly for CFR
    sources: landing index ``round(start*F)``; each frame carries
    ``pos_ts = (index - 1)/F`` (cv2's reported msec); a frame is kept when
    ``start_ts <= pos_ts <= end_ts``, capped at ``max_frames`` then subsampled
    with the identical ``np.linspace`` rule. Content at POS_FRAMES ``n`` is
    ``decoder[n]`` (alignment offset +0).
    """
    sess = _POOL.get(str(path))
    fps = sess.fps
    width, height = sess.width, sess.height
    num_frames = sess.num_frames or (1 << 30)
    start_ts = max(0.0, start_ts)
    kept_ts: list[float] = []
    gpu_rgbs: list[torch.Tensor] = []
    n = int(round(start_ts * fps))
    with sess.lock:
        decoder = sess.decoder
        while len(kept_ts) < max_frames and n < num_frames:
            pos_ts = (n - 1) / fps
            if pos_ts > end_ts:
                break
            if pos_ts >= start_ts and n >= 0:
                # Convert immediately to an independent GPU tensor: the decoder
                # may recycle the buffer that ``decoder[n]`` views, so we must
                # not hold raw frames past this iteration.
                gpu_rgbs.append(_native_frame_to_rgb_gpu(decoder[n], width, height))
                kept_ts.append(pos_ts)
            n += 1
    if gpu_rgbs:
        stacked = torch.stack(gpu_rgbs, dim=0).cpu().numpy()  # one host transfer
        rgbs = [stacked[i] for i in range(stacked.shape[0])]
    else:
        rgbs = []
    frames = [
        (ts, Image.fromarray(rgb)) for ts, rgb in zip(kept_ts, rgbs, strict=False)
    ]
    if sample_frames is not None and len(frames) > sample_frames:
        indices = np.linspace(0, len(frames) - 1, sample_frames, dtype=np.int32)
        return [frames[int(i)] for i in indices]
    return frames
