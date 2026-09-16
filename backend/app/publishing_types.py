"""Account publishing categories, independent of source-library storage."""
from enum import Enum
from .library_types import LibraryType

PublishingType = Enum("PublishingType", {**{v.name: v.value for v in LibraryType}, "FACECAM": "facecam"}, type=str)


def coerce_publishing_type(value):
    return PublishingType(str(value.value if isinstance(value, Enum) else value).strip().lower())
