from pydantic import BaseModel, ConfigDict, field_validator


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
