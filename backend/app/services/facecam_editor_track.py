"""Independent, cached illustration selection and native-speed Premiere placement."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .atomic_files import write_text_atomic


class IllustrationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scene_index: StrictInt
    keep: StrictBool
    reason: str = Field(min_length=1)


class IllustrationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[IllustrationDecision]


class FacecamEditorTrackService:
    VERSION = 1
    PROMPT = Path(__file__).parents[2] / "prompts/facecam/editor_track.md"

    @staticmethod
    def validate_selection(value, candidates) -> IllustrationSelection:
        result = IllustrationSelection.model_validate(value)
        if [d.scene_index for d in result.decisions] != [c["scene_index"] for c in candidates]:
            raise ValueError("La sélection V4 doit évaluer chaque candidat une fois, dans l'ordre source.")
        return result

    @classmethod
    def selection(cls, project_id):
        from .facecam_service import FacecamService

        state = FacecamService.load(project_id)
        reference = FacecamService.reference(project_id)
        matches = FacecamService.source_matches(project_id)
        story = FacecamService.selected_story(project_id, state)
        match_map = {m.scene_index: m for m in matches.matches}
        positions = {s.scene_index: i for i, s in enumerate(reference.scenes)}
        anchors = [m for m in state.montage_map if m.get("kind") in {"body", "raw"}
                   and m.get("source_scene_index") in positions]
        retained = {m["source_scene_index"] for m in anchors}
        candidates = []
        if anchors:
            first, last = min(positions[i] for i in retained), max(positions[i] for i in retained)
            for scene in reference.scenes[first + 1:last]:
                match = match_map.get(scene.scene_index)
                if (scene.scene_index in retained or not match or not match.episode
                        or not all(math.isfinite(v) for v in (match.start_time, match.end_time))
                        or match.start_time < 0 or match.end_time <= match.start_time):
                    continue
                candidates.append({"scene_index": scene.scene_index, "text": scene.text,
                                   "is_raw": scene.is_raw})
        context = {
            "script": [b.model_dump() for b in state.blocks],
            "story": story.model_dump(exclude={"clips"}) if story else None,
            "retained": [{"scene_index": s.scene_index, "text": s.text}
                         for s in reference.scenes if s.scene_index in retained],
            "candidates": candidates,
        }
        prompt = cls.PROMPT.read_text(encoding="utf-8")
        fingerprint = hashlib.sha256(json.dumps({
            "version": cls.VERSION, "prompt": prompt, "context": context,
            "references": reference.model_dump(), "matches": matches.model_dump(),
            "montage_map": state.montage_map,
            "model": FacecamService.project(project_id).resolved_llm_preset_key(),
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = FacecamService.directory(project_id) / "editor_selection.json"
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["fingerprint"] == fingerprint:
                    return cls.validate_selection(cached["selection"], candidates)
            except (ValueError, KeyError, TypeError):
                pass
        if candidates:
            value = FacecamService.llm(project_id, prompt + "\n" + json.dumps(context, ensure_ascii=False))
        else:
            value = {"decisions": []}
        selection = cls.validate_selection(value, candidates)
        # Concurrent edits must not install a selection for a superseded script.
        with FacecamService.lock(project_id):
            current = FacecamService.load(project_id)
            if current.revision != state.revision or current.montage_map != state.montage_map:
                raise ValueError("Le récit Facecam a changé pendant la sélection V4. Relancez le traitement.")
            write_text_atomic(path, json.dumps({"fingerprint": fingerprint,
                                               "selection": selection.model_dump()},
                                              ensure_ascii=False, indent=2))
        return selection

    @classmethod
    def selected_matches(cls, project_id, *, cancel_event=None):
        from .facecam_visual_candidates import FacecamVisualCandidates
        return FacecamVisualCandidates.matches(FacecamVisualCandidates.select(project_id, cancel_event=cancel_event))

    @classmethod
    def build(cls, project, scenes, source_rate):
        from .facecam_service import FacecamService
        from .facecam_visual_candidates import FacecamVisualCandidates
        from .facecam_visual_index import FacecamVisualIndex
        from .processing import ProcessingService

        state = FacecamService.load(project.id)
        candidates = FacecamVisualCandidates.select(project.id)
        resolved = {}
        for candidate, match in zip(candidates, FacecamVisualCandidates.matches(candidates)):
            resolved.update(ProcessingService.resolve_scene_sources([match],
                FacecamVisualIndex.native_rate(candidate["fps"]), library_type=project.media_library))
        return cls.place_alternatives(scenes, candidates, resolved, state.prepared["cover_start"], source_rate)

    @classmethod
    def place_alternatives(cls, scenes, candidates, sources, cover_start, source_rate):
        """Three choices maximum, all contained in the associated narration scene."""
        from ..config import settings

        clips, skipped = [], []
        cover_frame = round(cover_start * 60)
        minimum = max(1, math.ceil(settings.facecam_illustration_min_preview_seconds * 60))
        for index, scene in enumerate(scenes):
            start = round(scene["start"] * 60)
            next_start = round(scenes[index + 1]["start"] * 60) if index + 1 < len(scenes) else cover_frame
            end = min(round(scene["end"] * 60), next_start, cover_frame)
            if start < 0 or end <= start:
                raise ValueError(f"Scène V4 sans durée exploitable : {scene['scene_index']}.")
            original = {k: scene[k] for k in (
                "clipName", "source_in", "source_out", "source_in_frame", "source_out_frame")}
            original.update(id=f"v3_{scene['scene_index']}", output_scene_index=scene["scene_index"],
                            kind="retained", effective_speed=1.0)
            options = [original]
            # No tiny flash of a candidate simply to reach three choices.
            capacity = 0 if scene.get("is_raw") else min(2, max(0, (end - start) // minimum - 1))
            for candidate in (c for c in candidates if c["output_scene_index"] == scene["scene_index"]):
                if len(options) - 1 >= capacity:
                    skipped.append({"shot_id": candidate["shot_id"], "scene_index": scene["scene_index"],
                                    "reason": "Scène trop courte ou dialogue original protégé."})
                    continue
                source = sources.get(candidate["match_index"])
                if source is None:
                    raise ValueError(f"Source V4 introuvable : {candidate['shot_id']}.")
                if source.source_duration_seconds * 60 < minimum:
                    skipped.append({"shot_id": candidate["shot_id"], "scene_index": scene["scene_index"],
                                    "reason": "Plan source trop court pour une proposition lisible."})
                    continue
                options.append({"id": f"visual_{scene['scene_index']}_{len(options)}",
                    "output_scene_index": scene["scene_index"], "kind": "discovered", "effective_speed": 1.0,
                    "shot_id": candidate["shot_id"], "episode": candidate["episode"],
                    "episode_fingerprint": candidate["episode_fingerprint"], "reason": candidate["reason"],
                    "source_fps": candidate["fps"],
                    "clipName": source.clip_name, "source_in": source.source_in_seconds,
                    "source_out": source.source_out_seconds, "source_in_frame": source.source_in_frame,
                    "source_out_frame": source.source_out_frame})
            for rank, clip in enumerate(options):
                # Integer divisions partition exactly; there is no cumulative drift.
                slot_start = start + (end - start) * rank // len(options)
                slot_end = start + (end - start) * (rank + 1) // len(options)
                duration = ((clip["source_out_frame"] - clip["source_in_frame"]) / clip["source_fps"]
                            if clip.get("source_fps") else source_rate.seconds_from_frames(
                                clip["source_out_frame"] - clip["source_in_frame"]))
                native_frames = math.floor(duration * 60 + 1e-6)
                clip.update(start_frame=slot_start, end_frame=min(slot_end, slot_start + native_frames),
                            candidate_rank=rank, scene_start_frame=start, scene_end_frame=end)
                if clip["end_frame"] <= slot_start:
                    raise ValueError(f"Durée native V4 invalide : {clip['id']}.")
                identity = json.dumps([clip["clipName"], clip["source_in_frame"], clip["source_out_frame"]])
                clip["subclip_key"] = clip["id"] + "_" + hashlib.sha256(identity.encode()).hexdigest()[:12]
                clips.append(clip)
        return {"version": 2, "clips": clips, "skipped": skipped}

    @classmethod
    def place(cls, scenes, montage_map, references, selection, sources, cover_start, source_rate):
        """Keep V3 starts/trims; interpolate additions only between original-scene anchors."""
        origins = {m["output_scene_index"]: m for m in montage_map}
        source_positions = {s.scene_index: i for i, s in enumerate(references)}
        reference_map = {s.scene_index: s for s in references}
        clips, anchors = [], []
        for scene in scenes:
            origin = origins.get(scene["scene_index"], {})
            clip = {k: scene[k] for k in ("clipName", "source_in", "source_out", "source_in_frame", "source_out_frame")}
            clip.update(id="v3_" + str(scene["scene_index"]), start_frame=round(scene["start"] * 60),
                        source_scene_index=origin.get("source_scene_index"),
                        output_scene_index=scene["scene_index"], kind="retained", effective_speed=1.0)
            clips.append(clip)
            if origin.get("kind") in {"body", "raw"} and clip["source_scene_index"] in source_positions:
                anchors.append((scene, clip))
        skipped = []
        for decision in selection.decisions:
            if not decision.keep:
                continue
            source_index = decision.scene_index
            position = source_positions[source_index]
            pair = next(((left, right) for left, right in zip(anchors, anchors[1:])
                         if source_positions[left[1]["source_scene_index"]] < position
                         < source_positions[right[1]["source_scene_index"]]), None)
            if pair is None:
                skipped.append({"scene_index": source_index, "reason": "Aucun intervalle entre les repères V3."})
                continue
            (left_scene, left), (_, right) = pair
            left_source = reference_map[left["source_scene_index"]].start_time
            right_source = reference_map[right["source_scene_index"]].start_time
            span = right_source - left_source
            if span <= 0:
                raise ValueError("Les repères source V4 ne suivent pas l'ordre chronologique.")
            ratio = (reference_map[source_index].start_time - left_source) / span
            start = round(left["start_frame"] + ratio * (right["start_frame"] - left["start_frame"]))
            # Do not replace the picture accompanying protected original dialogue.
            lower = round(left_scene["end"] * 60) if left_scene.get("is_raw") else left["start_frame"] + 1
            start = max(start, lower)
            if start >= right["start_frame"]:
                skipped.append({"scene_index": source_index, "reason": "Pas de place entre les repères, dialogue original protégé."})
                continue
            source = sources.get(source_index)
            if source is None:
                raise ValueError(f"Source vidéo introuvable pour l'illustration V4 {source_index}.")
            clips.append({"id": "source_" + str(source_index), "source_scene_index": source_index,
                          "kind": "extra", "start_frame": start, "effective_speed": 1.0,
                          "clipName": source.clip_name, "source_in": source.source_in_seconds,
                          "source_out": source.source_out_seconds, "source_in_frame": source.source_in_frame,
                          "source_out_frame": source.source_out_frame, "reason": decision.reason})
        # Frame collisions may happen when many tiny shots share a short anchor span.
        # Retained clips always win; extras keep their original order and are reported if skipped.
        clips.sort(key=lambda c: (c["start_frame"], c["kind"] == "extra", source_positions.get(c["source_scene_index"], -1)))
        unique = []
        for clip in clips:
            if unique and clip["start_frame"] == unique[-1]["start_frame"]:
                if clip["kind"] == "retained":
                    raise ValueError("Deux clips V3 partagent le même départ sur V4.")
                skipped.append({"scene_index": clip["source_scene_index"], "reason": "Même image de départ qu'un autre plan."})
            else:
                unique.append(clip)
        cover_frame = round(cover_start * 60)
        for index, clip in enumerate(unique):
            duration = source_rate.seconds_from_frames(clip["source_out_frame"] - clip["source_in_frame"])
            native_frames = math.floor(duration * 60 + 1e-6)
            next_start = unique[index + 1]["start_frame"] if index + 1 < len(unique) else cover_frame
            clip["end_frame"] = min(clip["start_frame"] + native_frames, next_start, cover_frame)
            if clip["start_frame"] < 0 or clip["end_frame"] <= clip["start_frame"]:
                raise ValueError(f"Durée inexploitable sur V4 : {clip['id']}.")
            identity = json.dumps([clip["clipName"], clip["source_in_frame"], clip["source_out_frame"]])
            clip["subclip_key"] = clip["id"] + "_" + hashlib.sha256(identity.encode()).hexdigest()[:12]
        return {"version": cls.VERSION, "clips": unique, "skipped": skipped}

    @staticmethod
    def payload_from_jsx(jsx):
        match = re.search(r"var FACECAM\s*=\s*", jsx)
        if not match:
            return {}
        value, _ = json.JSONDecoder().raw_decode(jsx[match.end():])
        return (value or {}).get("editor_track", {})

    @classmethod
    def export_matches(cls, project_id, jsx):
        """Package the exact selection embedded in the JSX, without another model call."""
        from .facecam_service import FacecamService

        clips = cls.payload_from_jsx(jsx).get("clips", [])
        discovered = [c for c in clips if c["kind"] == "discovered"]
        result = []
        if discovered:
            from ..models import SceneMatch
            from .facecam_visual_candidates import FacecamVisualCandidates
            from .anime_library import AnimeLibraryService
            from .processing import ProcessingService
            allowed = {str(p.resolve()) for p in FacecamVisualCandidates.episodes(project_id)}
            project = FacecamService.project(project_id)
            for i, clip in enumerate(discovered):
                path = AnimeLibraryService.resolve_episode_path(clip["episode"], library_type=project.media_library)
                if path is None or str(path.resolve()) not in allowed:
                    raise ValueError("Une alternative V4 référence un épisode extérieur au projet.")
                if (not all(math.isfinite(clip[k]) for k in ("source_in", "source_out"))
                        or not 0 <= clip["source_in"] < clip["source_out"]):
                    raise ValueError("Une alternative V4 contient des bornes invalides.")
                _, clip_name = ProcessingService._resolve_source_reference(clip["episode"], library_type=project.media_library)
                if clip["clipName"] != clip_name:
                    raise ValueError("Le média d'une alternative V4 a changé depuis le traitement.")
                result.append(SceneMatch(scene_index=-i - 1, episode=str(path), start_time=clip["source_in"],
                                         end_time=clip["source_out"], confidence=1, speed_ratio=1))
        indices = {c["source_scene_index"] for c in clips
                   if c["kind"] == "extra"}
        if not indices:
            return result
        matches = {m.scene_index: m for m in FacecamService.source_matches(project_id).matches}
        if not indices.issubset(matches):
            raise ValueError("Une illustration V4 ne correspond plus aux références du projet.")
        return [*result, *[matches[i] for i in sorted(indices)]]
