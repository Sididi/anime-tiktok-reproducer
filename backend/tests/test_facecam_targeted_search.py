"""Bounded retrieval, aggregate spending, and real random-access source clocks."""
import base64
import io
import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.facecam_illustration_llm import IllustrationBudget, write_json
from app.services.facecam_visual_candidates import FacecamVisualCandidates as Candidates
from app.services.facecam_visual_index import FacecamVisualIndex as Index
from app.services.facecam_targeted_search import FacecamTargetedSearch as Search
from app.services.facecam_window_media import FacecamWindowMedia as Media
from app.services.llm_config_service import LLMConfigService
from test_facecam_visual_candidates import context, set_timing

REAL_BUDGET_CALL = IllustrationBudget.call


def test_prompt_model_and_capacity_changes_cannot_reset_budget(context, monkeypatch, tmp_path):
    original = IllustrationBudget.call
    paths = []

    def record(self, *args, **kw):
        assert self.conservative and self.limit <= .15
        paths.append(self.path)
        return original(self, *args, **kw)

    monkeypatch.setattr(IllustrationBudget, "call", record)
    Candidates.select("visual")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ("locate_illustrations.md", "verify_illustrations.md"):
        (prompts / name).write_text((Search.PROMPTS / name).read_text() + "\nPlus précis.\n")
    monkeypatch.setattr(Search, "PROMPTS", prompts)
    Candidates.select("visual")
    LLMConfigService.facecam_illustrations_entry().openrouter_id = "test/other-vision"
    monkeypatch.setattr(settings, "facecam_illustration_min_preview_seconds", .75)
    set_timing(context[0], [(0, 1.5, False)])
    Candidates.select("visual")
    assert len(paths) == 6 and len(set(paths)) == 1


def test_two_stages_share_budget_and_retry_does_not_dispatch_when_exhausted(context, monkeypatch):
    # Exercise the real accounting and transport against the isolated project.
    import httpx
    from openai import OpenAI
    from app.services.openrouter_service import OpenRouterService

    monkeypatch.setattr(IllustrationBudget, "call", REAL_BUDGET_CALL)
    monkeypatch.setattr(IllustrationBudget, "prices", staticmethod(lambda _: {"prompt": 1e-6, "completion": 4e-6}))
    path = context[4]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "budget", "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"scenes": [{"scene_index": 0, "window_ids": ["e0w0"]}]})}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 30, "total_tokens": 1030, "cost": .14}})

    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        monkeypatch.setattr(OpenRouterService, "_get_client", lambda: client)
        for _ in range(2):
            with pytest.raises(ValueError, match="Budget"):
                Candidates.select("visual")
    assert len(requests) == 1  # The localisation cache prevents paying again on retry.
    usage = list((path.parent / "project").rglob("usage.json"))
    assert len(usage) == 1
    assert len(json.loads(usage[0].read_text())["calls"]) == 1


def test_no_subtitles_still_checks_real_images_near_current_match(context, monkeypatch):
    monkeypatch.setattr(Index, "subtitles", lambda _: {"language": None, "cues": []})
    result = Candidates.select("visual")
    assert result and len(context[3]) == 1
    assert "shots" in json.loads(context[3][0].splitlines()[-1])


def test_invented_windows_fall_back_to_anchor_and_invented_shots_are_omitted(context, monkeypatch):
    original = IllustrationBudget.call

    def invalid(self, prompt, *args, **kw):
        data = json.loads(prompt.splitlines()[-1])
        if "windows" in data:
            return {"scenes": [{"scene_index": 0, "window_ids": ["invented"]}]}
        value = original(self, prompt, *args, **kw)
        value["scenes"][0]["alternatives"][0]["shot_id"] = "invented"
        return value

    monkeypatch.setattr(IllustrationBudget, "call", invalid)
    result = Candidates.select("visual")
    assert len(result) == 1 and result[0]["shot_id"] != "invented"
    report = next((context[4].parent / "project").rglob("report.json"))
    assert json.loads(report.read_text())["rejected"] == [{"scene_index": 0, "shot_id": "invented"}]


def test_corrupt_cached_source_coordinates_do_not_reach_export(context):
    Candidates.select("visual")
    path = next((context[4].parent / "project").rglob("candidates.json"))
    value = json.loads(path.read_text())
    value[0]["start"] += .5
    write_json(path, value)
    with pytest.raises(ValueError, match="bornes locales"):
        Candidates.select("visual")
    assert len(context[3]) == 2


def test_deadline_prevents_any_later_paid_request(context, monkeypatch):
    import app.services.facecam_targeted_search as module
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = IllustrationBudget.call

    def slow(self, *args, **kw):
        value = original(self, *args, **kw)
        clock[0] += settings.facecam_illustration_timeout_seconds + 1
        return value

    monkeypatch.setattr(IllustrationBudget, "call", slow)
    with pytest.raises(ValueError, match="Délai"):
        Candidates.select("visual")
    assert len(context[3]) == 1


def test_routing_rejection_is_not_charged_and_never_retries_automatically(tmp_path, monkeypatch):
    import httpx
    from openai import OpenAI, NotFoundError
    from app.models.llm_config import LLMPresetEntry
    from app.services.openrouter_service import OpenRouterService

    monkeypatch.setattr(IllustrationBudget, "prices", staticmethod(lambda _: {
        "prompt": 1e-6, "completion": 4e-6, "image": 1e-6, "request": 0}))
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(404, json={"error": {"code": 404,
            "message": "No endpoints found that satisfy the max price for this request"}})

    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(reject))) as client:
        monkeypatch.setattr(OpenRouterService, "_get_client", lambda: client)
        budget = IllustrationBudget(tmp_path, .15, LLMPresetEntry(openrouter_id="test/vision"), conservative=True)
        with pytest.raises(NotFoundError):
            budget.call("Locate", {})
    assert len(requests) == 1
    record = json.loads((tmp_path / "usage.json").read_text())["calls"][0]
    assert record["status"] == "rejected_before_generation" and record["charged_or_reserved_usd"] == 0


def test_real_seek_cut_detection_and_cached_frames_use_native_24fps(tmp_path, monkeypatch):
    import av
    import cv2
    import numpy as np
    from PIL import Image

    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    source = tmp_path / "colors.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 24, (160, 90))
    assert writer.isOpened()
    colors = ((0, 0, 255), (255, 255, 0), (0, 0, 0))
    for second in range(18):
        for _ in range(24):
            writer.write(np.full((90, 160, 3), colors[second % 3], dtype=np.uint8))
    writer.release()
    info = Media.probe(source)
    assert info["fps"] == 24
    with monkeypatch.context() as changed:
        changed.setattr(settings, "facecam_illustration_min_preview_seconds", .75)
        assert Media.probe(source)["fingerprint"] != info["fingerprint"]
    shots = Media.window(info, 12, 15, lambda: None)
    assert [(s["in_frame"], s["out_frame"]) for s in shots] == [(288, 312), (312, 336), (336, 360)]
    assert all(s["start"] == s["in_frame"] / 24 for s in shots)
    sheet = Media.contact_sheets(shots)[0]
    image = Image.open(io.BytesIO(base64.b64decode(sheet.split(",", 1)[1])))
    for row, expected in enumerate(((255, 0, 0), (0, 255, 255), (0, 0, 0))):
        pixel = image.getpixel((176, row * 222 + 60))
        assert max(abs(a - b) for a, b in zip(pixel, expected)) < 10
    monkeypatch.setattr(av, "open", lambda *a, **kw: pytest.fail("Cache should avoid decoding"))
    assert Media.window(info, 12, 15, lambda: None) == shots
