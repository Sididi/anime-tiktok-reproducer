"""Shared episode overviews, wider retrieval, then three-frame visual verification."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..config import settings
from .antigravity_illustration_client import AntigravityIllustrationClient
from .facecam_episode_overview import FacecamEpisodeOverview
from .facecam_illustration_llm import write_json
from .facecam_targeted_search import FacecamTargetedSearch
from .facecam_visual_index import FacecamVisualIndex, check_cancelled, file_identity, fingerprint
from .facecam_window_media import FacecamWindowMedia


class EpisodeLocations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_index: StrictInt
    window_ids: list[str] = Field(max_length=2)


class LocatedScenes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[EpisodeLocations]


class FacecamAntigravitySearch(FacecamTargetedSearch):
    VERSION = 1
    MAX_TARGETS = 64
    MAX_WINDOWS = 48
    MAX_SHOTS = 160
    VISION_BATCH = 8

    @classmethod
    def select(cls, project_id, *, cancel_event=None, progress=None):
        from .anime_library import AnimeLibraryService
        from .facecam_service import FacecamService
        from .facecam_visual_candidates import FacecamVisualCandidates
        from .project_service import ProjectService

        started = time.monotonic()
        deadline = started + settings.facecam_antigravity_timeout_seconds

        def check():
            check_cancelled(cancel_event)
            if time.monotonic() >= deadline:
                raise TimeoutError("Délai global de recherche des clips atteint ; les étapes terminées restent en cache.")

        with FacecamVisualIndex.locked("selection:" + project_id, check=check):
            check()
            state = FacecamService.load(project_id)
            if (not state.recording or state.recording.get("reviewed_revision") != state.revision
                    or state.approved_revision != state.revision
                    or (state.recording_edit and not state.recording_edit.applied)):
                raise ValueError("Validez la relecture du texte enregistré avant de rechercher des clips.")
            context, excluded = cls.targets(state)
            if not context["scenes"]:
                return []
            project = FacecamService.project(project_id)
            sources = FacecamVisualCandidates.episodes(project_id)
            infos = [FacecamWindowMedia.probe(path) for path in sources]
            subtitles = [FacecamVisualIndex.subtitles(path) for path in sources]
            windows = cls.windows(infos, subtitles)
            matches = ProjectService.load_matches(project_id)
            originals = {}
            for match in matches.matches:
                if match.episode:
                    path = AnimeLibraryService.resolve_episode_path(match.episode, library_type=project.media_library)
                    if path is not None:
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
            localize_prompt = (cls.PROMPTS / "locate_illustrations_antigravity.md").read_text()
            vision_prompt = (cls.PROMPTS / "verify_illustrations_antigravity.md").read_text()
            key = fingerprint([cls.VERSION, context, originals, [i["identity"] for i in infos], subtitles,
                localize_prompt, vision_prompt, FacecamEpisodeOverview.PROMPT.read_text(), FacecamEpisodeOverview.VERSION,
                settings.facecam_antigravity_model, settings.facecam_antigravity_overview_model,
                cls.MAX_WINDOWS, cls.MAX_SHOTS, cls.MAX_TARGETS, cls.VISION_BATCH,
                FacecamEpisodeOverview.MAX_FRAMES, FacecamEpisodeOverview.FRAMES_PER_EPISODE,
                settings.facecam_illustration_min_preview_seconds])
            directory = FacecamService.directory(project_id) / "antigravity_selection" / key
            result_path = directory / "candidates.json"

            def current():
                check()
                now = FacecamService.load(project_id)
                if (cls.targets(now)[0] != context or not now.recording
                        or now.approved_revision != now.revision
                        or now.recording.get("reviewed_revision") != now.revision
                        or (now.recording_edit and not now.recording_edit.applied)
                        or now.recording.get("fingerprint") != state.recording.get("fingerprint")):
                    raise ValueError("La relecture ou la narration a changé pendant la recherche de clips.")
                if ProjectService.load_matches(project_id) != matches:
                    raise ValueError("Les correspondances vidéo ont changé pendant la recherche de clips.")
                if any(file_identity(path) != info["identity"] for path, info in zip(sources, infos)):
                    raise ValueError("Un épisode a changé pendant la recherche de clips.")

            if result_path.is_file():
                result = json.loads(result_path.read_text())
                cls.validate_result(result, context["scenes"], infos, originals)
                current()
                return result
            client = AntigravityIllustrationClient(directory, deadline, current)
            locations_path = directory / "locations.json"
            overview_stats = {}
            if locations_path.is_file():
                locations = LocatedScenes.model_validate_json(locations_path.read_text())
            else:
                if progress:
                    progress(f"Repérage visuel dans les {len(infos)} épisode(s) du projet", .05)
                observations, overview_stats = FacecamEpisodeOverview.describe(infos, subtitles, client, check)
                if progress:
                    progress("Recherche des passages adaptés au récit", .35)
                data = {**context, "anchors": anchors, "windows": cls.compact_subtitles(windows), "visual_overview": observations}
                value = client.call("locations", localize_prompt + "\n" + json.dumps(data, ensure_ascii=False),
                                    LocatedScenes.model_json_schema(), model=settings.facecam_antigravity_model)
                locations = LocatedScenes.model_validate(value)
                cls.validate_locations(locations, context["scenes"], windows)
                current()
                write_json(locations_path, locations.model_dump())
                write_json(directory / "overview.json", overview_stats)
            cls.validate_locations(locations, context["scenes"], windows)
            located = {s.scene_index: s.window_ids for s in locations.scenes}
            suggestions = {}
            for target in context["scenes"]:
                index = target["scene_index"]
                choices = [*located.get(index, []), anchors.get(index)]
                for identity in list(choices):
                    if identity in windows:
                        w = windows[identity]
                        n = int(w["start"] // cls.WINDOW_SECONDS)
                        choices.extend((f"e{w['episode']}w{n - 1}", f"e{w['episode']}w{n + 1}"))
                suggestions[index] = list(dict.fromkeys(i for i in choices if i in windows))
            selected = []
            for rank in range(max(map(len, suggestions.values()), default=0)):
                for choices in suggestions.values():
                    if rank < len(choices) and choices[rank] not in selected and len(selected) < cls.MAX_WINDOWS:
                        selected.append(choices[rank])

            def decode(identity):
                w = windows[identity]
                return identity, FacecamWindowMedia.window(infos[w["episode"]], w["start"], w["end"], check, sample_count=3)

            if progress:
                progress("Extraction des plans candidats", .55)
            with ThreadPoolExecutor(max_workers=2) as pool:
                decoded = dict(pool.map(decode, selected))
            offered, catalogue = {}, {}
            for target in context["scenes"]:
                index = target["scene_index"]
                groups = [sorted((s for s in decoded.get(identity, []) if cls.compatible(s, originals.get(index))),
                                 key=lambda s: abs((s["start"] + s["end"]) / 2
                                     - (windows[identity]["start"] + windows[identity]["end"]) / 2))
                          for identity in suggestions[index]]
                choices = []
                for rank in range(max(map(len, groups), default=0)):
                    for group in groups:
                        if rank < len(group):
                            shot = group[rank]
                            if (shot["shot_id"] not in choices and len(choices) < target["slots"] * 2
                                    and (shot["shot_id"] in catalogue or len(catalogue) < cls.MAX_SHOTS)):
                                choices.append(shot["shot_id"])
                                catalogue[shot["shot_id"]] = shot
                offered[index] = choices
            targets = [s for s in context["scenes"] if offered[s["scene_index"]]]
            if progress:
                progress(f"Vérification visuelle des plans pour {len(targets)} scène(s)", .75)

            def verify(offset):
                current()
                batch = targets[offset:offset + cls.VISION_BATCH]
                path = directory / f"vision-{offset:03d}.json"
                ids = list(dict.fromkeys(i for s in batch for i in offered[s["scene_index"]]))
                shots = [catalogue[i] for i in ids]
                data = {"script": context["script"],
                    "scenes": [{**s, "shot_ids": offered[s["scene_index"]]} for s in batch],
                    "shots": [{"shot_id": s["shot_id"], "dialogue": " ".join(c["text"]
                        for info, subs in zip(infos, subtitles) if info["source"] == s["episode"]
                        for c in subs["cues"] if c["start"] < s["end"] and c["end"] > s["start"])[:400]} for s in shots]}
                if path.is_file():
                    value = json.loads(path.read_text())
                else:
                    value = client.call(f"vision-{offset:03d}", vision_prompt + "\n" + json.dumps(data, ensure_ascii=False),
                        cls.selection_schema(batch, offered), model=settings.facecam_antigravity_model,
                        images=FacecamWindowMedia.contact_sheets(shots, shots_per_sheet=8))
                response = FacecamVisualCandidates.validate(value, batch, catalogue, originals)
                results = []
                for target, scene in zip(batch, response.scenes):
                    if len(scene.alternatives) > target["slots"]:
                        raise ValueError("Trop de propositions pour la durée disponible de cette scène.")
                    for alternative in scene.alternatives:
                        if alternative.shot_id not in offered[scene.scene_index]:
                            raise ValueError("Antigravity a choisi un plan qui n'était pas proposé pour cette scène.")
                        results.append({**catalogue[alternative.shot_id], "output_scene_index": scene.scene_index,
                                        "reason": alternative.reason, "match_index": -1})
                current()
                write_json(path, response.model_dump())
                return results

            with ThreadPoolExecutor(max_workers=2) as pool:
                batches = list(pool.map(verify, range(0, len(targets), cls.VISION_BATCH)))
            results = [shot for batch in batches for shot in batch]
            for i, shot in enumerate(results):
                shot["match_index"] = -i - 1
            cls.validate_result(results, context["scenes"], infos, originals)
            with FacecamService.lock(project_id):
                current()
                if (directory / "overview.json").is_file():
                    overview_stats = json.loads((directory / "overview.json").read_text())
                write_json(directory / "report.json", {"strategy": "antigravity", "billing": "subscription",
                    "credit_overages": False, "model": settings.facecam_antigravity_model,
                    "eligible_scenes": len(context["scenes"]), "excluded_scenes": excluded,
                    "scenes_without_candidates": [s["scene_index"] for s in context["scenes"] if not offered[s["scene_index"]]],
                    "episodes": len(infos), "overview": overview_stats, "windows_decoded": len(selected),
                    "shots_reviewed": len(catalogue), "alternatives": len(results),
                    "elapsed_seconds": time.monotonic() - started, "deadline_seconds": settings.facecam_antigravity_timeout_seconds})
                write_json(result_path, results)
            return results

    @staticmethod
    def selection_schema(targets, offered):
        """Constrain IDs per scene, including when a shot is visible for another scene."""
        from .facecam_visual_candidates import AlternativeShot

        rows = []
        for target in targets:
            alternative = AlternativeShot.model_json_schema()
            alternative["properties"]["shot_id"]["enum"] = offered[target["scene_index"]]
            rows.append({"type": "object", "additionalProperties": False,
                "required": ["scene_index", "alternatives"], "properties": {
                    "scene_index": {"type": "integer", "enum": [target["scene_index"]]},
                    "alternatives": {"type": "array", "maxItems": target["slots"], "items": alternative}}})
        return {"type": "object", "additionalProperties": False, "required": ["scenes"],
            "properties": {"scenes": {"type": "array", "minItems": len(rows), "maxItems": len(rows),
                                      "items": {"oneOf": rows}}}}

    @staticmethod
    def validate_locations(locations, targets, windows):
        if [s.scene_index for s in locations.scenes] != [t["scene_index"] for t in targets]:
            raise ValueError("La localisation doit couvrir chaque scène une seule fois, dans l'ordre.")
        if any(len(s.window_ids) != len(set(s.window_ids)) or any(i not in windows for i in s.window_ids) for s in locations.scenes):
            raise ValueError("La localisation contient une fenêtre inconnue ou dupliquée.")
