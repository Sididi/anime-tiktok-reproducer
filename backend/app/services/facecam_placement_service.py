"""One resumable scene-placement job per explicitly validated recording review."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings
from .facecam_service import FacecamService, StaleFacecamRevision
from .facecam_illustration_llm import write_json
from .facecam_visual_index import FacecamVisualIndex, file_identity, fingerprint


class FacecamPlacementService:
    @staticmethod
    def preserve_audio_normalization(before, after):
        """Record an app-verified audio repair that stream-copies the same video."""
        write_json(settings.cache_dir / "facecam_audio_repairs" / (fingerprint(before) + ".json"),
                   {"before": before, "after": after})

    @staticmethod
    def same_visual_source(source):
        current = file_identity(Path(source["source"]))
        identity = source["identity"]
        visited = set()
        while identity != current:
            key = fingerprint(identity)
            path = settings.cache_dir / "facecam_audio_repairs" / (key + ".json")
            if key in visited or not path.is_file():
                return False
            visited.add(key)
            repair = json.loads(path.read_text())
            if repair["before"] != identity:
                return False
            identity = repair["after"]
        return True

    @staticmethod
    def require_review(state, key=None):
        placement = state.placement
        if (not placement or (key is not None and placement.key != key)
                or placement.revision != state.revision or state.approved_revision != state.revision
                or not state.recording or state.recording.get("reviewed_revision") != state.revision
                or placement.fingerprint != state.recording["fingerprint"]
                or (state.recording_edit and not state.recording_edit.applied)):
            raise StaleFacecamRevision("Validez la relecture du texte enregistré avant de placer les scènes.")
        return placement

    @classmethod
    def directory(cls, project_id, placement):
        return FacecamService.directory(project_id) / "placements" / placement.key

    @classmethod
    def candidates(cls, project_id):
        """Processing and exports read the committed choice; they never start a search."""
        from .facecam_targeted_search import FacecamTargetedSearch
        from .facecam_visual_candidates import FacecamVisualCandidates
        state = FacecamService.load(project_id)
        placement = cls.require_review(state)
        if placement.status != "complete":
            raise ValueError("Attendez la fin du placement des scènes après la validation de la relecture.")
        path = cls.directory(project_id, placement) / "candidates.json"
        if not path.is_file():
            raise ValueError("Les plans validés sont indisponibles. Relancez le placement depuis la relecture.")
        saved = json.loads(path.read_text())
        if (saved["context"] != FacecamVisualCandidates.context(state)
                or any(not cls.same_visual_source(info) for info in saved["sources"])):
            raise ValueError("Le texte ou les épisodes ont changé depuis le placement validé.")
        allowed = {str(p) for p in FacecamVisualCandidates.episodes(project_id)}
        if any(info["source"] not in allowed for info in saved["sources"]):
            raise ValueError("Un épisode du placement n'appartient plus au projet.")
        # Keep the original selection even when silence/speed settings later change
        # available slots. Placement can trim/skip previews without paying again.
        FacecamTargetedSearch.validate_result(saved["candidates"], saved["targets"], saved["sources"], {})
        return saved["candidates"]

    @classmethod
    def run(cls, project_id, key):
        from .facecam_media_service import FacecamMediaService
        from .facecam_visual_candidates import FacecamVisualCandidates
        from .facecam_window_media import FacecamWindowMedia

        with FacecamVisualIndex.locked("placement:" + project_id):
            state = FacecamService.load(project_id)
            placement = cls.require_review(state, key)
            if placement.status == "complete":
                cls.candidates(project_id)
                return state

            def progress(message, value):
                current = FacecamService.load(project_id)
                cls.require_review(current, key)
                FacecamService.update(project_id, {"placement": current.placement.model_copy(update={"status": "running", "error": None}),
                    "job": {"status": "processing", "step": "Placement des scènes", "message": message, "progress": value}})

            try:
                progress("Répartition du texte enregistré sur les scènes", .1)
                FacecamService.assign_scenes(project_id, state.revision)
                progress("Synchronisation des scènes avec votre voix", .3)
                FacecamMediaService.prepare(project_id)
                progress("Recherche des illustrations supplémentaires", .6)
                candidates = FacecamVisualCandidates.select(project_id, progress=lambda message, fraction: progress(message, .6 + .3 * fraction))
                current = FacecamService.load(project_id)
                cls.require_review(current, key)
                targets, _ = FacecamVisualCandidates.targets(current)
                sources = [FacecamWindowMedia.probe(path) for path in FacecamVisualCandidates.episodes(project_id)]
                with FacecamService.lock(project_id):
                    current = FacecamService.load(project_id)
                    cls.require_review(current, key)
                    write_json(cls.directory(project_id, placement) / "candidates.json", {
                        "context": FacecamVisualCandidates.context(current), "targets": targets["scenes"],
                        "sources": sources, "candidates": candidates})
                    FacecamService.update(project_id, {"placement": current.placement.model_copy(update={"status": "complete", "error": None}),
                        "preview_enabled": True,
                        "job": {"status": "processing", "step": "Placement des scènes", "message": "Préparation de l'aperçu audio", "progress": .95}})
                # Failure of a music mix must not discard or rerun successful placement.
                return FacecamMediaService.preview(project_id)
            except Exception as exc:
                with FacecamService.lock(project_id):
                    current = FacecamService.load(project_id)
                    if current.placement and current.placement.key == key and current.placement.status != "complete":
                        FacecamService.update(project_id, {"placement": current.placement.model_copy(update={"status": "error", "error": str(exc)})})
                raise
