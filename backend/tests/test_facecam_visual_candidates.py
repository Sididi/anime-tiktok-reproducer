"""Grounded local coordinates, bounded paid work, cache reuse, and V4-only placement."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import MatchList, Project, SceneMatch, SceneTranscription, Transcription
from app.models.facecam import FacecamState, ScriptBlock
from app.models.llm_config import LLMPresetEntry
from app.services.facecam_editor_track import FacecamEditorTrackService as Editor
from app.services.facecam_illustration_llm import IllustrationBudget
from app.services.facecam_visual_index import FacecamVisualIndex as Index, file_identity
from app.services.facecam_visual_candidates import FacecamVisualCandidates as Candidates
from app.services.facecam_targeted_search import FacecamTargetedSearch as Search
from app.services.facecam_window_media import FacecamWindowMedia as Media
from app.services.facecam_service import FacecamService
from app.services.llm_config_service import LLMConfigService
from app.services.openrouter_service import OpenRouterService
from app.services.project_service import ProjectService
from app.services.anime_library import AnimeLibraryService
from app.services.processing import ResolvedSceneSource
from app.services.processing import ProcessingService
from app.services.otio_timing import FrameRateInfo
from test_facecam_editor_track import scene


def shot(i=0, **kw):
    return {"shot_id": f"s{i:05d}", "in_frame": 240 + 48 * i, "out_frame": 288 + 48 * i,
            "start": 10 + 2 * i, "end": 12 + 2 * i, "samples": [250 + 48 * i, 270 + 48 * i],
            "visual": "Un dragon marche dans la forêt.", "dialogue": "Je dois survivre.", "usable": True, **kw}


def alternative(i, target=0, **kw):
    return {**shot(i), "shot_id": f"e0:s{i:05d}", "output_scene_index": target, "match_index": -i - 1,
            "episode": "local.mp4", "fps": 24, "episode_fingerprint": "episode-v1", "reason": "Illustre la forêt.", **kw}


def resolved(c):
    return ResolvedSceneSource(c["match_index"], Path(c["episode"]), "local", c["in_frame"], c["out_frame"],
                               c["start"], c["end"], c["end"] - c["start"])


def test_three_choices_partition_scene_preserving_native_bounds_and_v3():
    scenes = [scene(0, 0, 6, 30, 40), scene(1, 6, 8, 50, 51)]
    before = json.dumps(scenes)
    candidates = [alternative(0), alternative(1)]
    plan = Editor.place_alternatives(scenes, candidates, {c["match_index"]: resolved(c) for c in candidates},
                                     8, FrameRateInfo(24, False))
    assert [(c["start_frame"], c["end_frame"]) for c in plan["clips"]] == [(0, 120), (120, 240), (240, 360), (360, 420)]
    assert all(c["effective_speed"] == 1 for c in plan["clips"])
    assert plan["clips"][0]["source_out"] == 40  # Extendable handles remain available.
    assert json.dumps(scenes) == before


def test_short_scenes_reduce_choices_and_raw_dialogue_and_cover_are_protected():
    scenes = [scene(0, 0, 1, 0, 2), scene(1, 1, 3, 3, 5, is_raw=True), scene(2, 3, 7, 5, 10)]
    candidates = [alternative(0), alternative(1), alternative(2, target=1)]
    plan = Editor.place_alternatives(scenes, candidates, {c["match_index"]: resolved(c) for c in candidates},
                                     5, FrameRateInfo(24, False))
    assert [(c["start_frame"], c["end_frame"]) for c in plan["clips"]] == [(0, 30), (30, 60), (60, 180), (180, 300)]
    assert len(plan["skipped"]) == 2


def test_illustrations_stop_at_reveal_even_when_later_scenes_have_no_slot():
    scenes = [scene(0, 0, 6, 0, 10), scene(1, 6, 8, 10, 12), scene(2, 8, 10, 12, 14)]
    plan = Editor.place_alternatives(scenes, [], {}, 5, FrameRateInfo(24, False))
    assert [(c['start_frame'], c['end_frame']) for c in plan['clips']] == [(0, 300)]


def test_candidates_use_their_own_native_clock_in_ntsc_project():
    c = alternative(0)
    plan = Editor.place_alternatives([scene(0, 0, 8, 0, 10)], [c], {-1: resolved(c)}, 8, FrameRateInfo(24, True))
    extra = plan["clips"][1]
    assert extra["end_frame"] - extra["start_frame"] == 120
    assert extra["source_fps"] == 24
    assert Index.native_rate(24.000011) == FrameRateInfo(24, False)
    assert Index.native_rate(24000 / 1001) == FrameRateInfo(24, True)


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    path = tmp_path / "episode.mp4"
    path.write_bytes(b"local video")
    project = Project(id="visual", project_mode="facecam")
    state = FacecamState(revision=1, approved_revision=1,
        blocks=[ScriptBlock(id="body", kind="body", text="Le dragon fuit.")],
        montage_map=[{"output_scene_index": 0, "source_scene_index": 4, "kind": "body"}],
        assignment=[{"scene_index": 0, "text": "Le dragon fuit."}])
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    state.prepared = {"directory": str(prepared), "cover_start": 6}
    set_timing(state, [(0, 6, False)])
    state.recording = {"fingerprint": "take", "reviewed_revision": state.revision}
    matches = MatchList(matches=[SceneMatch(scene_index=0, episode=str(path), start_time=1, end_time=2,
                                           confidence=1, speed_ratio=1)])
    monkeypatch.setattr(FacecamService, "project", lambda _: project)
    monkeypatch.setattr(FacecamService, "load", lambda _: state)
    monkeypatch.setattr(FacecamService, "source_matches", lambda _: matches)
    monkeypatch.setattr(FacecamService, "directory", lambda _: tmp_path / "project")
    monkeypatch.setattr(ProjectService, "load_matches", lambda _: matches)
    monkeypatch.setattr(AnimeLibraryService, "resolve_episode_path", lambda episode, **kw: Path(episode))
    monkeypatch.setattr(AnimeLibraryService, "list_episode_paths", lambda **kw: pytest.fail("Must not scan other episodes"))
    entry = LLMPresetEntry(openrouter_id="test/vision", max_output_tokens=4096)
    monkeypatch.setattr(LLMConfigService, "facecam_illustrations_entry", lambda: entry)
    manifest = {"source": str(path), "identity": file_identity(path), "fingerprint": "v1",
                "fps": 24, "frame_count": 1000, "shots": [shot(i) for i in range(3)]}
    manifest["duration"] = 36
    monkeypatch.setattr(Index, "build", lambda *a, **kw: pytest.fail("No full episode index"))
    monkeypatch.setattr(Media, "probe", lambda _: manifest)
    monkeypatch.setattr(Index, "subtitles", lambda _: {"language": "fr", "cues": [
        {"start": 3, "end": 10, "text": "Le dragon traverse la forêt."}]})
    def decode(info, start, end, check):
        check()
        return [{**shot(i), "shot_id": f"{info['fingerprint']}f{int(start * 24) + 96 + i * 48}",
            "episode": info["source"], "episode_fingerprint": info["fingerprint"], "fps": 24,
            "start": start + 4 + i * 2, "end": start + 6 + i * 2,
            "in_frame": round((start + 4 + i * 2) * 24), "out_frame": round((start + 6 + i * 2) * 24)}
            for i in range(3)]
    monkeypatch.setattr(Media, "window", decode)
    monkeypatch.setattr(Media, "contact_sheets", lambda shots: ["data:image/jpeg;base64,AA=="])
    calls = []
    def generate(self, prompt, schema, **kw):
        calls.append(prompt)
        data = json.loads(prompt.splitlines()[-1])
        if "windows" in data:
            return {"scenes": [{"scene_index": s["scene_index"], "window_ids": ["e0w0"]} for s in data["scenes"]]}
        return {"scenes": [{"scene_index": s["scene_index"], "alternatives": [
            {"shot_id": identity, "reason": "Le dragon visible illustre sa fuite."} for identity in s["shot_ids"][:s["slots"]]]}
            for s in data["scenes"]]}
    monkeypatch.setattr(IllustrationBudget, "call", generate)
    return state, matches, manifest, calls, path


def set_timing(state, rows):
    timing = Transcription(language="fr", scenes=[SceneTranscription(scene_index=i, text="Récit",
        start_time=start, end_time=end, is_raw=raw) for i, (start, end, raw) in enumerate(rows)])
    (Path(state.prepared["directory"]) / "transcription.json").write_text(timing.model_dump_json())
    state.prepared["cover_start"] = max(end for _, end, _ in rows)


def test_selection_is_cached_and_reuses_media_across_speed_changes(context):
    state, matches, manifest, calls, _ = context
    result = Candidates.select("visual")
    assert len(result) == 2 and len(calls) == 2
    assert result[0]["start"] == 4 and result[0]["match_index"] == -1
    assert Candidates.select("visual") == result and len(calls) == 2
    state.auto_editor.speed = 1.2
    Candidates.select("visual")
    assert len(calls) == 2
    state.assignment[0]["text"] = "Le dragon s'enfuit dans les bois."
    Candidates.select("visual")
    assert len(calls) == 4
    manifest["fingerprint"] = "new-episode-index"
    Candidates.select("visual")
    assert len(calls) == 6


def test_legacy_state_needs_no_vision_call_and_no_library_scan(context, monkeypatch):
    state, _, _, calls, _ = context
    state.assignment = []
    monkeypatch.setattr(Index, "build", lambda _: pytest.fail("No narrative targets"))
    assert Candidates.select("visual") == []
    assert not calls


def test_many_target_scenes_use_at_most_three_calls(context):
    state, _, _, calls, _ = context
    state.assignment = [{"scene_index": i, "text": "Il traverse la forêt."} for i in range(40)]
    state.montage_map = [{"output_scene_index": i, "source_scene_index": i, "kind": "body"} for i in range(40)]
    set_timing(state, [(i * 3, i * 3 + 3, False) for i in range(40)])
    result = Candidates.select("visual")
    assert len(result) == 64
    assert len(calls) == 3
    assert [len(json.loads(p.splitlines()[-1])["scenes"]) for p in calls] == [32, 16, 16]
    assert Candidates.select("visual") == result
    assert len(calls) == 3


def test_no_room_raw_and_outro_are_excluded_before_paid_work(context, monkeypatch):
    state, _, _, calls, _ = context
    state.assignment = [{"scene_index": i, "text": "Il traverse la forêt."} for i in range(5)]
    state.montage_map = [{"output_scene_index": i, "source_scene_index": i, "kind": "outro" if i == 2 else "body"}
                         for i in range(5)]
    set_timing(state, [(0, .99, False), (.99, 3, True), (3, 6, False), (6, 7, False), (7, 9, False)])
    result = Candidates.select("visual")
    assert [c["output_scene_index"] for c in result] == [3, 4, 4]
    assert [s["scene_index"] for s in json.loads(calls[0].splitlines()[-1])["scenes"]] == [3, 4]
    set_timing(state, [(0, .5, False), (.5, 1, False), (1, 1.5, False), (1.5, 2, False), (2, 2.5, False)])
    monkeypatch.setattr(Media, "probe", lambda _: pytest.fail("No eligible scenes"))
    assert Candidates.select("visual") == []
    assert len(calls) == 2


@pytest.mark.parametrize("ids", [["missing"], ["x", "x"], ["x", "y", "z"]])
def test_unknown_duplicate_and_excess_candidates_are_rejected(ids):
    with pytest.raises(ValueError):
        Candidates.validate({"scenes": [{"scene_index": 0, "alternatives": [
            {"shot_id": i, "reason": "utile"} for i in ids]}]}, [{"scene_index": 0}],
            {"x": {"episode": "e", "start": 1, "end": 2}}, {})


def test_overlapping_original_is_not_an_alternative():
    with pytest.raises(ValueError, match="recouvre"):
        Candidates.validate({"scenes": [{"scene_index": 0, "alternatives": [{"shot_id": "x", "reason": "utile"}]}]},
            [{"scene_index": 0}], {"x": {"episode": "e", "start": 1, "end": 3}},
            {0: {"episode": "e", "start": 2, "end": 4}})


def test_overlapping_original_is_removed_before_vision(context):
    _, matches, _, calls, _ = context
    matches.matches[0].start_time = 4
    matches.matches[0].end_time = 5
    result = Candidates.select("visual")
    assert result and all(c["start"] >= 5 for c in result)
    data = json.loads(calls[-1].splitlines()[-1])
    assert all(shot["shot_id"] != "v1f96" for shot in data["shots"])
    assert len(calls) == 2


def test_script_changed_during_selection_cannot_install_result(context, monkeypatch, tmp_path):
    state = context[0]
    def stale(self, prompt, *args, **kw):
        if "windows" in json.loads(prompt.splitlines()[-1]):
            return {"scenes": []}
        state.assignment[0]["text"] = "Nouveau récit"
        return {"scenes": [{"scene_index": 0, "alternatives": []}]}
    monkeypatch.setattr(IllustrationBudget, "call", stale)
    with pytest.raises(ValueError, match="changé"):
        Candidates.select("visual")
    assert not list((tmp_path / "project").rglob("candidates.json"))


def test_episode_cache_reuses_paid_batches_and_invalidates_subtitles(context, monkeypatch, tmp_path):
    _, _, manifest, calls, path = context
    # Restore actual build; the fixture only replaced it to isolate selection.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(Index, "manifest", lambda _: (tmp_path / "index", manifest))
    subtitles = {"language": "fr", "cues": [{"start": 9, "end": 14, "text": "Un dragon."}]}
    monkeypatch.setattr(Index, "subtitles", lambda _: subtitles)
    monkeypatch.setattr(Index, "prepare_frames", lambda *a, **kw: None)
    monkeypatch.setattr(Index, "contact_sheets", lambda *a, **kw: ["data:image/jpeg;base64,AA=="])
    monkeypatch.setattr(LLMConfigService, "facecam_illustrations_entry", lambda: LLMPresetEntry(openrouter_id="test/vision"))
    def generate(self, prompt, schema, **kw):
        calls.append(prompt)
        return {"shots": [{"shot_id": s["shot_id"], "visual": "Un dragon.", "usable": True}
                          for s in json.loads(prompt.splitlines()[-1])["shots"]]}
    monkeypatch.setattr(IllustrationBudget, "call", generate)
    assert len(Index.build(path)["shots"]) == 3
    Index.build(path)
    assert len(calls) == 1
    subtitles["cues"][0]["text"] = "Le héros est un dragon."
    Index.build(path)
    assert len(calls) == 2


def test_budget_uses_real_usage_and_reserves_before_request(tmp_path, monkeypatch):
    monkeypatch.setattr(IllustrationBudget, "prices", staticmethod(lambda _: {"prompt": 1e-6, "completion": 4e-6}))
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        assert json.loads((tmp_path / "usage.json").read_text())["calls"][-1]["status"] == "reserved"
        return httpx.Response(200, json={"id": "cost-test", "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700, "cost": 0.009}})
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        monkeypatch.setattr(OpenRouterService, "_get_client", lambda: client)
        budget = IllustrationBudget(tmp_path, 0.01, LLMPresetEntry(openrouter_id="test/vision", max_output_tokens=1000))
        assert budget.call("Describe", {"type": "object"}, images=["data:image/jpeg;base64,AA=="]) == {}
        with pytest.raises(ValueError, match="Budget"):
            budget.call("Describe", {"type": "object"})
    assert len(requests) == 1
    assert requests[0]["provider"]["allow_fallbacks"] is False
    assert requests[0]["provider"]["max_price"] == {"prompt": 1, "completion": 4, "request": 0, "image": 1e-6}
    assert requests[0]["messages"][0]["content"][1]["type"] == "image_url"
    assert json.loads((tmp_path / "usage.json").read_text())["calls"][0]["charged_or_reserved_usd"] == 0.009


def test_timeout_reservation_survives_retry_without_automatic_paid_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(IllustrationBudget, "prices", staticmethod(lambda _: {"prompt": 1e-6, "completion": 4e-6}))
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(fail))) as client:
        monkeypatch.setattr(OpenRouterService, "_get_client", lambda: client)
        entry = LLMPresetEntry(openrouter_id="test/vision", max_output_tokens=1000)
        budget = IllustrationBudget(tmp_path, 0.006, entry)
        from openai import APITimeoutError
        with pytest.raises(APITimeoutError):
            budget.call("Describe", {})
        with pytest.raises(ValueError, match="Budget"):
            IllustrationBudget(tmp_path, 0.006, entry).call("Describe", {})
    assert len(calls) == 1


def test_visual_description_cannot_invent_or_omit_shot_ids():
    with pytest.raises(ValueError):
        Index.validate_descriptions({"shots": [{"shot_id": "not-a-local-shot", "visual": "Un dragon", "usable": True}]}, [shot()])
    with pytest.raises(ValueError):
        Index.validate_manifest({"fps": 24, "frame_count": 250, "shots": [shot()]})


def test_build_and_export_use_discovered_media_without_reselecting(context, monkeypatch):
    state, _, _, calls, path = context
    monkeypatch.setattr(ProcessingService, "_resolve_source_reference", lambda episode, **kw: (Path(episode), Path(episode).stem))
    from app.services.facecam_placement_service import FacecamPlacementService
    selected = Candidates.select("visual")
    monkeypatch.setattr(FacecamPlacementService, "candidates", lambda _: selected)
    project = FacecamService.project("visual")
    scenes = [scene(0, 0, 6, 1, 2)]
    plan = Editor.build(project, scenes, FrameRateInfo(24, True))
    assert len(calls) == 2 and len(plan["clips"]) == 3
    assert plan["clips"][1]["source_in_frame"] == 96  # Actual 24 fps, not the V3 NTSC default.
    monkeypatch.setattr(Candidates, "select", lambda _: pytest.fail("Export must not regenerate candidates"))
    jsx = "var FACECAM = " + json.dumps({"editor_track": plan}) + ";"
    matches = Editor.export_matches("visual", jsx)
    assert len(matches) == 2 and all(m.episode == str(path) for m in matches)
    plan["clips"][1]["episode"] = str(path.parent / "other-episode.mp4")
    with pytest.raises(ValueError, match="extérieur"):
        Editor.export_matches("visual", "var FACECAM = " + json.dumps({"editor_track": plan}))


def test_native_subclip_uses_actual_episode_seconds_and_remains_extendable():
    import subprocess
    path = ProcessingService.PREMIERE_JSX_TEMPLATE_PATH.parent / "premiere_facecam_editor_track.jsx"
    code = path.read_text()
    start = code.index("  function getFacecamEditorSubclip(")
    end = code.index("\n  function ", start + 1)
    javascript = """
