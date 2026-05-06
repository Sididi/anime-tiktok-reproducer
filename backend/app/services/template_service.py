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
            return TemplatesConfig.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid templates config: {exc}") from exc

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
