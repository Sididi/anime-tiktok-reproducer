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
# each, 8 GB shared with the SSCD embedder). LRU-evicted sessions are dropped so
# their VRAM is returned when the last in-flight reference dies (upstream
# 2.1.0's SimpleDecoder.stop() is broken — the native object has no ``stop`` —
# so teardown is refcount-driven). Kept at 2 (not 3): under fast-mode
# concurrency (§4) TWO processes each hold their own pool, so 2
# sessions/process is 4 decoders (~1.6 GB) on the shared card — 3 would push
# the peak into the embed's OOM margin. One matching only ever touches ~1-2
# source files per window anyway.
_MAX_SESSIONS = 2

_ENV_FLAG = "ATR_PYNV_DECODE"

# Free-VRAM floor below which window decode is refused (raised as
# :class:`PyNvDecodeUnavailable`, caught by the matcher's per-window cv2
# fallback). PyNvVideoCodec's allocation-failure path SEGFAULTS the process
# under concurrent session use (2026-07-19 crash, reproduced): when the card is
# this full — NVENC preview encodes, a second matching, indexation — NVDEC must
# not even be attempted. cv2 frames are byte-identical, only slower.
_MIN_FREE_MIB_ENV = "ATR_PYNV_MIN_FREE_MIB"
_DEFAULT_MIN_FREE_MIB = 600

# Retries when the session we fetched was LRU-evicted between the pool lookup
# and taking its lock (the pool rebuilds a fresh one on re-get).
_EVICTION_RETRIES = 3

_import_lock = threading.Lock()
_nvc = None  # cached module handle
_import_failed = False

# Serialises EVERY native SimpleDecoder call (session build + indexed decode)
# process-wide. PyNvVideoCodec 2.1.0 is not safe with two sessions inside
# ``__getitem__`` concurrently once VRAM runs short: its cuvid error path
# corrupts state and SIGSEGVs (reproduced 2026-07-19; two backend crashes in
# production). Per-session locks still serialise same-file windows; this lock
# closes the cross-session hole. Decode throughput is unaffected in practice —
# the card has a single NVDEC engine.
_NATIVE_LOCK = threading.Lock()


class PyNvDecodeUnavailable(RuntimeError):
    """GPU window decode refused/aborted; caller should decode via cv2."""


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
            if sess is not None and sess.decoder is not None:
                self._pool.move_to_end(path)
                return sess
        # Build outside the pool lock (decoder init ~0.23s): a concurrent
        # builder for the same path just means one extra transient session,
        # resolved by the re-check below.
        sess = self._build(path)
        evicted: list[_Session] = []
        with self._lock:
            existing = self._pool.get(path)
            if existing is not None and existing.decoder is not None:
                self._pool.move_to_end(path)
                evicted.append(sess)
                sess = existing
            else:
                self._pool[path] = sess
                self._pool.move_to_end(path)
                while len(self._pool) > self._max:
                    _old_path, old = self._pool.popitem(last=False)
                    evicted.append(old)
        # Tear down evicted sessions outside the pool lock: _stop waits on each
        # session's own lock, i.e. on any in-flight window decode — that wait
        # must not stall unrelated pool lookups.
        for old in evicted:
            self._stop(old)
        return sess

    def _build(self, path: str) -> _Session:
        nvc = _load_nvc()
        if nvc is None:
            raise RuntimeError("PyNvVideoCodec unavailable")
        with _NATIVE_LOCK:
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
        # Serialise with any in-flight decode_window on this session: the
        # decoder must never be nulled (let alone torn down) under a live
        # native call — that race produced silent TypeErrors in every
        # multi-file prefetch run.
        with sess.lock:
            decoder, sess.decoder = sess.decoder, None
        if decoder is None:
            return
        try:
            stop = getattr(decoder, "stop", None)
            if callable(stop):
                stop()  # upstream 2.1.0 raises AttributeError; VRAM is
                # actually freed when the last reference dies
        except Exception:  # pragma: no cover
            pass

    def invalidate(self, path: str) -> None:
        with self._lock:
            sess = self._pool.pop(path, None)
        if sess is not None:
            self._stop(sess)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._pool.values())
            self._pool.clear()
        for s in sessions:
            self._stop(s)

    def session_count(self) -> int:
        with self._lock:
            return len(self._pool)


_POOL = _SessionPool()


