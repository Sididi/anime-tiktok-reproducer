"""Regression tests for source gaps and bounded, atomic narration repair."""
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project, Transcription
from app.models.llm_config import LLMConfig
from app.services.llm_config_service import LLMConfigService
from app.services.openrouter_service import OpenRouterService
from app.services.script_payload_service import ScriptPayloadService as Payload
from app.services.script_repair_service import ScriptRepairService as Repair
from app.services.script_phase_prompt_service import ScriptPhasePromptService
from app.services.script_automation_service import ScriptAutomationService as Automation
from app.services.project_service import ProjectService
from app.services.voice_config_service import VoiceConfigService
from app.api.routes.processing import router


def source(texts, *, raw=(), indices=None):
    return Transcription(language="en", scenes=[
        {"scene_index": indices[i] if indices else i, "text": text,
         "is_raw": i in raw, "start_time": i * 2, "end_time": i * 2 + 2}
        for i, text in enumerate(texts)
    ])


def draft(transcription, texts=None):
    return {"language": "fr", "scenes": [
        {"scene_index": scene.scene_index, "text": texts[i] if texts else scene.text}
        for i, scene in enumerate(transcription.scenes)
    ]}


@pytest.fixture(autouse=True)
def isolate_model(monkeypatch):
    cfg = LLMConfig.model_validate({"default": "unrelated", "presets": {"unrelated": {
        "label": "Other", "big": {"openrouter_id": "other/big"}, "light": {"openrouter_id": "other/light"},
    }}})
    monkeypatch.setattr(LLMConfigService, "_cached", cfg)
    monkeypatch.setattr(OpenRouterService, "is_configured", lambda: True)
    monkeypatch.setattr(OpenRouterService, "generate_json_value_with_entry", lambda *a, **k: pytest.fail("Unexpected model call"))


