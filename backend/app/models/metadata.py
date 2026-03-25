from pydantic import BaseModel, ConfigDict, field_validator


METADATA_TITLE_CANDIDATE_COUNT = 10
METADATA_TITLE_MAX_CHARS = 62
TIKTOK_FIXED_HASHTAGS = ["#Anime", "#animerecommendations"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FacebookMetadata(_StrictModel):
    title: str
    description: str
    tags: list[str]

    @field_validator("title", "description")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Field cannot be empty")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("At least one tag is required")
        cleaned = [tag.strip() for tag in value if isinstance(tag, str) and tag.strip()]
        if len(cleaned) != len(value):
            raise ValueError("Tags must be non-empty strings")
        return cleaned


class InstagramMetadata(_StrictModel):
    caption: str

    @field_validator("caption")
    @classmethod
    def validate_caption(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Caption cannot be empty")
        return value.strip()


class YouTubeMetadata(_StrictModel):
    title: str
    description: str
    tags: list[str]

    @field_validator("title", "description")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Field cannot be empty")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("At least one tag is required")
        cleaned = [tag.strip() for tag in value if isinstance(tag, str) and tag.strip()]
        if len(cleaned) != len(value):
            raise ValueError("Tags must be non-empty strings")
        return cleaned


class TikTokMetadata(_StrictModel):
    description: str

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Description cannot be empty")
        return value.strip()


class VideoMetadataPayload(_StrictModel):
    facebook: FacebookMetadata
    instagram: InstagramMetadata
    youtube: YouTubeMetadata
    tiktok: TikTokMetadata


def _validate_non_empty_strings(value: list[str], label: str) -> list[str]:
    if not value:
        raise ValueError(f"{label} must contain at least one value")

    cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(cleaned) != len(value):
        raise ValueError(f"{label} entries must be non-empty strings")
    return cleaned


def _normalize_hashtags(value: list[str], label: str) -> list[str]:
    cleaned = _validate_non_empty_strings(value, label)
    normalized: list[str] = []
    for item in cleaned:
        compact = item.replace(" ", "")
        hashtag = compact if compact.startswith("#") else f"#{compact}"
        if len(hashtag) <= 1:
            raise ValueError(f"{label} entries must contain hashtag content")
        normalized.append(hashtag)
    return normalized


class MetadataCandidateFacebook(_StrictModel):
    description: str
    tags: list[str]

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Description cannot be empty")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return _validate_non_empty_strings(value, "facebook.tags")


class MetadataCandidateInstagram(_StrictModel):
    hashtags: list[str]

    @field_validator("hashtags")
    @classmethod
    def validate_hashtags(cls, value: list[str]) -> list[str]:
        return _normalize_hashtags(value, "instagram.hashtags")


class MetadataCandidateYouTube(_StrictModel):
    description: str
    tags: list[str]

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Description cannot be empty")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return _validate_non_empty_strings(value, "youtube.tags")


class MetadataTitleCandidatesPayload(_StrictModel):
    title_candidates: list[str]
    facebook: MetadataCandidateFacebook
    instagram: MetadataCandidateInstagram
    youtube: MetadataCandidateYouTube

    @field_validator("title_candidates")
    @classmethod
    def validate_title_candidates(cls, value: list[str]) -> list[str]:
        cleaned = _validate_non_empty_strings(value, "title_candidates")
        if len(cleaned) != METADATA_TITLE_CANDIDATE_COUNT:
            raise ValueError(
                "title_candidates must contain exactly "
                f"{METADATA_TITLE_CANDIDATE_COUNT} titles"
            )
        for title in cleaned:
            if len(title) > METADATA_TITLE_MAX_CHARS:
                raise ValueError(
                    "title_candidates entries must be "
                    f"<= {METADATA_TITLE_MAX_CHARS} characters"
                )
        return cleaned
