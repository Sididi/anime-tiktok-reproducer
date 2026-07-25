"""Task 9 (B3): variant-retrieval thinning behind ATR_R2_THIN.

``_weak_scene_sample_indices`` decides which scenes are "weak" enough to
warrant the expensive variant-retrieval + interior-split tail. Mainline skips
a scene when its best segment has ``inlier_count >= 4``; under the lever the
floor drops to 3, thinning out variant retrieval for scenes that mainline
would still treat as weak.

``fast_matching.r2_lever`` reads ``os.environ`` directly on every call (no
module-level caching), so a plain ``monkeypatch.setenv`` is enough to flip
behaviour between calls -- no ``importlib.reload`` needed (verified by
reading ``app/services/fast_matching.py``: ``r2_lever``/``fast_r2_enabled``/
``fast_enabled`` all call ``os.environ.get`` fresh, no cached globals).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scene_aligner import SceneAlignerService


def _scene_list(n=1):
    scenes = [
        SimpleNamespace(start_time=float(k), end_time=float(k + 1))
        for k in range(n)
    ]
    return SimpleNamespace(scenes=scenes)


def _samples():
    return [SimpleNamespace(t_tiktok=0.5)]


def test_inlier3_scene_is_weak_on_mainline(monkeypatch):
    monkeypatch.setenv("ATR_FAST_MATCHING", "0")  # R2 off => mainline rule
    segs = {0: [SimpleNamespace(inlier_count=3)]}
    weak = SceneAlignerService._weak_scene_sample_indices(
        _scene_list(), _samples(), segs
    )
    assert weak == {0}


def test_inlier3_scene_skipped_under_thin(monkeypatch):
    monkeypatch.setenv("ATR_FAST_MATCHING", "1")
    monkeypatch.setenv("ATR_FAST_R2", "1")
    monkeypatch.setenv("ATR_R2_THIN", "1")
    segs = {0: [SimpleNamespace(inlier_count=3)]}
    weak = SceneAlignerService._weak_scene_sample_indices(
        _scene_list(), _samples(), segs
    )
    assert weak == set()


def test_inlier3_scene_still_weak_when_thin_explicitly_off(monkeypatch):
    monkeypatch.setenv("ATR_FAST_MATCHING", "1")
    monkeypatch.setenv("ATR_FAST_R2", "1")
    monkeypatch.setenv("ATR_R2_THIN", "0")
    segs = {0: [SimpleNamespace(inlier_count=3)]}
    weak = SceneAlignerService._weak_scene_sample_indices(
        _scene_list(), _samples(), segs
    )
    assert weak == {0}
