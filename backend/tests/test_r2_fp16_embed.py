from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from app.services.anime_matcher import AnimeMatcherService


def test_embed_pil_batch_has_half_kwarg():
    sig = inspect.signature(AnimeMatcherService._embed_pil_batch.__func__)
    assert "half" in sig.parameters
    assert sig.parameters["half"].default is False


def test_variant_embeds_never_half():
    # index-facing embeds must not grow a half switch: the vF3 ban.
    from app.services import scene_aligner

    src = inspect.getsource(scene_aligner.SceneAlignerService._embed_variant_images)
    assert "half=True" not in src


def test_stage5_edge_mid_embed_stays_fp32():
    """vF16 audit finding: the brief listed the stage-5 edge/mid embed as a
    safe half=True site, but its output (edge_embs -> mid_embs/edge_queries)
    is reused verbatim as `_query_deep_recall`'s query, which runs a FAISS
    `index.search` + a direct cosine against `index.reconstruct`-ed index
    vectors (see `_query_deep_recall`, `scene_aligner.py`). That is the vF3
    hard rule violation this task's audit step exists to catch. This
    consumer must therefore stay fp32 — pin the exact `edge_embs = ...`
    call line so a future edit can't silently reintroduce the issue."""
    from app.services import scene_aligner

    src = inspect.getsource(scene_aligner.SceneAlignerService._stage5_refine)
    for line in src.splitlines():
        if "edge_embs = AnimeMatcherService._embed_pil_batch" in line:
            assert "half=True" not in line
            return
    raise AssertionError("edge_embs embed call not found in _stage5_refine source")


def test_window_embed_wired_to_fp16_lever():
    """Pins the one call site the audit cleared: `window()`'s own embed call
    threads `ATR_R2_FP16_WIN` through `half=`. Window embeddings are only
    ever compared against other window embeddings or against fresh
    edge/mid-query embeddings inside stage 5 — never against the FAISS
    index — so this site is safe for half=True."""
    from app.services.scene_aligner import _WindowEmbedCache

    src = inspect.getsource(_WindowEmbedCache.window)
    assert 'half=r2_lever("ATR_R2_FP16_WIN")' in src


class _RecordingEmbedder:
    """No GPU in the test sandbox, so `half=True` degrades to a no-op
    (`torch.cuda.is_available()` is False) — this just pins that passing it
    doesn't break the plain `embed_batch` fallback path or its output."""

    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        return np.full((len(images), 2), 3.0, dtype=np.float32)


def test_embed_pil_batch_half_true_no_gpu_matches_half_false(monkeypatch) -> None:
    fake = _RecordingEmbedder()
    monkeypatch.setattr(AnimeMatcherService, "_embedder", fake)
    AnimeMatcherService.reset_runtime_stats()

    images = [Image.new("RGB", (8, 8), "black") for _ in range(3)]
    out_false = AnimeMatcherService._embed_pil_batch(images, half=False)
    out_true = AnimeMatcherService._embed_pil_batch(images, half=True)

    assert out_false.dtype == np.float32
    assert out_true.dtype == np.float32
    assert np.array_equal(out_false, out_true)
