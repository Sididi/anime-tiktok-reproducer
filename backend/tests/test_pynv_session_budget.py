import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import pynv_decode


def test_set_session_budget_clamps():
    pynv_decode.set_session_budget(5)
    assert pynv_decode.get_session_budget() == 3
    pynv_decode.set_session_budget(0)
    assert pynv_decode.get_session_budget() == 1
    pynv_decode.set_session_budget(2)
    assert pynv_decode.get_session_budget() == 2


def test_budget_applies_to_pool_max():
    pynv_decode.set_session_budget(3)
    assert pynv_decode._pool_max_sessions() == 3
    pynv_decode.set_session_budget(2)
    assert pynv_decode._pool_max_sessions() == 2