const assert = require('node:assert/strict');
function facecamEditorSubclipName(){return 'choice'}
function getCachedProjectItem(){return null} function findProjectItem(){return null}
function buildRawTimeFromSourceFrame(){throw new Error('Wrong episode clock')}
function buildRawTimeFromSeconds(s){return {ticks:'seconds:'+s}}
function moveItemToProjectBin(){} function cacheProjectItem(){} function cacheProjectItemByName(){}
var args;
function getOrImportClip(){return {createSubClip(...a){args=a;return {}}}}
""" + code[start:end] + """
getFacecamEditorSubclip({source_fps:24,source_in:10,source_out:12,source_in_frame:240,source_out_frame:288});
assert.deepEqual(args,['choice','seconds:10','seconds:12',0,1,0]);
"""
    result = subprocess.run(["node", "-e", javascript], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_real_local_cut_detection_and_cached_images_keep_frame_identity(tmp_path, monkeypatch):
    import base64
    import cv2
    import io
    import numpy as np
    from PIL import Image

    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    path = tmp_path / "colors.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24, (160, 90))
    assert writer.isOpened()
    for color in ((0, 0, 255), (255, 255, 0), (0, 0, 0)):
        for _ in range(24):
            writer.write(np.full((90, 160, 3), color, dtype=np.uint8))
    writer.release()
    directory, manifest = Index.manifest(path)
    assert [(s["in_frame"], s["out_frame"]) for s in manifest["shots"]] == [(0, 24), (24, 48), (48, 72)]
    Index.prepare_frames(path, manifest, directory / "frames")
    sheet = Index.contact_sheets(path, manifest["shots"], frames_dir=directory / "frames")[0]
    image = Image.open(io.BytesIO(base64.b64decode(sheet.split(",", 1)[1])))
    for row, expected in enumerate(((255, 0, 0), (0, 255, 255), (0, 0, 0))):
        pixel = image.getpixel((176, row * 222 + 60))
        assert max(abs(a - b) for a, b in zip(pixel, expected)) < 10
    # A cached index and its frames remain usable without reopening the decoder.
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **kw: pytest.fail("Unexpected decode"))
    assert Index.manifest(path)[1] == manifest
    Index.prepare_frames(path, manifest, directory / "frames")


def test_cancelled_selection_makes_no_index_or_model_calls(context, monkeypatch):
    import threading
    event = threading.Event()
    event.set()
    monkeypatch.setattr(Index, "build", lambda *a, **kw: pytest.fail("Cancelled request"))
    with pytest.raises(RuntimeError, match="annulée"):
        Candidates.select("visual", cancel_event=event)