def close_pool() -> None:
    """Release all decoder sessions (VRAM). Called on matcher teardown so a
    fast run does not leave NVDEC sessions pinned for the next queue task."""
    _POOL.close_all()


def invalidate_session(path: str) -> None:
    """Drop the pooled session for ``path`` after a decode error: a session
    that has been through a cuvid failure (VRAM pressure) must not be reused —
    its internal state can be corrupt (the 2026-07-19 SIGSEGV class). The next
    window rebuilds fresh, or falls back to cv2 if the card is still full."""
    _POOL.invalidate(str(path))


def should_fallback_to_cv2(exc: BaseException) -> bool:
    """Is this decode_window failure one the matcher should absorb by decoding
    the window on cv2 (byte-identical, slower) instead of failing the probe?

    Covers the VRAM-pressure gate, torch CUDA OOM ("out of memory"), and every
    PyNvVideoCodec native error — cuvid failures report "Error code : 208"-style
    texts that the old "out of memory" substring check silently missed."""
    if isinstance(exc, PyNvDecodeUnavailable):
        return True
    if "out of memory" in str(exc).lower():
        return True
    if _nvc is not None:
        native_exc = getattr(_nvc, "PyNvVCException", None)
        if native_exc is not None and isinstance(exc, native_exc):
            return True
    return type(exc).__name__.startswith("PyNvVC")


def _vram_pressure() -> tuple[bool, int]:
    """(free VRAM below the floor?, floor in MiB). Unknown free => no gate."""
    raw = os.environ.get(_MIN_FREE_MIB_ENV, "").strip()
    try:
        floor_mib = int(raw) if raw else _DEFAULT_MIN_FREE_MIB
    except ValueError:
        floor_mib = _DEFAULT_MIN_FREE_MIB
    if floor_mib <= 0:
        return False, floor_mib
    try:
        free, _total = torch.cuda.mem_get_info()
    except Exception:  # pragma: no cover - env-dependent
        return False, floor_mib
    return free < floor_mib * (1 << 20), floor_mib


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


def _window_candidate_indices(
    start_ts: float,
    end_ts: float,
    fps: float,
    num_frames: int,
    max_frames: int,
) -> list[tuple[int, float]]:
    """Return the native frames visited by the legacy CFR decode loop."""
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
    return candidates


def _sample_window_frame_indices(
    candidates: list[tuple[int, float]],
    sample_frames: int | None,
) -> list[tuple[int, float]]:
    """Apply the legacy post-decode linspace selection without pixel copies."""

    if sample_frames is not None and len(candidates) > sample_frames:
        positions = np.linspace(
            0,
            len(candidates) - 1,
            sample_frames,
            dtype=np.int32,
        )
        return [candidates[int(position)] for position in positions]
    return candidates


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
    pressured, floor_mib = _vram_pressure()
    if pressured:
        raise PyNvDecodeUnavailable(
            f"free VRAM below {floor_mib} MiB; NVDEC refused for this window"
        )
    for _attempt in range(_EVICTION_RETRIES):
        sess = _POOL.get(str(path))
        fps = sess.fps
        width, height = sess.width, sess.height
        num_frames = sess.num_frames or (1 << 30)
        candidate_frames = _window_candidate_indices(
            start_ts,
            end_ts,
            fps,
            num_frames,
            max_frames,
        )
        selected_frames = _sample_window_frame_indices(
            candidate_frames,
            sample_frames,
        )
        selected_indices = {index for index, _timestamp in selected_frames}
        frames: list[tuple[float, Image.Image]] = []
        with sess.lock:
            if sess.decoder is None:
                # Evicted between the pool lookup and our lock; re-get builds
                # a fresh session.
                continue
            decoder = sess.decoder
            with _NATIVE_LOCK:
                for n, pos_ts in candidate_frames:
                    # Keep the decoder's original sequential access pattern:
                    # NVIDIA's stateful GOP traversal can produce small
                    # boundary differences if indices are skipped. Only
                    # selected frames pay for RGB conversion and the
                    # full-resolution device-to-host copy.
                    native_frame = decoder[n]
                    if n not in selected_indices:
                        continue
                    rgb = (
                        _native_frame_to_rgb_gpu(native_frame, width, height)
                        .cpu()
                        .numpy()
                    )
                    frames.append((pos_ts, Image.fromarray(rgb)))
        return frames
    raise PyNvDecodeUnavailable(
        f"decoder session for {path} evicted {_EVICTION_RETRIES} times in a row"
    )
