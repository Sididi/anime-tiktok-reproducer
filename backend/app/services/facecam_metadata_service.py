"""One Facecam description, with deterministic platform metadata."""
from pathlib import Path

from ..models.metadata import FacecamMetadataCandidates, METADATA_TITLE_MAX_CHARS, VideoMetadataPayload
from .metadata import MetadataService


class FacecamMetadataService(MetadataService):
    VERSION = 2
    FACEBOOK_TAG = "Anime TV & Movies"
    FACEBOOK_HASHTAGS = "#anime #animerecommendations #manga #animefr #otaku"
    INSTAGRAM_HASHTAGS = "#anime #animefr #animerecommendations"
    TIKTOK_DESCRIPTION = "Recommendation anime #animerecommendations"

    @classmethod
    def candidate_response_schema(cls):
        return {
            "type": "object",
            "properties": {
                "title_candidates": MetadataService.candidate_response_schema()["properties"]["title_candidates"],
                "description": {"type": "string"},
                "youtube_tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["title_candidates", "description", "youtube_tags"],
            "additionalProperties": False,
        }

    @classmethod
    def build_prompt(cls, anime_name, script_text, target_language="fr", library_type=None):
        template = Path(__file__).resolve().parents[2] / "prompts" / "facecam" / "metadata.md"
        return template.read_text(encoding="utf-8").replace("[OEUVRE]", anime_name).replace("[SCRIPT]", script_text)

    @classmethod
    def validate_candidate_payload(cls, payload):
        return FacecamMetadataCandidates.model_validate(payload)

    @classmethod
    def compose(cls, *, title, description, youtube_tags):
        title, description = title.strip(), description.strip()
        if not title or len(title) > METADATA_TITLE_MAX_CHARS:
            raise ValueError(f"Le titre doit contenir entre 1 et {METADATA_TITLE_MAX_CHARS} caractères.")
        return VideoMetadataPayload.model_validate({
            "facebook": {"title": title, "description": f"{description}\n\n{cls.FACEBOOK_HASHTAGS}",
                         "tags": [cls.FACEBOOK_TAG]},
            "instagram": {"caption": f"{description}\n\n{cls.INSTAGRAM_HASHTAGS}"},
            "youtube": {"title": title, "description": description, "tags": youtube_tags},
            "tiktok": {"description": cls.TIKTOK_DESCRIPTION},
        })

    @classmethod
    def resolve_candidate_payload(cls, payload, *, selected_title):
        candidates = cls.validate_candidate_payload(payload)
        return cls.compose(title=selected_title, description=candidates.description, youtube_tags=candidates.youtube_tags)

    @classmethod
    def normalize_metadata(cls, payload):
        # YouTube holds the shared text without platform suffixes. Editing it
        # updates every generated description and restores the fixed tags.
        metadata = cls.validate_payload(payload)
        return cls.compose(title=metadata.youtube.title, description=metadata.youtube.description,
                           youtube_tags=metadata.youtube.tags)