def mock_responses(monkeypatch, responses):
    calls = []
    def call(prompt, **kwargs):
        calls.append({"input": json.loads(prompt), **kwargs})
        response = responses[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(OpenRouterService, "generate_json_value_with_entry", call)
    return calls


def patch(**texts):
    return {"changes": [{"scene_index": int(index), "text": text} for index, text in texts.items()]}


def test_diagnostics_collect_all_and_normalization_remains_strict():
    t = source(["one", "", "raw", "four"], raw=(2,))
    data = draft(t, ["...", " \u200b ", "bad raw text", "quatre"])
    issues = Payload.inspect(data, t)
    assert [(i.code, i.scene_index) for i in issues] == [("empty_narration", 0), ("empty_narration", 1), ("raw_text", 2)]
    with pytest.raises(RuntimeError, match="Scene 0.*Scene 1.*Scene 2"):
        Payload.normalize(payload=data, transcription=t)
    assert Payload.has_narration("À") and Payload.has_narration("3") and Payload.has_narration("你好")
    data["scenes"][0]["scene_index"] = True
    assert Payload.inspect(data, t)[0].code == "invalid_structure"


@pytest.mark.asyncio
async def test_no_calls_for_valid_or_invalid_structure():
    t = source(["Bonjour", ""], raw=(1,))
    data = draft(t)
    result, report = await Repair.repair(data, t, "fr")
    assert result == data and report.status == "not_needed" and report.attempts == 0
    for broken in [{"scenes": []}, {"scenes": [{"scene_index": 0, "text": None}]}, draft(t, ["", "raw violation"])]:
        result, report = await Repair.repair(broken, t, "fr")
        assert result == broken and report.status == "invalid" and report.attempts == 0


@pytest.mark.asyncio
async def test_local_repair_fixed_model_and_raw_immutability(monkeypatch):
    t = source(["He opens", "", "the door", "", "She leaves"], raw=(3,), indices=[10, 20, 30, 40, 50])
    data = draft(t, ["Il ouvre la porte.", "", "Alors", "", "Elle part."])
    calls = mock_responses(monkeypatch, [patch(**{"10": "Il ouvre", "20": "la porte", "30": "alors."})])
    result, report = await Repair.repair(data, t, "fr")
    assert report.status == "repaired" and report.review_required and report.attempts == 1
    assert report.source_gap_scene_indices == [20]
    assert result["scenes"][3:] == data["scenes"][3:]
    assert data["scenes"][1]["text"] == ""  # Caller-owned draft was not mutated.
    assert calls[0]["entry"].openrouter_id == "google/gemini-3.1-flash-lite"
    assert calls[0]["entry"].thinking.effort == "minimal"
    assert calls[0]["max_output_tokens"] == 4096
    assert calls[0]["response_schema"] == Repair.RESPONSE_SCHEMA
    assert [r["scene_index"] for r in calls[0]["input"]["neighborhoods"][0]["editable_scenes"]] == [10, 20, 30]


@pytest.mark.asyncio
async def test_wider_retry_only_after_unsuccessful_attempt(monkeypatch):
    t = source(["known"] * 11)
    data = draft(t, ["Texte"] * 5 + [""] + ["Texte"] * 5)
    calls = mock_responses(monkeypatch, [patch(), patch(**{"5": "Puis"})])
    _, report = await Repair.repair(data, t, "fr")
    assert report.status == "repaired" and report.attempts == 2
    rows = lambda call: call["input"]["neighborhoods"][0]["editable_scenes"]
    assert [r["scene_index"] for r in rows(calls[0])] == list(range(3, 8))
    assert [r["scene_index"] for r in rows(calls[1])] == list(range(1, 10))


@pytest.mark.asyncio
async def test_independent_windows_atomic_and_retry_preserves_success(monkeypatch):
    t = source(["first", "", "tail", "", "other", "", "tail"], raw=(3,))
    data = draft(t, ["Premier", "", "petit bout.", "", "Autre", "", "petit bout."])
    calls = mock_responses(monkeypatch, [patch(**{"1": "petit", "2": "fin", "4": "", "5": "Autre petit"}), patch()])
    result, report = await Repair.repair(data, t, "fr")
    assert result["scenes"][1]["text"] == "petit"
    assert result["scenes"][4:] == data["scenes"][4:]
    assert report.status == "partial" and report.unresolved_scene_indices == [5]
    assert len(calls[1]["input"]["neighborhoods"]) == 1
    assert all(r["scene_index"] > 3 for r in calls[1]["input"]["neighborhoods"][0]["editable_scenes"])


@pytest.mark.parametrize("response", [
    None, [], {"changes": "bad"}, {"changes": [None]},
    {"changes": [{"scene_index": 1, "text": None}]},
    {"changes": [{"scene_index": True, "text": "bad"}]},
    {"changes": [{"scene_index": 1, "text": "a"}, {"scene_index": 1, "text": "b"}]},
    patch(**{"99": "unauthorized"}), patch(**{"1": "..."}), patch(**{"1": "OK", "0": ""}),
    RuntimeError("Unavailable"),
])
@pytest.mark.asyncio
async def test_bad_responses_exhaust_two_attempts_without_losing_draft(monkeypatch, response):
    t = source(["known", "", "tail"])
    data = draft(t)
    calls = mock_responses(monkeypatch, [response, response])
    result, report = await Repair.repair(data, t, "fr")
    assert result == data and report.status == "failed"
    assert report.attempts == len(calls) == 2 and report.unresolved_scene_indices == [1]


@pytest.mark.asyncio
async def test_no_context_and_no_api_preserve_draft(monkeypatch):
    t = source(["", "", ""], raw=(1,))
    result, report = await Repair.repair(draft(t), t, "fr")
    assert report.attempts == 0 and report.unresolved_scene_indices == [0, 2]
    assert "insufficient" in report.warning
    monkeypatch.setattr(OpenRouterService, "is_configured", lambda: False)
    _, report = await Repair.repair(draft(t), t, "fr")
    assert report.attempts == 0 and "not configured" in report.warning


def test_neighborhood_edges_clusters_and_raw_boundaries():
    t = source(["a", "", "", "b", "", "c", ""], raw=(4,))
    assert Repair.neighborhoods(t, {0, 1, 2, 6}, 2) == [(0, 3), (5, 6)]


@pytest.mark.asyncio
async def test_source_empty_filler_is_rejected_before_redistribution_retry(monkeypatch):
    t = source(["He blocks the entrance.", "", "She waits for him to turn away."])
    data = draft(t, ["Il bloque l’entrée.", "", "Elle attend qu’il se retourne."])
    calls = mock_responses(monkeypatch, [patch(**{"1": "Le temps presse."}),
        patch(**{"1": "Elle", "2": "attend qu’il se retourne."})])
    result, report = await Repair.repair(data, t, "fr")
    assert report.status == "repaired" and report.attempts == 2
    assert result["scenes"][0] == data["scenes"][0]
    assert result["scenes"][1]["text"] == "Elle"
    assert calls[1]["input"]["neighborhoods"][0]["editable_scenes"][1]["draft_text"] == ""


@pytest.mark.asyncio
async def test_nonempty_source_can_restore_omitted_meaning_without_donor(monkeypatch):
    t = source(["He waits.", "She opens the door.", "They leave."])
    data = draft(t, ["Il attend.", "", "Ils partent."])
    mock_responses(monkeypatch, [patch(**{"1": "Elle ouvre la porte."})])
    result, report = await Repair.repair(data, t, "fr")
    assert report.status == "repaired" and not report.source_gap_scene_indices
    assert result["scenes"][0] == data["scenes"][0]
    assert result["scenes"][2] == data["scenes"][2]


@pytest.mark.parametrize("language,anchors", [
    ("fr", {0: "bloque", 2: "attend", 3: "attrape", 5: "déverrouille"}),
    ("en", {0: "blocks", 2: "waits", 3: "grabs", 5: "unlocks"}),
    ("es", {0: "bloquea", 2: "espera", 3: "quita", 5: "abre"}),
])
def test_retained_live_language_samples_keep_action_anchors_and_raw_boundary(language, anchors):
    fixture = json.loads((Path(__file__).parent / "fixtures/script_repair_languages.json").read_text())
    t = Transcription.model_validate(fixture["source"])
    case = next(c for c in fixture["cases"] if c["language"] == language)
    response = {"changes": [{"scene_index": c["scene_index"], "text": c["after"]} for c in case["report"]["changes"]]}
    result = Repair.apply_response(response, case["input"], t, Repair.neighborhoods(t, {1, 4}, 2))
    assert result == case["output"] and not Payload.inspect(result, t)
    assert result["scenes"][6:] == case["input"]["scenes"][6:]
    for index, anchor in anchors.items():
        assert anchor in result["scenes"][index]["text"]


@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "fixtures/script_source_gaps.json").read_text())["cases"], ids=lambda c: c["project_id"])
@pytest.mark.asyncio
async def test_reported_project_source_gaps_in_long_reconstructed_drafts(monkeypatch, case):
    # These are reconstructed drafts, not recovered LLM output.
    t = source(["Placeholder narration"] * case["scene_count"])
    t.language = case["source_language"]
    exact = {s["scene_index"]: s for s in case["source_scenes"]}
    t = Transcription(language=t.language, scenes=[exact.get(s.scene_index, s.model_dump()) for s in t.scenes])
    data = draft(t, ["" if s.scene_index in case["missing_indices"] or s.is_raw else "Narration reconstruite." for s in t.scenes])
    changes = {str(i): "reconstruite" for i in case["missing_indices"]}
    for left, right in Repair.neighborhoods(t, set(case["missing_indices"]), 2):
        donor = next(p for p in range(left, right + 1) if data["scenes"][p]["text"])
        changes[str(t.scenes[donor].scene_index)] = "Narration."
    calls = mock_responses(monkeypatch, [patch(**changes)])
    result, report = await Repair.repair(data, t, "fr")
    assert report.source_gap_scene_indices == case["missing_indices"]
    assert report.status == "repaired" and report.attempts == 1
    assert not Payload.inspect(result, t)
    editable = [r for w in calls[0]["input"]["neighborhoods"] for r in w["editable_scenes"]]
    assert len(editable) < len(t.scenes)


