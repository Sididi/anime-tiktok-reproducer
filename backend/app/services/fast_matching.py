"""FAST MODE master switch + numeric configuration (GOAL_FAST.md).

``ATR_FAST_MATCHING`` gates the owner-facing speed variant. In this branch it
defaults ON so the owner tests by simply running a project; ``0``/``off``/
``false``/``no`` forces the exact mainline cv2/fp32 path (the keep-or-discard
mechanism — trivially reversible).

This module owns only the *decision*: the flag reader, the embedder precision it
implies (F3), and the process-global TF32 toggle (F3). GPU window decode (F1)
lives in :mod:`.pynv_decode`; both consult the same flag semantics so a single
``ATR_FAST_MATCHING=0`` reverts everything. When CUDA/PyNv are absent the fast
switches are inert, so flag-ON on a CPU-only box is identical to mainline too.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_FAST_FLAG = "ATR_FAST_MATCHING"
_PRECISION_OVERRIDE = "ATR_FAST_PRECISION"  # experiment only: force fp16 (BROKEN) / fp32
_DECODE_OVERRIDE = "ATR_FAST_DECODE"        # F1 lever: 0 keeps cv2 decode in fast mode
_NUMERICS_OVERRIDE = "ATR_FAST_NUMERICS"    # F3 lever: 0 disables TF32 (fp32 baseline)

_tf32_configured = False


def _off(val: str | None) -> bool:
    return val is not None and val.strip().lower() in {"0", "off", "false", "no"}


def fast_enabled() -> bool:
    """True unless ``ATR_FAST_MATCHING`` is explicitly disabled (branch default
    ON, per GOAL_FAST §0)."""
    return not _off(os.environ.get(_FAST_FLAG))


def decode_enabled() -> bool:
    """Whether GPU (PyNv) window decode is requested (F1 lever).

    On when fast mode is on, unless ``ATR_FAST_DECODE=0`` isolates it out (keeps
    cv2 decode with the TF32 embedder — lets the owner measure F3 alone).
    """
    return fast_enabled() and not _off(os.environ.get(_DECODE_OVERRIDE))


def numerics_enabled() -> bool:
    """Whether TF32 matmul/conv is requested on the SSCD path (F3 lever).

    On in fast mode unless ``ATR_FAST_NUMERICS=0`` isolates it out (fp32 no-TF32
    numeric baseline, for measuring F1 decode alone).
    """
    return fast_enabled() and not _off(os.environ.get(_NUMERICS_OVERRIDE))


def embedder_precision(default: str = "fp32") -> str:
    """Precision to build the SSCD embedder with.

    Fast mode → **fp32** (with TF32 matmul; see :func:`configure_numerics`).
    MEASURED FACT (vF3, 2026-07-16): the ``.half()`` SSCD torchscript model
    collapses — cos(fp32, fp16) = 0.079, orthogonal garbage — so fp16 destroys
    matching (Source 0/20, everything no-match) and is NOT usable; bf16 is
    unsupported by the embedder. ``ATR_FAST_PRECISION=fp16`` is retained only so
    the owner can reproduce that broken delta on demand. Mainline keeps
    ``default`` (fp32).
    """
    if not fast_enabled():
        return default
    override = os.environ.get(_PRECISION_OVERRIDE, "").strip().lower()
    if override in {"fp16", "fp32"}:
        return override
    return "fp32"


def configure_numerics() -> None:
    """Enable TF32 matmul/conv on the SSCD model path when fast mode is on (F3).

    TF32 is bit-safe on this model (cos(fp32, fp32+TF32) = 1.000000, vF3) and
    speeds the ResNet forward on Ada. Global and idempotent; only touched inside
    the fast branch so the mainline (flag-off) path leaves PyTorch's defaults
    exactly as they were — required for flag-off byte-identity. ``ATR_FAST_
    NUMERICS=0`` skips it for the F1-isolation baseline.
    """
    global _tf32_configured
    if _tf32_configured or not numerics_enabled():
        return
    try:
        import torch

        if torch.cuda.is_available():
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            _tf32_configured = True
            logger.info("fast mode: TF32 matmul/cudnn enabled")
    except Exception as exc:  # pragma: no cover - env dependent
        logger.warning("fast mode: could not enable TF32: %s", exc)


# ---------------------------------------------------------------------------
# Round 2 (2026-07-23 spec): wall-time levers behind a master switch.
# ATR_FAST_R2 rides on top of ATR_FAST_MATCHING: fast mode OFF kills R2 too,
# so ATR_FAST_MATCHING=0 stays the proven byte-identical mainline escape hatch.
# ---------------------------------------------------------------------------
_R2_FLAG = "ATR_FAST_R2"
_MATCHER_V2_FLAG = "ATR_MATCHER_V2"


def fast_r2_enabled() -> bool:
    if not fast_enabled():
        return False
    return not _off(os.environ.get(_R2_FLAG))


def r2_lever(name: str, default: bool = True) -> bool:
    """Per-lever toggle (e.g. ATR_R2_COARSE); dead when the master is off."""
    if not fast_r2_enabled():
        return False
    val = os.environ.get(name)
    if val is None:
        return default
    return not _off(val)


def matcher_v2_enabled() -> bool:
    """Whether the original matcher (the V2 compatibility path) is selected.

    The rollout flag is intentionally inverted: ``ATR_MATCHER_V2=1`` opts into
    the old matcher, while an unset flag or a normal false value selects the
    bounded hierarchical matcher by default.  This keeps the fast bounded
    path as the normal application behavior and preserves the old matcher as
    an explicit escape hatch.

    Explicit process environment takes precedence.  When the process was
    started from the backend launcher, the value may instead come from the
    application's pydantic ``.env`` settings; pydantic reads that file into
    ``settings`` and (correctly) does not export it into ``os.environ``.
    """
    value = os.environ.get(_MATCHER_V2_FLAG)
    # An exported-but-empty value is not a choice. Treating it as one would
    # silently select the old matcher, while the same empty value in .env makes
    # pydantic raise — so fall through to settings instead.
    if value is not None and value.strip():
        return not _off(value)

    # Keep this import lazy: fast_matching is also imported by lightweight
    # tests and diagnostics that do not need to initialize the full settings
    # object.  The backend's normal app import has already constructed it.
    try:
        from ..config import settings

        return bool(settings.matcher_v2)
    except Exception:  # pragma: no cover - defensive fallback for utilities
        return False


def bounded_matcher_enabled() -> bool:
    """Whether the new bounded hierarchical matcher should run by default."""
    return not matcher_v2_enabled()
