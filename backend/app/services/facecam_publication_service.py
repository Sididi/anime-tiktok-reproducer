"""Publication choices are bound to their script and survive navigation/restarts."""
from __future__ import annotations

import hashlib
import uuid

from ..library_types import LibraryType
from .facecam_service import FacecamService, StaleFacecamRevision
from .facecam_visual_index import fingerprint
from .llm_service import LLMService
from .metadata import MetadataService
from .project_service import ProjectService


class FacecamPublicationService:
    @staticmethod
    def script(project_id):
        state = FacecamService.load(project_id)
        reference = {s.scene_index: s.text for s in FacecamService.reference(project_id).scenes}
        return {"scenes": [{"text": block.text if block.kind != "raw" else
                            " ".join(reference.get(index, "") for index in block.scene_indices)}
                           for block in state.blocks]}

    @classmethod
    def signature(cls, project_id):
        project = FacecamService.project(project_id)
        return fingerprint([project.anime_name, cls.script(project_id)])

    @staticmethod
    def digest(project_id):
        path = ProjectService.get_metadata_file(project_id)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    @classmethod
    def status(cls, project_id):
        state = FacecamService.load(project_id)
        generation = state.metadata_generation
        current = bool(generation and generation["signature"] == cls.signature(project_id))
        metadata = None
        if current and generation.get("selected_title") and generation.get("digest") == cls.digest(project_id):
            try:
                metadata = MetadataService.load(project_id)
            except ValueError:
                pass
        return {"revision": state.revision, "generation_id": generation["id"] if current else None,
                "candidates": generation["candidates"] if current else None,
                "metadata": metadata.model_dump() if metadata else None,
                "ready": metadata is not None and state.approved_revision == state.revision}

    @classmethod
    def generate(cls, project_id, revision):
        state, project = FacecamService.load(project_id), FacecamService.project(project_id)
        if state.revision != revision or state.approved_revision != revision:
            raise StaleFacecamRevision("Validez le script actuel avant de générer les métadonnées.")
        if project.library_type != LibraryType.ANIME or not (project.anime_name or "").strip():
            raise ValueError("Un Facecam nécessite le nom de l'anime.")
        signature = cls.signature(project_id)
        prompt = MetadataService.build_prompt_from_script_payload(
            project.anime_name, cls.script(project_id), "fr", LibraryType.ANIME)
        # Exactly the faceless metadata prompt, schema, model preset and light tier.
        candidates = MetadataService.validate_candidate_payload(LLMService.generate_json(
            prompt, preset_key=project.resolved_llm_preset_key(), tier="light",
            response_schema=MetadataService.candidate_response_schema(), schema_name="metadata"))
        with FacecamService.lock(project_id):
            if cls.signature(project_id) != signature:
                raise StaleFacecamRevision("Le script a changé ; régénérez les métadonnées.")
            FacecamService.update(project_id, {"metadata_generation": {
                "id": uuid.uuid4().hex, "signature": signature, "candidates": candidates.model_dump(),
                "selected_title": None, "digest": None}}, revision)
            cls.invalidate_bundle(project_id)

    @classmethod
    def require_generation(cls, project_id, revision, generation_id):
        state = FacecamService.load(project_id)
        generation = state.metadata_generation
        if (state.revision != revision or state.approved_revision != revision or not generation
                or generation["id"] != generation_id or generation["signature"] != cls.signature(project_id)):
            raise StaleFacecamRevision("Les métadonnées ne correspondent plus au script. Rechargez les propositions.")
        if project_id in FacecamService._processing:
            raise ValueError("Attendez la fin du traitement Facecam.")
        return generation

    @classmethod
    def select_title(cls, project_id, revision, generation_id, title):
        with FacecamService.lock(project_id):
            generation = cls.require_generation(project_id, revision, generation_id)
            if title not in generation["candidates"]["title_candidates"]:
                raise ValueError("Choisissez l'un des huit titres proposés.")
            payload = MetadataService.resolve_candidate_payload(generation["candidates"], selected_title=title)
            MetadataService.save(project_id, payload)
            FacecamService.update(project_id, {"metadata_generation": {
                **generation, "selected_title": title, "digest": cls.digest(project_id)}}, revision)
            cls.invalidate_bundle(project_id)
            return cls.status(project_id)

    @classmethod
    def save_metadata(cls, project_id, revision, generation_id, payload):
        with FacecamService.lock(project_id):
            generation = cls.require_generation(project_id, revision, generation_id)
            if not generation.get("selected_title"):
                raise ValueError("Choisissez d'abord un titre parmi les propositions.")
            MetadataService.save(project_id, MetadataService.validate_payload(payload))
            FacecamService.update(project_id, {"metadata_generation": {
                **generation, "digest": cls.digest(project_id)}}, revision)
            cls.invalidate_bundle(project_id)
            return cls.status(project_id)

    @staticmethod
    def invalidate_bundle(project_id):
        # Publication edits do not change the timeline; only the packaged ZIP is stale.
        from .export_service import ExportService
        project = FacecamService.project(project_id)
        (ProjectService.get_project_dir(project_id) / f"{ExportService.output_folder_name(project)}.zip").unlink(missing_ok=True)

    @classmethod
    def bundle_signature(cls, project_id):
        state = FacecamService.load(project_id)
        return fingerprint([state.metadata_generation, cls.digest(project_id), state.thumbnail])

    @classmethod
    def require_ready(cls, project_id):
        if not cls.status(project_id)["ready"]:
            raise ValueError("Générez les métadonnées et choisissez un titre avant le traitement.")
        from .facecam_thumbnail_service import FacecamThumbnailService
        return FacecamThumbnailService.require_selected(project_id)