@pytest.mark.parametrize("library_type", ["anime", "pure"])
@pytest.mark.parametrize("source_language,target_language", [("en", "fr"), ("en", "es"), ("fr", "fr")])
def test_all_prompt_variants_distinguish_raw_and_empty_source(library_type, source_language, target_language):
    t = source(["", ""], raw=(1,))
    t.language = source_language
    prompt = ScriptPhasePromptService.build_script_prompt(project=Project(id="prompt", library_type=library_type), transcription=t, target_language=target_language)
    data = json.loads(prompt.split("DONNÉES D'ENTRÉE :")[-1])
    assert [s["is_raw"] for s in data["scenes"]] == [False, True]
    assert "Un texte source vide ne signifie JAMAIS" in prompt
    assert "seule contrainte rigide" not in prompt


@pytest.fixture
def stored_project(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", tmp_path)
    project = Project(id="repairtest")
    p = tmp_path / project.id
    p.mkdir()
    (p / "project.json").write_text(project.model_dump_json())
    t = source(["He opens", "", "the door"])
    (p / "transcription.json").write_text(t.model_dump_json())
    monkeypatch.setattr(VoiceConfigService, "get_voice", lambda key: SimpleNamespace(model_id="eleven_v3"))
    return p, t


@pytest.mark.asyncio
async def test_automation_forces_review_and_persists_original(monkeypatch, stored_project):
    p, t = stored_project
    calls = mock_responses(monkeypatch, [patch(**{"1": "the", "2": "door"})])
    events = [event async for event in Automation.stream_automation(
        project_id=p.name, target_language="fr", voice_key="test", existing_script_json=draft(t),
        skip_tts=True, skip_metadata=True, skip_overlay=True, pause_after_script=False,
    )]
    assert events[-1]["event"] == "script_ready"
    assert events[-1]["repair_report"]["review_required"]
    assert not any(e["event"].startswith("tts_") for e in events)
    latest = Automation.get_latest_run(p.name)
    assert latest["draft_status"] == "review_required" and latest["parts"] == []
    run = p / Automation.RUNS_DIR_NAME / latest["run_id"]
    assert json.loads((run / "draft.original.json").read_text()) == draft(t)
    assert (run / "script.json").exists()
    assert len(calls) == 1


def test_repair_endpoint_reload_review_and_stale_source(monkeypatch, stored_project):
    p, t = stored_project
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    calls = mock_responses(monkeypatch, [patch(**{"1": "the", "2": "door"})])
    response = client.post(f"/projects/{p.name}/script/repair", json={"script_json": draft(t), "target_language": "fr"})
    assert response.status_code == 200
    body = response.json()
    latest = client.get(f"/projects/{p.name}/script/latest-generation").json()
    assert latest["draft_status"] == "review_required" and latest["source_matches"]
    assert len(calls) == 1  # Read/reload never retries.
    reviewed = client.post(f"/projects/{p.name}/script/repair/review", json={
        "run_id": body["run_id"], "script_json": body["script_json"], "target_language": "fr",
    })
    assert reviewed.status_code == 200
    assert Automation.get_latest_run(p.name)["draft_status"] == "validated"
    t.scenes[0].text = "Changed source"
    (p / "transcription.json").write_text(t.model_dump_json())
    assert not Automation.get_latest_run(p.name)["source_matches"]
    assert client.post(f"/projects/{p.name}/script/repair/review", json={
        "run_id": body["run_id"], "script_json": body["script_json"], "target_language": "fr",
    }).status_code == 409


@pytest.mark.asyncio
async def test_unresolved_persistence_and_empty_run_does_not_hide_draft(monkeypatch, stored_project):
    p, t = stored_project
    mock_responses(monkeypatch, [patch(), patch()])
    events = [e async for e in Automation.stream_automation(project_id=p.name, target_language="fr", voice_key="test",
        existing_script_json=draft(t), skip_tts=True)]
    assert events[-1]["event"] == "error" and events[-1]["script_json"] == draft(t)
    latest = Automation.get_latest_run(p.name)
    assert latest["draft_status"] == "unresolved"
    assert not (p / Automation.RUNS_DIR_NAME / latest["run_id"] / "script.json").exists()
    Automation._prepare_run_dirs(p.name, "e" * 32)
    assert Automation.get_latest_run(p.name)["run_id"] == latest["run_id"]


@pytest.mark.asyncio
async def test_source_change_during_repair_cannot_be_applied(monkeypatch, stored_project):
    p, t = stored_project
    def response(*args, **kwargs):
        changed = deepcopy(t)
        changed.scenes[0].text = "Source edited while waiting"
        (p / "transcription.json").write_text(changed.model_dump_json())
        return patch(**{"1": "the", "2": "door"})
    monkeypatch.setattr(OpenRouterService, "generate_json_value_with_entry", response)
    events = [e async for e in Automation.stream_automation(project_id=p.name, target_language="fr", voice_key="test",
        existing_script_json=draft(t), skip_tts=True)]
    assert events[-1]["event"] == "error" and "Source transcription changed" in events[-1]["message"]
    latest = Automation.get_latest_run(p.name)
    assert latest["script_json"] == draft(t) and not latest["source_matches"]


@pytest.mark.asyncio
async def test_cancellation_does_not_commit_late_model_result(monkeypatch, stored_project):
    import threading
    p, t = stored_project
    started, release = threading.Event(), threading.Event()
    def response(*args, **kwargs):
        started.set()
        release.wait(5)
        return patch(**{"1": "the", "2": "door"})
    monkeypatch.setattr(OpenRouterService, "generate_json_value_with_entry", response)
    async def consume():
        return [e async for e in Automation.stream_automation(project_id=p.name, target_language="fr", voice_key="test",
            existing_script_json=draft(t), skip_tts=True)]
    task = asyncio.create_task(consume())
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    latest = Automation.get_latest_run(p.name)
    assert latest["script_json"] == draft(t) and latest["draft_status"] == "unresolved"


def test_reviewed_edit_invalidates_legacy_audio_across_reload(stored_project):
    p, t = stored_project
    run_id = "c" * 32
    run_dir, parts_dir = Automation._prepare_run_dirs(p.name, run_id)
    previous = draft(t, ["Il ouvre", "la", "porte."])
    (run_dir / "script.json").write_text(json.dumps(previous))
    (parts_dir / "part_1.mp3").write_bytes(b"old audio")
    assert len(Automation.get_latest_run(p.name)["parts"]) == 1
    app = FastAPI()
    app.include_router(router)
    changed = deepcopy(previous)
    changed["scenes"][0]["text"] = "Elle ouvre"
    response = TestClient(app).post(f"/projects/{p.name}/script/repair/review", json={
        "run_id": run_id, "script_json": changed, "target_language": "fr",
    })
    assert response.status_code == 200
    latest = Automation.get_latest_run(p.name)
    assert latest["script_json"] == changed and latest["parts"] == []
    assert json.loads((run_dir / "draft_state.json").read_text())["audio_invalidated"]
    assert (parts_dir / "part_1.mp3").exists()  # History retained, never reused for edited narration.
