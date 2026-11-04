"""Choose at most two additional, visually grounded shots per approved scene."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..models import SceneMatch
from ..config import settings
from .anime_library import AnimeLibraryService


class AlternativeShot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    shot_id: str
    reason: str = Field(min_length=1, max_length=180)


class SceneAlternatives(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_index: StrictInt
    alternatives: list[AlternativeShot] = Field(max_length=2)


class VisualSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneAlternatives]


class FacecamVisualCandidates:
    @staticmethod
    def episodes(project_id):
        """Resolve only immutable project matches; never scan the anime/library."""
        from .facecam_service import FacecamService
        project = FacecamService.project(project_id)
        episodes = {}
        for match in FacecamService.source_matches(project_id).matches:
            if not match.episode:
                continue
            path = AnimeLibraryService.resolve_episode_path(match.episode, library_type=project.media_library)
            if path is None:
                raise ValueError(f"Épisode local introuvable : {match.episode}.")
            episodes.setdefault(str(path.resolve()), path.resolve())
        return list(episodes.values())

    @staticmethod
    def context(state):
        origins = {m["output_scene_index"]: m for m in state.montage_map}
        return {
            "script": [b.model_dump() for b in state.blocks],
            "scenes": [{"scene_index": row["scene_index"], "text": row["text"]}
                       for row in state.assignment
                       if row.get("text", "").strip()
                       and origins.get(row["scene_index"], {}).get("kind") in {"body", "hook"}],
        }

    @staticmethod
    def validate(value, targets, catalogue, originals):
        result = VisualSelection.model_validate(value)
        if [s.scene_index for s in result.scenes] != [s["scene_index"] for s in targets]:
            raise ValueError("La sélection doit couvrir chaque scène une seule fois, dans l'ordre.")
        for scene in result.scenes:
            seen = set()
            for candidate in scene.alternatives:
                shot = catalogue.get(candidate.shot_id)
                if not shot or candidate.shot_id in seen:
                    raise ValueError("La sélection contient un plan inconnu ou dupliqué.")
                seen.add(candidate.shot_id)
                original = originals.get(scene.scene_index)
                if original and shot["episode"] == original["episode"] and (
                    shot["start"] < original["end"] and shot["end"] > original["start"]):
                    raise ValueError("Une alternative recouvre le plan déjà proposé pour cette scène.")
        return result

    @classmethod
    def select(cls, project_id, *, cancel_event=None, progress=None):
        options = {"cancel_event": cancel_event}
        if settings.facecam_illustration_provider == "antigravity":
            options["progress"] = progress
        return cls.search().select(project_id, **options)

    @staticmethod
    def search():
        if settings.facecam_illustration_provider == "antigravity":
            from .facecam_antigravity_search import FacecamAntigravitySearch
            return FacecamAntigravitySearch
        from .facecam_targeted_search import FacecamTargetedSearch
        return FacecamTargetedSearch

    @classmethod
    def targets(cls, state):
        return cls.search().targets(state)

    @staticmethod
    def matches(candidates):
        return [SceneMatch(scene_index=c["match_index"], episode=c["episode"],
                           start_time=c["start"], end_time=c["end"], confidence=1, speed_ratio=1)
                for c in candidates]
