"""Loads and caches the LLM preset catalog."""
from __future__ import annotations

from pathlib import Path
from threading import Lock

import yaml
from pydantic import ValidationError

from ..config import settings
from ..models.llm_config import LLMConfig, LLMPreset, LLMPresetEntry


class LLMConfigService:
    """Thread-safe loader for config/llm/config.yaml."""

    _lock = Lock()
    _cached: LLMConfig | None = None

    @classmethod
    def _path(cls) -> Path:
        return settings.llm_config_path

    @classmethod
    def _load_from_disk(cls) -> LLMConfig:
        path = cls._path()
        if not path.exists():
            raise ValueError(f"LLM config file not found: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse LLM config YAML: {exc}") from exc
        if raw is None:
            raise ValueError(f"LLM config file is empty: {path}")
        try:
            return LLMConfig.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid LLM config: {exc}") from exc

    @classmethod
    def get_config(cls, *, force_reload: bool = False) -> LLMConfig:
        with cls._lock:
            if force_reload or cls._cached is None:
                cls._cached = cls._load_from_disk()
            return cls._cached

    @classmethod
    def default_preset_key(cls) -> str:
        return cls.get_config().default

    @classmethod
    def get_preset(cls, key: str) -> LLMPreset:
        cfg = cls.get_config()
        preset = cfg.presets.get(key)
        if preset is None:
            raise ValueError(
                f"Unknown LLM preset '{key}'. Available: {sorted(cfg.presets.keys())}"
            )
        return preset

    @classmethod
    def translation_entry(cls) -> LLMPresetEntry:
        """Model used for subtitle translation; falls back to the default preset's light tier."""
        cfg = cls.get_config()
        if cfg.translation is not None:
            return cfg.translation
        return cfg.presets[cfg.default].light

    @classmethod
    def list_presets(cls) -> list[tuple[str, LLMPreset]]:
        cfg = cls.get_config()
        return list(cfg.presets.items())
