import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import fast_matching


def _reload_with_env(monkeypatch, **env):
    for key in ("ATR_FAST_R2", "ATR_R2_COARSE", "ATR_R2_FP16_WIN", "ATR_R2_THIN"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    importlib.reload(fast_matching)
    return fast_matching


def test_r2_default_on_when_fast_mode_on(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="1")
    assert fm.fast_r2_enabled() is True


def test_r2_master_kill_switch(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="1", ATR_FAST_R2="0")
    assert fm.fast_r2_enabled() is False
    # levers are dead when the master is off, whatever their own flag says
    assert fm.r2_lever("ATR_R2_COARSE") is False


def test_r2_follows_fast_master(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="0")
    assert fm.fast_r2_enabled() is False


def test_r2_lever_toggles(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="1", ATR_R2_FP16_WIN="0")
    assert fm.r2_lever("ATR_R2_FP16_WIN") is False
    assert fm.r2_lever("ATR_R2_COARSE") is True  # default ON on the branch
