from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from ..config import settings


@dataclass(frozen=True)
class MusicEntry:
    key: str
    display_name: str
    file_path: str
    volume_db: float


@dataclass(frozen=True)
class MusicConfig:
    default_music_key: str | None
    musics: dict[str, MusicEntry]


class MusicConfigService:
    """Loads / validates music configuration from YAML."""

    _lock = Lock()
    _cached: MusicConfig | None = None

    @classmethod
    def _path(cls) -> Path:
        return settings.music_config_path

    @classmethod
    def _parse(cls, raw: dict[str, Any]) -> MusicConfig:
        if not isinstance(raw, dict):
            raise ValueError("Music config root must be a mapping")

        default_key = raw.get("default_music_key")
        if default_key is not None and not isinstance(default_key, str):
            raise ValueError("default_music_key must be a string or null")

        musics_raw = raw.get("musics") or raw.get("music")
        if musics_raw is None:
            musics_raw = {}
        if not isinstance(musics_raw, dict):
            raise ValueError("musics must be a mapping")

        musics: dict[str, MusicEntry] = {}
        for key, value in musics_raw.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Music keys must be non-empty strings")
            if not isinstance(value, dict):
                raise ValueError(f"Music entry '{key}' must be a mapping")

            display_name = value.get("display_name")
            file_path = value.get("file_path")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError(f"Music '{key}' is missing display_name")
            if not isinstance(file_path, str) or not file_path.strip():
                raise ValueError(f"Music '{key}' is missing file_path")

            volume_db_raw = value.get("volume_db", -12)
            if not isinstance(volume_db_raw, (int, float)):
                raise ValueError(f"Music '{key}' volume_db must be numeric")
            volume_db = float(volume_db_raw)
            if volume_db < -30 or volume_db > 0:
                raise ValueError(f"Music '{key}' volume_db must be between -30 and 0")

            normalized_key = key.strip()
            musics[normalized_key] = MusicEntry(
                key=normalized_key,
                display_name=display_name.strip(),
                file_path=file_path.strip(),
                volume_db=volume_db,
            )

        if default_key is not None:
            default_key = default_key.strip()
            if default_key and default_key not in musics:
                raise ValueError(
                    f"default_music_key '{default_key}' does not exist in musics entries"
                )
            if not default_key:
                default_key = None

        return MusicConfig(default_music_key=default_key, musics=musics)

    @classmethod
    def _load_from_disk(cls) -> MusicConfig:
        path = cls._path()
        if not path.exists():
            return MusicConfig(default_music_key=None, musics={})
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse music config YAML: {exc}") from exc
        if raw is None:
            return MusicConfig(default_music_key=None, musics={})
        return cls._parse(raw)

    @classmethod
    def get_config(cls, *, force_reload: bool = False) -> MusicConfig:
        with cls._lock:
            if force_reload or cls._cached is None:
                cls._cached = cls._load_from_disk()
            return cls._cached

    @classmethod
    def list_musics(cls) -> list[MusicEntry]:
        config = cls.get_config()
        return list(config.musics.values())

    @classmethod
    def get_music(cls, music_key: str) -> MusicEntry:
        config = cls.get_config()
        key = music_key.strip()
        music = config.musics.get(key)
        if music is None:
            raise ValueError(f"Unknown music key '{music_key}'")
        return music
