"""Three bounded model calls at most: subtitles first, then only useful local images."""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..config import settings
from ..models import Transcription
from .facecam_illustration_llm import IllustrationBudget, write_json
from .facecam_visual_index import FacecamVisualIndex, check_cancelled, file_identity, fingerprint
from .facecam_window_media import FacecamWindowMedia
from .llm_config_service import LLMConfigService


class LocatedScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_index: StrictInt
    window_ids: list[str] = Field(max_length=1)


class LocatedScenes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[LocatedScene]


class FacecamTargetedSearch:
    VERSION = 1
    WINDOW_SECONDS = 12
    MAX_WINDOWS = 24
    MAX_SHOTS = 96
    MAX_TARGETS = 32
    VISION_BATCH = 16
    MAX_SUBTITLE_BYTES = 40_000
    PROMPTS = Path(__file__).parents[2] / "prompts/facecam"

    @classmethod
    def targets(cls, state):
        """Apply the same frame/slot rules as placement BEFORE any model call."""
        from .facecam_visual_candidates import FacecamVisualCandidates

        context = FacecamVisualCandidates.context(state)
        if not context["scenes"] or not state.prepared:
            return {**context, "scenes": []}, []
        path = Path(state.prepared["directory"]) / "transcription.json"
        timing = Transcription.model_validate_json(path.read_text()).scenes
        cover = round(state.prepared["cover_start"] * 60)
        minimum = max(1, math.ceil(settings.facecam_illustration_min_preview_seconds * 60))
        slots = {}
        for i, scene in enumerate(timing):
            start = round(scene.start_time * 60)
            next_start = round(timing[i + 1].start_time * 60) if i + 1 < len(timing) else cover
            end = min(round(scene.end_time * 60), next_start, cover)
            slots[scene.scene_index] = 0 if scene.is_raw else min(2, max(0, (end - start) // minimum - 1))
        all_rows = state.assignment
        neighbours = {row["scene_index"]: " ".join(r["text"] for r in all_rows[max(0, i - 2):i + 3])
                      for i, row in enumerate(all_rows)}
        eligible = [{**s, "slots": slots.get(s["scene_index"], 0), "nearby_narration": neighbours[s["scene_index"]]}
                    for s in context["scenes"] if slots.get(s["scene_index"], 0)]
        # A bounded preview set also bounds decode time and total vision tokens.
        chosen = {s["scene_index"] for s in sorted(eligible, key=lambda s: (-s["slots"], s["scene_index"]))[:cls.MAX_TARGETS]}
        return {**context, "scenes": [s for s in eligible if s["scene_index"] in chosen]}, [
            s["scene_index"] for s in context["scenes"] if s["scene_index"] not in chosen]

    @classmethod
    def windows(cls, infos, subtitles):
        windows = {}
        for episode, (info, subs) in enumerate(zip(infos, subtitles)):
            for i in range(math.ceil(info["duration"] / cls.WINDOW_SECONDS)):
                start, end = i * cls.WINDOW_SECONDS, min(info["duration"], (i + 1) * cls.WINDOW_SECONDS)
                identity = f"e{episode}w{i}"
                windows[identity] = {"window_id": identity, "episode": episode, "start": start, "end": end,
                    "dialogue": " ".join(c["text"] for c in subs["cues"] if c["start"] < end and c["end"] > start)}
        return windows

    @classmethod
    def compact_subtitles(cls, windows):
        rows = [[identity, w["dialogue"][:260]] for identity, w in windows.items() if w["dialogue"]]
        # Retain coverage of every supplied episode; shorten each row uniformly if needed.
        while len(json.dumps(rows, ensure_ascii=False).encode()) > cls.MAX_SUBTITLE_BYTES:
            if max((len(row[1]) for row in rows), default=0) <= 40:
                raise ValueError("Les sous-titres dépassent la limite de recherche par projet. Réduisez les épisodes utilisés.")
            rows = [[identity, text[:max(40, len(text) * 3 // 4)]] for identity, text in rows]
        return rows

    @staticmethod
    def compatible(shot, original):
        return not (original and shot["episode"] == original["episode"]
                    and shot["start"] < original["end"] and shot["end"] > original["start"])

    @classmethod
    def select(cls, project_id, *, cancel_event=None):
        from .facecam_service import FacecamService
        from .facecam_visual_candidates import FacecamVisualCandidates, VisualSelection
        from .project_service import ProjectService
        from .anime_library import AnimeLibraryService

        started = time.monotonic()
        deadline = started + settings.facecam_illustration_timeout_seconds

        def check():
            check_cancelled(cancel_event)
            if time.monotonic() > deadline:
                raise ValueError("Délai de recherche V4 atteint ; les étapes terminées restent en cache.")

        with FacecamVisualIndex.locked("selection:" + project_id):
            check()
            state = FacecamService.load(project_id)
            if (not state.recording or state.recording.get("reviewed_revision") != state.revision
                    or state.approved_revision != state.revision
                    or (state.recording_edit and not state.recording_edit.applied)):
                raise ValueError("Validez la relecture du texte enregistré avant de rechercher des plans V4.")
            context, excluded = cls.targets(state)
            if not context["scenes"]:
                return []
            project = FacecamService.project(project_id)
            sources = FacecamVisualCandidates.episodes(project_id)
            infos = [FacecamWindowMedia.probe(path) for path in sources]
            subtitles = [FacecamVisualIndex.subtitles(path) for path in sources]
            windows = cls.windows(infos, subtitles)
            originals = {}
            matches = ProjectService.load_matches(project_id)
            for match in matches.matches if matches else []:
                path = AnimeLibraryService.resolve_episode_path(match.episode, library_type=project.media_library)
                if path:
                    originals[match.scene_index] = {"episode": str(path.resolve()), "start": match.start_time, "end": match.end_time}
            anchors = {}
            for target in context["scenes"]:
                original = originals.get(target["scene_index"])
                if original:
                    episode = next((i for i, info in enumerate(infos) if info["source"] == original["episode"]), None)
                    if episode is not None:
                        identity = f"e{episode}w{int((original['start'] + original['end']) / 2 // cls.WINDOW_SECONDS)}"
                        if identity in windows:
                            anchors[target["scene_index"]] = identity
            entry = LLMConfigService.facecam_illustrations_entry()
            localize_prompt = (cls.PROMPTS / "locate_illustrations.md").read_text()
            vision_prompt = (cls.PROMPTS / "verify_illustrations.md").read_text()
            # Retrying or changing a prompt/model cannot reset this context's spending limit.
            budget_key = fingerprint([FacecamVisualCandidates.context(state), originals,
                                      [i["identity"] for i in infos], subtitles])
            budget_dir = FacecamService.directory(project_id) / "targeted_budgets" / budget_key
            budget = IllustrationBudget(budget_dir, min(0.15, settings.facecam_illustration_budget_usd), entry, conservative=True)
            key = fingerprint([cls.VERSION, budget_key, context, [i["fingerprint"] for i in infos],
                               localize_prompt, vision_prompt, entry.model_dump(), cls.WINDOW_SECONDS,
                               cls.MAX_WINDOWS, cls.MAX_SHOTS, cls.MAX_TARGETS, cls.VISION_BATCH,
                               settings.facecam_illustration_min_preview_seconds])
            directory = FacecamService.directory(project_id) / "targeted_selection" / key
            result_path = directory / "candidates.json"

            def current():
                if cls.targets(FacecamService.load(project_id))[0] != context:
                    raise ValueError("La narration ou sa durée a changé pendant la recherche V4.")
                if ProjectService.load_matches(project_id) != matches:
                    raise ValueError("Les correspondances vidéo ont changé pendant la recherche V4.")
                if any(file_identity(path) != info["identity"] for path, info in zip(sources, infos)):
                    raise ValueError("Un épisode a changé pendant la recherche V4.")

            if result_path.exists():
                result = json.loads(result_path.read_text())
                cls.validate_result(result, context["scenes"], infos, originals)
                current()
                return result

            def call(name, prompt, schema, *, images=None, output=3072):
                check()
                path = directory / name
                if path.exists():
                    return json.loads(path.read_text())
                value = budget.call(prompt, schema, images=images, max_output_tokens=output, timeout=deadline - time.monotonic())
                write_json(path, value)
                return value

            rows = cls.compact_subtitles(windows)
            located = {}
            if rows:
                data = {**context, "anchors": anchors, "windows": rows}
                response = LocatedScenes.model_validate(call("locations.json", localize_prompt + "\n" + json.dumps(data, ensure_ascii=False),
                                                            LocatedScenes.model_json_schema(), output=1536))
                valid_targets = {s["scene_index"] for s in context["scenes"]}
                for scene in response.scenes:
                    if scene.scene_index in valid_targets and scene.scene_index not in located:
                        located[scene.scene_index] = next((i for i in scene.window_ids if i in windows), None)

            # Prefer the subtitle location, then a nearby alternative to the current match.
            suggestions = {}
            for target in context["scenes"]:
                index = target["scene_index"]
                choices = [located.get(index), anchors.get(index)]
                if anchors.get(index):
                    anchor = windows[anchors[index]]
                    previous = f"e{anchor['episode']}w{int(anchor['start'] // cls.WINDOW_SECONDS) - 1}"
                    following = f"e{anchor['episode']}w{int(anchor['start'] // cls.WINDOW_SECONDS) + 1}"
                    choices.extend((previous, following))
                suggestions[index] = list(dict.fromkeys(i for i in choices if i in windows))
            selected_windows = []
            for rank in range(4):
                for choices in suggestions.values():
                    if rank < len(choices) and choices[rank] not in selected_windows and len(selected_windows) < cls.MAX_WINDOWS:
                        selected_windows.append(choices[rank])

            def decode(identity):
                window = windows[identity]
                return identity, FacecamWindowMedia.window(infos[window["episode"]], window["start"], window["end"], check)

            check()
            with ThreadPoolExecutor(max_workers=2) as pool:
                decoded = dict(pool.map(decode, selected_windows))
            offered, catalogue = {}, {}
            # A few choices per target; no expensive captioning/index of every visible shot.
            for target in context["scenes"]:
                index = target["scene_index"]
                groups = []
                for identity in suggestions[index]:
                    window = windows[identity]
                    middle = (window["start"] + window["end"]) / 2
                    groups.append(sorted((s for s in decoded.get(identity, []) if cls.compatible(s, originals.get(index))),
                                         key=lambda s: abs((s["start"] + s["end"]) / 2 - middle)))
                choices = []
                for rank in range(4):
                    for group in groups:
                        if rank < len(group):
                            shot = group[rank]
                            if (shot["shot_id"] not in choices and len(choices) < target["slots"] * 2
                                    and (shot["shot_id"] in catalogue or len(catalogue) < cls.MAX_SHOTS)):
                                choices.append(shot["shot_id"])
                                catalogue[shot["shot_id"]] = shot
                offered[index] = choices

            results, rejected = [], []
            vision_targets = [s for s in context["scenes"] if offered[s["scene_index"]]]
            for offset in range(0, len(vision_targets), cls.VISION_BATCH):
                batch = vision_targets[offset:offset + cls.VISION_BATCH]
                ids = list(dict.fromkeys(identity for target in batch for identity in offered[target["scene_index"]]))
                shots = [catalogue[i] for i in ids]
                data = {"script": context["script"], "scenes": [{**s, "shot_ids": offered[s["scene_index"]]} for s in batch],
                        "shots": [{"shot_id": shot["shot_id"], "dialogue": " ".join(
                            c["text"] for info, subs in zip(infos, subtitles) if info["source"] == shot["episode"]
                            for c in subs["cues"] if c["start"] < shot["end"] and c["end"] > shot["start"])[:220]}
                            for shot in shots]}
                path = directory / f"vision_{offset:03d}.json"
                images = None if path.exists() else FacecamWindowMedia.contact_sheets(shots)
                response = VisualSelection.model_validate(call(path.name, vision_prompt + "\n" + json.dumps(data, ensure_ascii=False),
                                                               VisualSelection.model_json_schema(), images=images))
                batch_targets = {s["scene_index"]: s for s in batch}
                seen = set()
                for scene in response.scenes:
                    target = batch_targets.get(scene.scene_index)
                    for candidate in scene.alternatives:
                        identity = (scene.scene_index, candidate.shot_id)
                        if (not target or candidate.shot_id not in offered[scene.scene_index] or identity in seen
                                or sum(c["output_scene_index"] == scene.scene_index for c in results) >= target["slots"]):
                            rejected.append({"scene_index": scene.scene_index, "shot_id": candidate.shot_id})
                            continue
                        seen.add(identity)
                        results.append({**catalogue[candidate.shot_id], "output_scene_index": scene.scene_index,
                                        "reason": candidate.reason, "match_index": -len(results) - 1})
            cls.validate_result(results, context["scenes"], infos, originals)
            with FacecamService.lock(project_id):
                check()
                current()
                write_json(directory / "report.json", {"strategy": "targeted", "eligible_scenes": len(context["scenes"]),
                    "excluded_scenes": excluded, "windows_decoded": len(selected_windows), "shots_reviewed": len(catalogue),
                    "alternatives": len(results), "rejected": rejected, "elapsed_seconds": time.monotonic() - started,
                    "usage_path": str(budget.path), "budget_usd": budget.limit})
                write_json(result_path, results)
            return results

    @staticmethod
    def validate_result(results, targets, infos, originals):
        sources = {i["source"]: i for i in infos}
        capacities = {t["scene_index"]: t["slots"] for t in targets}
        counts, seen = {}, set()
        for shot in results:
            source = sources.get(shot["episode"])
            index = shot["output_scene_index"]
            identity = (index, shot["shot_id"])
            counts[index] = counts.get(index, 0) + 1
            if (not source or index not in capacities or counts[index] > capacities[index] or identity in seen
                    or not all(math.isfinite(shot[k]) for k in ("start", "end", "fps"))
                    or shot["episode_fingerprint"] != source["fingerprint"] or shot["fps"] != source["fps"]
                    or shot["end"] - shot["start"] + 1e-6 < settings.facecam_illustration_min_preview_seconds
                    or not 0 <= shot["in_frame"] < shot["out_frame"] <= source["frame_count"]
                    or abs(shot["start"] * shot["fps"] - shot["in_frame"]) > 1e-4
                    or abs(shot["end"] * shot["fps"] - shot["out_frame"]) > 1e-4
                    or not FacecamTargetedSearch.compatible(shot, originals.get(index))):
                raise ValueError("Une proposition V4 ne correspond pas aux images et bornes locales autorisées.")
            seen.add(identity)
