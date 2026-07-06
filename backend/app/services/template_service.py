"""Loads and caches the JSX template catalog."""
from __future__ import annotations

from pathlib import Path
from threading import Lock

import yaml
from pydantic import ValidationError

from ..config import settings
from ..models.template import Template, TemplatesConfig


class TemplateService:
    """Thread-safe loader for config/templates/config.yaml."""

    _lock = Lock()
    _cached: TemplatesConfig | None = None

    @classmethod
    def _path(cls) -> Path:
        return settings.templates_config_path

    @classmethod
    def _load_from_disk(cls) -> TemplatesConfig:
        path = cls._path()
        if not path.exists():
            raise ValueError(f"Templates config file not found: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse templates config YAML: {exc}") from exc
        if raw is None:
            raise ValueError(f"Templates config file is empty: {path}")
        try:
            config = TemplatesConfig.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid templates config: {exc}") from exc

        from .llm_config_service import LLMConfigService
        from .music_config_service import MusicConfigService
        from .voice_config_service import VoiceConfigService

        voice_config = (
            VoiceConfigService.get_config(force_reload=True)
            if any(template.voice_key for template in config.templates.values())
            else None
        )
        music_config = (
            MusicConfigService.get_config(force_reload=True)
            if any(template.music_key for template in config.templates.values())
            else None
        )
        llm_config = (
            LLMConfigService.get_config(force_reload=True)
            if any(template.llm_preset for template in config.templates.values())
            else None
        )
        for key, template in config.templates.items():
            if template.voice_key and template.voice_key not in voice_config.voices:
                raise ValueError(
                    f"Invalid template '{key}' voice_key: {template.voice_key}"
                )
            if template.music_key and template.music_key not in music_config.musics:
                raise ValueError(
                    f"Invalid template '{key}' music_key: {template.music_key}"
                )
            if template.llm_preset and template.llm_preset not in llm_config.presets:
                raise ValueError(
                    f"Invalid template '{key}' llm_preset: {template.llm_preset}"
                )
        return config

    @classmethod
    def get_config(cls, *, force_reload: bool = False) -> TemplatesConfig:
        with cls._lock:
            if force_reload or cls._cached is None:
                cls._cached = cls._load_from_disk()
            return cls._cached

    @classmethod
    def default_key(cls) -> str:
        return cls.get_config().default

    @classmethod
    def get(cls, key: str) -> Template:
        cfg = cls.get_config()
        tpl = cfg.templates.get(key)
        if tpl is None:
            raise ValueError(
                f"Unknown template '{key}'. Available: {sorted(cfg.templates.keys())}"
            )
        return tpl

    @classmethod
    def list_templates(cls) -> list[tuple[str, Template]]:
        cfg = cls.get_config()
        return list(cfg.templates.items())
