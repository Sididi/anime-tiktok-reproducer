# /script Phase Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate LLM dispatch to OpenRouter, introduce per-project LLM preset / template / min-playback-speed knobs in the /script phase, and add a templates system (classic + minimal) that captures all the asset/visual choices currently hardcoded.

**Architecture:** Configuration-driven design. New `config/llm/config.yaml` declares LLM presets (each bundling big + light models with reasoning configs). New `config/templates/config.yaml` declares templates (foreground/background prfpsets, subtitle mogrts, white border toggle, overlay style + enable). Per-project overrides (`llm_preset`, `template`, `min_playback_speed`) live on the existing `Project` model and fall through to config defaults when unset. `OpenRouterService` replaces `claude_service.py` / `gemini_service.py` entirely behind the unchanged `LLMService` facade contract (with a new `tier="big"|"light"` argument).

**Tech Stack:** Python 3 / FastAPI / Pydantic v2 / `openai` SDK pointed at OpenRouter / PIL / React + TypeScript / pytest

**Spec:** [docs/superpowers/specs/2026-05-06-script-phase-upgrade-design.md](../specs/2026-05-06-script-phase-upgrade-design.md)

---

## File Structure

**New files:**
- `backend/app/models/llm_config.py` — Pydantic models for LLM presets
- `backend/app/models/template.py` — Pydantic models for templates
- `backend/app/services/llm_config_service.py` — load/cache `config/llm/config.yaml`
- `backend/app/services/template_service.py` — load/cache `config/templates/config.yaml`
- `backend/app/services/openrouter_service.py` — single LLM provider
- `config/llm/config.yaml` + `config.example.yaml`
- `config/templates/config.yaml` + `config.example.yaml`
- `backend/tests/test_llm_config_service.py`
- `backend/tests/test_template_service.py`
- `backend/tests/test_openrouter_service.py`
- `backend/tests/test_project_resolution.py`
- `frontend/src/components/script/ProjectSettingsPanel.tsx`

**Deleted files:**
- `backend/app/services/claude_service.py`
- `backend/app/services/gemini_service.py`

**Modified files (with primary responsibility):**
- `backend/app/config.py` — remove old LLM env vars; add `openrouter_*`, paths
- `backend/app/services/llm_service.py` — facade now delegates to OpenRouter, adds `tier` kwarg
- `backend/app/services/script_automation_service.py` — pass `tier`, skip overlay when disabled
- `backend/app/services/metadata.py` — pass `tier="light"`
- `backend/app/services/processing.py` — read template values, drop `grand_mode_enabled` checks
- `backend/app/services/export_service.py` — read template values for prfpset/mogrt names
- `backend/app/services/title_image_generator.py` — split into renderer registry
- `backend/app/services/otio_timing.py` — accept resolved speed
- `backend/app/services/gap_resolution.py` — accept resolved speed
- `backend/app/api/routes/processing.py` — extend config endpoint, new settings endpoint
- `backend/app/api/routes/gaps.py` — accept resolved speed
- `backend/app/services/upload_phase.py` — add resolved fields to manager rows
- `backend/app/models/project.py` — three new optional fields + resolution helpers
- `backend/app/services/__init__.py` — register new services, drop claude/gemini
- `frontend/src/api/client.ts` — new `updateScriptSettings` shape, types
- `frontend/src/types/index.ts` — extend `ProjectManagerRow` and add settings shapes
- `frontend/src/pages/ScriptRestructurePage.tsx` — render `ProjectSettingsPanel`, gate overlay
- `frontend/src/components/project-manager/ProjectTable.tsx` — three new columns
- `frontend/src/components/project-manager/ProjectRow.tsx` — render three new cells
- `.env.example` — drop legacy LLM vars, add `ATR_OPENROUTER_API_KEY`

---

# Phase 1 — LLM config foundation

## Task 1: LLM config Pydantic models

**Files:**
- Create: `backend/app/models/llm_config.py`
- Test: `backend/tests/test_llm_config_models.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_llm_config_models.py
"""Tests for LLM preset Pydantic models."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.llm_config import (
    AnthropicThinking,
    GeminiThinking,
    LLMPresetEntry,
    LLMPreset,
    LLMConfig,
)


def test_anthropic_thinking_validates_max_tokens_positive():
    AnthropicThinking(max_tokens=4000)
    with pytest.raises(ValueError):
        AnthropicThinking(max_tokens=0)


def test_gemini_thinking_validates_effort_enum():
    GeminiThinking(effort="high")
    with pytest.raises(ValueError):
        GeminiThinking(effort="medium-high")


def test_preset_entry_accepts_either_thinking_shape_or_null():
    LLMPresetEntry(openrouter_id="x/y", thinking=AnthropicThinking(max_tokens=6000))
    LLMPresetEntry(openrouter_id="x/y", thinking=GeminiThinking(effort="high"))
    LLMPresetEntry(openrouter_id="x/y", thinking=None)


def test_llm_config_default_must_exist_in_presets():
    presets = {
        "claude": LLMPreset(
            label="Claude",
            big=LLMPresetEntry(openrouter_id="anthropic/x", thinking=None),
            light=LLMPresetEntry(openrouter_id="anthropic/y", thinking=None),
        )
    }
    LLMConfig(default="claude", presets=presets)
    with pytest.raises(ValueError):
        LLMConfig(default="missing", presets=presets)
```

- [ ] **Step 2: Run test (expect failure on imports)**

```bash
cd backend && uv run pytest tests/test_llm_config_models.py -v
```
Expected: ImportError (module does not exist).

- [ ] **Step 3: Write the model file**

```python
# backend/app/models/llm_config.py
"""Pydantic models for the LLM preset catalog (config/llm/config.yaml)."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


class AnthropicThinking(BaseModel):
    """Reasoning shape for Anthropic models — budget in tokens."""

    max_tokens: int = Field(..., gt=0, le=64000)
    model_config = {"extra": "forbid"}


class GeminiThinking(BaseModel):
    """Reasoning shape for Gemini models — effort level."""

    effort: Literal["low", "medium", "high", "xhigh"]
    model_config = {"extra": "forbid"}


ThinkingConfig = Annotated[
    Union[AnthropicThinking, GeminiThinking],
    Field(discriminator=None),
]


class LLMPresetEntry(BaseModel):
    """One model tier inside a preset (big or light)."""

    openrouter_id: str = Field(..., min_length=1)
    thinking: ThinkingConfig | None = None
    model_config = {"extra": "forbid"}


class LLMPreset(BaseModel):
    """A preset bundles a big model + a light model under a label."""

    label: str = Field(..., min_length=1)
    big: LLMPresetEntry
    light: LLMPresetEntry
    model_config = {"extra": "forbid"}


class LLMConfig(BaseModel):
    """Root of config/llm/config.yaml."""

    default: str = Field(..., min_length=1)
    presets: dict[str, LLMPreset]
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _default_must_exist(self) -> "LLMConfig":
        if self.default not in self.presets:
            raise ValueError(
                f"default preset '{self.default}' is not in presets keys: "
                f"{sorted(self.presets.keys())}"
            )
        return self
```

- [ ] **Step 4: Run test, verify pass**

```bash
cd backend && uv run pytest tests/test_llm_config_models.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/llm_config.py backend/tests/test_llm_config_models.py
git commit -m "feat: add LLM preset Pydantic models for OpenRouter migration"
```

---

## Task 2: Settings — add OpenRouter + config paths

**Files:**
- Modify: `backend/app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add new settings fields**

Edit `backend/app/config.py` — inside the `Settings` class, add after the existing Anthropic block (around line 92):

```python
    # OpenRouter (replaces per-provider keys)
    openrouter_api_key: str | None = None
    openrouter_timeout: int = 600  # seconds; generous for thinking models
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Config paths for new feature configs
    llm_config_path: Path = PROJECT_ROOT / "config" / "llm" / "config.yaml"
    templates_config_path: Path = PROJECT_ROOT / "config" / "templates" / "config.yaml"
```

Do not delete the old fields yet — Task 8 handles the removal once everything is wired.

- [ ] **Step 2: Update `.env.example`**

Add at the top of the LLM section (search for `ATR_GEMINI_API_KEY=`):

```bash
# OpenRouter (replaces ATR_GEMINI_*, ATR_ANTHROPIC_*, ATR_LLM_PROVIDER)
ATR_OPENROUTER_API_KEY=
ATR_OPENROUTER_TIMEOUT=600
```

- [ ] **Step 3: Verify Settings still loads**

```bash
cd backend && uv run python -c "from app.config import settings; print(settings.openrouter_base_url, settings.llm_config_path)"
```
Expected: prints the URL and a path ending in `config/llm/config.yaml`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/config.py .env.example
git commit -m "feat: add OpenRouter settings and config-file paths"
```

---

## Task 3: LLMConfigService — load and cache the YAML

**Files:**
- Create: `backend/app/services/llm_config_service.py`
- Test: `backend/tests/test_llm_config_service.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_llm_config_service.py
"""Tests for LLMConfigService YAML loading."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm_config_service import LLMConfigService


VALID_YAML = """\
default: claude
presets:
  claude:
    label: "Claude"
    big:
      openrouter_id: anthropic/claude-opus-4.7
      thinking:
        max_tokens: 6000
    light:
      openrouter_id: anthropic/claude-haiku-4.5
      thinking: null
  gemini:
    label: "Gemini"
    big:
      openrouter_id: google/gemini-3-pro-preview
      thinking:
        effort: high
    light:
      openrouter_id: google/gemini-2.5-flash
      thinking: null
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_loads_valid_config(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    cfg = LLMConfigService.get_config(force_reload=True)
    assert cfg.default == "claude"
    assert "claude" in cfg.presets
    assert cfg.presets["claude"].big.openrouter_id == "anthropic/claude-opus-4.7"


def test_default_preset_resolves(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    assert LLMConfigService.default_preset_key() == "claude"
    preset = LLMConfigService.get_preset("gemini")
    assert preset.big.openrouter_id == "google/gemini-3-pro-preview"


def test_unknown_preset_key_raises(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    with pytest.raises(ValueError):
        LLMConfigService.get_preset("nope")


def test_invalid_yaml_raises(tmp_path, monkeypatch):
    path = _write(tmp_path, "default: claude\npresets: not-a-mapping\n")
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    with pytest.raises(ValueError):
        LLMConfigService.get_config(force_reload=True)


def test_default_must_exist_in_presets(tmp_path, monkeypatch):
    body = VALID_YAML.replace("default: claude", "default: missing")
    path = _write(tmp_path, body)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    with pytest.raises(ValueError):
        LLMConfigService.get_config(force_reload=True)
```

- [ ] **Step 2: Run test (expect failures)**

```bash
cd backend && uv run pytest tests/test_llm_config_service.py -v
```

- [ ] **Step 3: Write the service**

```python
# backend/app/services/llm_config_service.py
"""Loads and caches the LLM preset catalog."""
from __future__ import annotations

from pathlib import Path
from threading import Lock

import yaml
from pydantic import ValidationError

from ..config import settings
from ..models.llm_config import LLMConfig, LLMPreset


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
    def list_presets(cls) -> list[tuple[str, LLMPreset]]:
        cfg = cls.get_config()
        return list(cfg.presets.items())
```

- [ ] **Step 4: Run tests, verify pass**

```bash
cd backend && uv run pytest tests/test_llm_config_service.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/llm_config_service.py backend/tests/test_llm_config_service.py
git commit -m "feat: add LLMConfigService for loading preset catalog"
```

---

## Task 4: Initial config/llm/config.yaml

**Files:**
- Create: `config/llm/config.yaml`
- Create: `config/llm/config.example.yaml`

- [ ] **Step 1: Write the production config**

```yaml
# config/llm/config.yaml
default: claude

presets:
  claude:
    label: "Claude (Opus 4.7 + Haiku 4.5)"
    big:
      openrouter_id: anthropic/claude-opus-4.7
      thinking:
        max_tokens: 6000
    light:
      openrouter_id: anthropic/claude-haiku-4.5
      thinking: null

  gemini:
    label: "Gemini (3.1 Pro + 2.5 Flash)"
    big:
      openrouter_id: google/gemini-3-pro-preview
      thinking:
        effort: high
    light:
      openrouter_id: google/gemini-2.5-flash
      thinking: null
```

- [ ] **Step 2: Copy as the example**

```bash
cp config/llm/config.yaml config/llm/config.example.yaml
```

- [ ] **Step 3: Verify the service loads it**

```bash
cd backend && uv run python -c "
from app.services.llm_config_service import LLMConfigService
cfg = LLMConfigService.get_config(force_reload=True)
print('default:', cfg.default)
for key, preset in cfg.presets.items():
    print(f'  {key}: {preset.label} | big={preset.big.openrouter_id} | light={preset.light.openrouter_id}')
"
```
Expected: prints `default: claude` and both preset entries.

- [ ] **Step 4: Commit**

```bash
git add config/llm/config.yaml config/llm/config.example.yaml
git commit -m "feat: add initial LLM preset catalog (claude + gemini)"
```

---

## Task 5: OpenRouterService

**Files:**
- Create: `backend/app/services/openrouter_service.py`
- Test: `backend/tests/test_openrouter_service.py`

- [ ] **Step 1: Verify openai SDK is available**

```bash
cd backend && uv run python -c "import openai; print(openai.__version__)"
```
If missing:
```bash
cd backend && uv pip install openai
```

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_openrouter_service.py
"""Unit tests for OpenRouterService — API calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.llm_config import (
    AnthropicThinking,
    GeminiThinking,
    LLMPresetEntry,
)
from app.services.openrouter_service import OpenRouterService


def _make_chat_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_build_reasoning_anthropic():
    entry = LLMPresetEntry(
        openrouter_id="anthropic/x", thinking=AnthropicThinking(max_tokens=4000)
    )
    out = OpenRouterService._build_reasoning(entry)
    assert out == {"max_tokens": 4000, "exclude": True}


def test_build_reasoning_gemini():
    entry = LLMPresetEntry(
        openrouter_id="google/x", thinking=GeminiThinking(effort="high")
    )
    out = OpenRouterService._build_reasoning(entry)
    assert out == {"effort": "high", "exclude": True}


def test_build_reasoning_none():
    entry = LLMPresetEntry(openrouter_id="x/y", thinking=None)
    assert OpenRouterService._build_reasoning(entry) is None


def test_generate_text_uses_preset_big_by_default(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response("hello")

    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )

    fake_preset = MagicMock()
    fake_preset.big = LLMPresetEntry(
        openrouter_id="anthropic/big", thinking=AnthropicThinking(max_tokens=2000)
    )
    fake_preset.light = LLMPresetEntry(openrouter_id="anthropic/light", thinking=None)
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.get_preset",
        classmethod(lambda cls, key: fake_preset),
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "claude"),
    )

    out = OpenRouterService.generate_text("hi", tier="big")
    assert out == "hello"
    call = fake_client.chat.completions.create.call_args
    assert call.kwargs["model"] == "anthropic/big"
    assert call.kwargs["extra_body"]["reasoning"] == {
        "max_tokens": 2000,
        "exclude": True,
    }


def test_generate_text_light_tier_no_reasoning(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response("ok")

    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )

    fake_preset = MagicMock()
    fake_preset.big = LLMPresetEntry(openrouter_id="x/big", thinking=None)
    fake_preset.light = LLMPresetEntry(openrouter_id="x/light", thinking=None)
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.get_preset",
        classmethod(lambda cls, key: fake_preset),
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "x"),
    )

    OpenRouterService.generate_text("hi", tier="light")
    call = fake_client.chat.completions.create.call_args
    assert call.kwargs["model"] == "x/light"
    assert "reasoning" not in call.kwargs.get("extra_body", {})


def test_generate_json_value_strips_fence(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response(
        "```json\n{\"a\": 1}\n```"
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )
    fake_preset = MagicMock()
    fake_preset.big = LLMPresetEntry(openrouter_id="x/big", thinking=None)
    fake_preset.light = LLMPresetEntry(openrouter_id="x/light", thinking=None)
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.get_preset",
        classmethod(lambda cls, key: fake_preset),
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "x"),
    )
    out = OpenRouterService.generate_json_value("hi")
    assert out == {"a": 1}
```

- [ ] **Step 3: Run test (expect ImportError)**

```bash
cd backend && uv run pytest tests/test_openrouter_service.py -v
```

- [ ] **Step 4: Implement the service**

```python
# backend/app/services/openrouter_service.py
"""Single LLM provider — calls every model through OpenRouter via the
openai-compatible chat completions API."""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

from openai import OpenAI, APITimeoutError

from ..config import settings
from ..models.llm_config import (
    AnthropicThinking,
    GeminiThinking,
    LLMPresetEntry,
)
from .llm_config_service import LLMConfigService


logger = logging.getLogger(__name__)

Tier = Literal["big", "light"]


class OpenRouterService:
    """Wrapper over OpenRouter's OpenAI-compatible API."""

    _client: OpenAI | None = None

    @classmethod
    def _get_client(cls) -> OpenAI:
        if cls._client is not None:
            return cls._client
        api_key = (settings.openrouter_api_key or "").strip()
        if not api_key:
            raise RuntimeError(
                "OpenRouter API key is missing (ATR_OPENROUTER_API_KEY)"
            )
        cls._client = OpenAI(
            api_key=api_key,
            base_url=settings.openrouter_base_url,
            timeout=settings.openrouter_timeout,
        )
        return cls._client

    @classmethod
    def is_configured(cls) -> bool:
        return bool((settings.openrouter_api_key or "").strip())

    @classmethod
    def _resolve_entry(cls, *, preset_key: str | None, tier: Tier) -> LLMPresetEntry:
        key = preset_key or LLMConfigService.default_preset_key()
        preset = LLMConfigService.get_preset(key)
        return preset.big if tier == "big" else preset.light

    @classmethod
    def _build_reasoning(cls, entry: LLMPresetEntry) -> dict[str, Any] | None:
        if entry.thinking is None:
            return None
        if isinstance(entry.thinking, AnthropicThinking):
            return {"max_tokens": entry.thinking.max_tokens, "exclude": True}
        if isinstance(entry.thinking, GeminiThinking):
            return {"effort": entry.thinking.effort, "exclude": True}
        raise RuntimeError(f"Unknown thinking shape: {entry.thinking!r}")

    @classmethod
    def _chat(
        cls,
        prompt: str,
        *,
        entry: LLMPresetEntry,
        system: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        client = cls._get_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": entry.openrouter_id,
            "messages": messages,
        }
        if max_output_tokens:
            kwargs["max_tokens"] = max_output_tokens

        extra_body: dict[str, Any] = {}
        reasoning = cls._build_reasoning(entry)
        if reasoning is not None:
            extra_body["reasoning"] = reasoning
        if extra_body:
            kwargs["extra_body"] = extra_body

        try:
            response = client.chat.completions.create(**kwargs)
        except APITimeoutError as exc:
            raise RuntimeError(
                f"OpenRouter timeout after {settings.openrouter_timeout}s "
                f"(model={entry.openrouter_id})"
            ) from exc

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            raise RuntimeError(
                f"OpenRouter response was empty (model={entry.openrouter_id})"
            )
        return text

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        trimmed = raw.strip()
        if not trimmed.startswith("```"):
            return trimmed
        lines = trimmed.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @classmethod
    def _parse_json_value(cls, raw: str) -> Any:
        stripped = cls._strip_json_fence(raw)
        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(stripped)
            if not stripped[end:].strip():
                return parsed
        except json.JSONDecodeError:
            pass
        for idx, char in enumerate(stripped):
            if char not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[idx:])
                return parsed
            except json.JSONDecodeError:
                continue
        raise RuntimeError("Unable to parse OpenRouter JSON response")

    # --- public API (matches LLMService facade) ---

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
        max_output_tokens: int | None = None,
    ) -> str:
        entry = cls._resolve_entry(preset_key=preset_key, tier=tier)
        return cls._chat(prompt, entry=entry, max_output_tokens=max_output_tokens)

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> Any:
        entry = cls._resolve_entry(preset_key=preset_key, tier=tier)
        raw = cls._chat(
            prompt,
            entry=entry,
            system="You must respond with valid JSON only. No markdown fences, no explanation.",
        )
        return cls._parse_json_value(raw)

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> dict[str, Any]:
        parsed = cls.generate_json_value(prompt, preset_key=preset_key, tier=tier)
        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("OpenRouter JSON response must be a JSON object")

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        if not cls.is_configured():
            return {"status": "skipped", "detail": "OpenRouter API key not configured"}
        try:
            preset_key = LLMConfigService.default_preset_key()
            preset = LLMConfigService.get_preset(preset_key)
            reply = cls.generate_text("Reply with exactly: pong", tier="light")
            return {
                "status": "ok",
                "detail": f"OpenRouter reachable (preset={preset_key})",
                "model": preset.light.openrouter_id,
                "reply": reply[:60],
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
```

- [ ] **Step 5: Run tests, verify pass**

```bash
cd backend && uv run pytest tests/test_openrouter_service.py -v
```
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/openrouter_service.py backend/tests/test_openrouter_service.py
git commit -m "feat: add OpenRouterService with reasoning passthrough and JSON helpers"
```

---

# Phase 2 — LLM cutover

## Task 6: Refactor LLMService facade

**Files:**
- Modify: `backend/app/services/llm_service.py`

- [ ] **Step 1: Replace the file contents**

```python
# backend/app/services/llm_service.py
"""Facade over OpenRouterService. Provides a `tier` parameter so call sites
declare whether they need the heavyweight reasoning model (big) or the
fast/cheap one (light)."""
from __future__ import annotations

import logging
from typing import Any, Literal

from .llm_config_service import LLMConfigService
from .openrouter_service import OpenRouterService


logger = logging.getLogger(__name__)

Tier = Literal["big", "light"]


class LLMService:
    """Single entry point for LLM calls. Delegates to OpenRouter."""

    @classmethod
    def is_configured(cls) -> bool:
        return OpenRouterService.is_configured()

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
        max_output_tokens: int | None = None,
    ) -> str:
        return OpenRouterService.generate_text(
            prompt,
            preset_key=preset_key,
            tier=tier,
            max_output_tokens=max_output_tokens,
        )

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> dict[str, Any]:
        return OpenRouterService.generate_json(
            prompt, preset_key=preset_key, tier=tier
        )

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> Any:
        return OpenRouterService.generate_json_value(
            prompt, preset_key=preset_key, tier=tier
        )

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        return OpenRouterService.check_api_health()

    @classmethod
    def preset_key(cls, *, preset_key: str | None = None) -> str:
        return preset_key or LLMConfigService.default_preset_key()

    @classmethod
    def active_model(cls, *, preset_key: str | None = None) -> str:
        key = cls.preset_key(preset_key=preset_key)
        return LLMConfigService.get_preset(key).big.openrouter_id

    @classmethod
    def active_light_model(cls, *, preset_key: str | None = None) -> str:
        key = cls.preset_key(preset_key=preset_key)
        return LLMConfigService.get_preset(key).light.openrouter_id
```

- [ ] **Step 2: Verify import works**

```bash
cd backend && uv run python -c "from app.services.llm_service import LLMService; print(LLMService.is_configured())"
```
Expected: `False` (no OpenRouter key set in test env) or `True`. No exception.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/llm_service.py
git commit -m "refactor: rewrite LLMService facade to delegate to OpenRouter"
```

---

## Task 7: Update LLM call sites with `tier` and `preset_key`

**Files:**
- Modify: `backend/app/services/script_automation_service.py:793`, `913`, `1066`, `881`, `906`
- Modify: `backend/app/services/metadata.py`
- Modify: `backend/app/api/routes/processing.py:308-315`

- [ ] **Step 1: Update script_automation_service.py — overlay generation (light tier)**

In `script_automation_service.py`, locate the `generate_video_overlay` method (around line 786-800).
Find the existing call:

```python
result = LLMService.generate_json(prompt, enable_thinking=False)
```

Replace with:

```python
result = LLMService.generate_json(prompt, preset_key=preset_key, tier="light")
```

Add `preset_key: str | None = None` to the method's signature (default `None` so callers without context fall through to the default preset).

- [ ] **Step 2: Update script_automation_service.py — script generation (big tier)**

Locate the script generation call (around line 913):

```python
script_json = LLMService.generate_json_value(
    prompt, model=cls._script_model(), enable_thinking=True
)
```

Replace with:

```python
script_json = LLMService.generate_json_value(
    prompt, preset_key=preset_key, tier="big"
)
```

Remove the `_script_model()` helper if unused after this change. Add `preset_key: str | None = None` to the surrounding method signature; thread the project's resolved preset down from the orchestrator entry point (typically `automate(...)`).

- [ ] **Step 3: Update script_automation_service.py — metadata candidates (light tier)**

Locate the metadata call (around line 1066):

```python
result = LLMService.generate_json(prompt, ...)
```

Replace any `enable_thinking=...` argument with `tier="light"` and pass `preset_key=preset_key` through.

- [ ] **Step 4: Update metadata.py**

In `metadata.py`, locate the `LLMService.generate_json` call. Add `tier="light"` and accept `preset_key` from the caller.

- [ ] **Step 5: Update routes/processing.py — automation config endpoint**

Locate `get_script_automation_config` (around line 254). Find the LLM info block (around lines 308-315) that uses `provider_name()` / `active_model()` / `active_light_model()`. Replace with the new preset-based reporting. Replace existing block (around lines 308-315):

```python
"llm": {
    "configured": LLMService.is_configured(),
    "provider": LLMService.provider_name(),
    "model": LLMService.active_model(),
    "light_model": LLMService.active_light_model(),
},
```

with:

```python
preset_key = project.llm_preset or LLMConfigService.default_preset_key()
preset = LLMConfigService.get_preset(preset_key)
"llm": {
    "configured": LLMService.is_configured(),
    "preset_key": preset_key,
    "preset_label": preset.label,
    "big_model": preset.big.openrouter_id,
    "light_model": preset.light.openrouter_id,
},
```

(Note: `project.llm_preset` is added in Task 11 — temporarily use `LLMConfigService.default_preset_key()` here, then restore project-level resolution after Task 11 lands. To avoid a temporary bad state, do this task AFTER Task 11.)

- [ ] **Step 6: Re-run any existing tests**

```bash
cd backend && uv run pytest tests/ -v
```
Expected: nothing new fails.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/script_automation_service.py backend/app/services/metadata.py backend/app/api/routes/processing.py
git commit -m "refactor: thread tier and preset_key through LLM call sites"
```

> **Note:** Step 5 is split out — see Task 11 ordering.

---

## Task 8: Delete legacy LLM code and env vars + boot warning

**Files:**
- Delete: `backend/app/services/claude_service.py`
- Delete: `backend/app/services/gemini_service.py`
- Modify: `backend/app/config.py` — remove old fields + add boot warning
- Modify: `backend/app/services/__init__.py` — remove ClaudeService/GeminiService entries
- Modify: `.env.example` — remove old keys

- [ ] **Step 1: Verify no remaining imports**

```bash
cd backend && grep -rn "from .claude_service\|from .gemini_service\|claude_service\|gemini_service\|ClaudeService\|GeminiService" app/ tests/ 2>/dev/null
```
Expected: no results except entries in `__init__.py`. If any remain, fix them first (they should already be done in Task 7).

- [ ] **Step 2: Delete the provider files**

```bash
rm backend/app/services/claude_service.py backend/app/services/gemini_service.py
```

- [ ] **Step 3: Update services `__init__.py`**

In `backend/app/services/__init__.py`, remove the two lines that register `ClaudeService` and `GeminiService` (likely entries in the `_LAZY_EXPORTS` or similar dict).

- [ ] **Step 4: Remove old fields from Settings**

In `backend/app/config.py`, delete:
- `llm_provider`, `gemini_api_key`, `gemini_model`, `gemini_light_model`, `gemini_timeout`
- `anthropic_api_key`, `anthropic_model`, `anthropic_light_model`, `anthropic_timeout`
- `grand_mode_enabled` (absorbed into the `classic` template — see Phase 5)
- The `_normalize_llm_provider` validator

- [ ] **Step 5: Add boot warning for legacy env vars**

After the `settings = Settings()` line in `backend/app/config.py`, add:

```python
import logging as _logging
_legacy_env_keys = (
    "ATR_LLM_PROVIDER",
    "ATR_GEMINI_API_KEY",
    "ATR_GEMINI_MODEL",
    "ATR_GEMINI_LIGHT_MODEL",
    "ATR_GEMINI_TIMEOUT",
    "ATR_ANTHROPIC_API_KEY",
    "ATR_ANTHROPIC_MODEL",
    "ATR_ANTHROPIC_LIGHT_MODEL",
    "ATR_ANTHROPIC_TIMEOUT",
    "ATR_GRAND_MODE_ENABLED",
)
_logger = _logging.getLogger("app.config")
for _key in _legacy_env_keys:
    if _key in os.environ:
        _logger.warning(
            "%s is set but ignored. Configure LLM models via "
            "config/llm/config.yaml and templates via "
            "config/templates/config.yaml. Use ATR_OPENROUTER_API_KEY for "
            "the API key.",
            _key,
        )
```

- [ ] **Step 6: Remove old keys from `.env.example`**

In `.env.example`, delete:
- `ATR_LLM_PROVIDER=...`
- `ATR_GEMINI_API_KEY=`, `ATR_GEMINI_MODEL=`, `ATR_GEMINI_LIGHT_MODEL=`, `ATR_GEMINI_TIMEOUT=`
- `ATR_ANTHROPIC_API_KEY=`, `ATR_ANTHROPIC_MODEL=`, `ATR_ANTHROPIC_LIGHT_MODEL=`, `ATR_ANTHROPIC_TIMEOUT=`
- `ATR_GRAND_MODE_ENABLED=`

- [ ] **Step 7: Verify boot still works**

```bash
cd backend && uv run python -c "from app.main import app; print('OK')" 2>&1 | tail -5
```
Expected: `OK` printed (boot warnings allowed).

- [ ] **Step 8: Run all tests**

```bash
cd backend && uv run pytest tests/ -v
```

- [ ] **Step 9: Commit**

```bash
git add -u backend/app/services backend/app/config.py .env.example
git commit -m "feat: drop legacy LLM provider code and env vars"
```

---

# Phase 3 — Templates foundation

## Task 9: Templates Pydantic models + initial config

**Files:**
- Create: `backend/app/models/template.py`
- Test: `backend/tests/test_template_models.py`
- Create: `config/templates/config.yaml`
- Create: `config/templates/config.example.yaml`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_template_models.py
"""Tests for Template Pydantic models."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.template import (
    BackgroundConfig,
    ForegroundConfig,
    OverlayConfig,
    OverlaySideConfig,
    SubtitlesConfig,
    Template,
    TemplatesConfig,
    WhiteBorderConfig,
)


def _classic() -> Template:
    return Template(
        label="Classic",
        foreground=ForegroundConfig(prfpset="fg.prfpset", zoom=0.76),
        background=BackgroundConfig(prfpset="bg.prfpset"),
        subtitles=SubtitlesConfig(mogrt="s.mogrt", raw_mogrt="r.mogrt"),
        white_border=WhiteBorderConfig(enabled=True, mogrt="border.mogrt"),
        overlay=OverlayConfig(
            enabled=True,
            title=OverlaySideConfig(style="classic", prfpset=None),
            category=OverlaySideConfig(style="classic", prfpset=None),
        ),
    )


def test_template_zoom_must_be_positive():
    with pytest.raises(ValueError):
        ForegroundConfig(prfpset="x", zoom=-0.1)
    with pytest.raises(ValueError):
        ForegroundConfig(prfpset="x", zoom=0)


def test_white_border_disabled_allows_null_mogrt():
    WhiteBorderConfig(enabled=False, mogrt=None)


def test_white_border_enabled_requires_mogrt():
    with pytest.raises(ValueError):
        WhiteBorderConfig(enabled=True, mogrt=None)


def test_overlay_side_style_required():
    with pytest.raises(ValueError):
        OverlaySideConfig(style="", prfpset=None)


def test_templates_config_default_must_exist():
    cfg = TemplatesConfig(default="classic", templates={"classic": _classic()})
    assert cfg.default == "classic"
    with pytest.raises(ValueError):
        TemplatesConfig(default="missing", templates={"classic": _classic()})
```

- [ ] **Step 2: Run test (expect ImportError)**

```bash
cd backend && uv run pytest tests/test_template_models.py -v
```

- [ ] **Step 3: Write the model**

```python
# backend/app/models/template.py
"""Pydantic models for the JSX-template catalog (config/templates/config.yaml)."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class OverlaySideConfig(BaseModel):
    """One side of the overlay (title or category)."""

    style: str = Field(..., min_length=1)
    prfpset: str | None = None
    model_config = {"extra": "forbid"}


class OverlayConfig(BaseModel):
    enabled: bool
    title: OverlaySideConfig
    category: OverlaySideConfig
    model_config = {"extra": "forbid"}


class WhiteBorderConfig(BaseModel):
    enabled: bool
    mogrt: str | None = None
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _mogrt_required_when_enabled(self) -> "WhiteBorderConfig":
        if self.enabled and not self.mogrt:
            raise ValueError("white_border.mogrt is required when enabled is true")
        return self


class ForegroundConfig(BaseModel):
    prfpset: str = Field(..., min_length=1)
    zoom: float = Field(..., gt=0, le=2.0)
    model_config = {"extra": "forbid"}


class BackgroundConfig(BaseModel):
    prfpset: str = Field(..., min_length=1)
    model_config = {"extra": "forbid"}


class SubtitlesConfig(BaseModel):
    mogrt: str = Field(..., min_length=1)
    raw_mogrt: str = Field(..., min_length=1)
    model_config = {"extra": "forbid"}


class Template(BaseModel):
    label: str = Field(..., min_length=1)
    foreground: ForegroundConfig
    background: BackgroundConfig
    subtitles: SubtitlesConfig
    white_border: WhiteBorderConfig
    overlay: OverlayConfig
    model_config = {"extra": "forbid"}


class TemplatesConfig(BaseModel):
    default: str = Field(..., min_length=1)
    templates: dict[str, Template]
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _default_must_exist(self) -> "TemplatesConfig":
        if self.default not in self.templates:
            raise ValueError(
                f"default template '{self.default}' is not in templates keys: "
                f"{sorted(self.templates.keys())}"
            )
        return self
```

- [ ] **Step 4: Run tests, verify pass**

```bash
cd backend && uv run pytest tests/test_template_models.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Write `config/templates/config.yaml`**

```yaml
# config/templates/config.yaml
default: classic

templates:
  classic:
    label: "Classic (white panel)"
    foreground:
      prfpset: "SPM Anime Foreground.prfpset"
      zoom: 0.76
    background:
      prfpset: "SPM Anime Background.prfpset"
    subtitles:
      mogrt: "SPM_Anime_Subtitle.mogrt"
      raw_mogrt: "SPM_Anime_Subtitle_Raw.mogrt"
    white_border:
      enabled: true
      mogrt: "White border 10px.mogrt"
    overlay:
      enabled: true
      title:
        style: "classic"
        prfpset: "SPM Anime Category Title.prfpset"
      category:
        style: "classic"
        prfpset: null

  minimal:
    label: "Minimal (no panel)"
    foreground:
      prfpset: "SPM Anime Foreground.prfpset"
      zoom: 0.76
    background:
      prfpset: "SPM Anime Background.prfpset"
    subtitles:
      mogrt: "SPM_Anime_Subtitle.mogrt"
      raw_mogrt: "SPM_Anime_Subtitle_Raw.mogrt"
    white_border:
      enabled: true
      mogrt: "White border 10px.mogrt"
    overlay:
      enabled: true
      title:
        style: "minimal"
        prfpset: null
      category:
        style: "minimal"
        prfpset: null
```

- [ ] **Step 6: Copy as the example**

```bash
cp config/templates/config.yaml config/templates/config.example.yaml
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/template.py backend/tests/test_template_models.py config/templates/config.yaml config/templates/config.example.yaml
git commit -m "feat: add Template Pydantic models and initial classic + minimal templates"
```

---

## Task 10: TemplateService

**Files:**
- Create: `backend/app/services/template_service.py`
- Test: `backend/tests/test_template_service.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_template_service.py
"""Tests for TemplateService."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.template_service import TemplateService


VALID = """\
default: classic
templates:
  classic:
    label: "Classic"
    foreground: { prfpset: fg.prfpset, zoom: 0.76 }
    background: { prfpset: bg.prfpset }
    subtitles: { mogrt: s.mogrt, raw_mogrt: r.mogrt }
    white_border: { enabled: true, mogrt: border.mogrt }
    overlay:
      enabled: true
      title: { style: classic, prfpset: null }
      category: { style: classic, prfpset: null }
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_loads_valid_config(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    cfg = TemplateService.get_config(force_reload=True)
    assert cfg.default == "classic"
    assert cfg.templates["classic"].foreground.zoom == 0.76


def test_get_template_returns_known_key(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    TemplateService.get_config(force_reload=True)
    tpl = TemplateService.get("classic")
    assert tpl.label == "Classic"


def test_unknown_template_raises(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    TemplateService.get_config(force_reload=True)
    with pytest.raises(ValueError):
        TemplateService.get("nope")


def test_default_template_resolves(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    TemplateService.get_config(force_reload=True)
    assert TemplateService.default_key() == "classic"
```

- [ ] **Step 2: Run test (expect ImportError)**

```bash
cd backend && uv run pytest tests/test_template_service.py -v
```

- [ ] **Step 3: Write the service**

```python
# backend/app/services/template_service.py
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
```

- [ ] **Step 4: Run tests, verify pass**

```bash
cd backend && uv run pytest tests/test_template_service.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/template_service.py backend/tests/test_template_service.py
git commit -m "feat: add TemplateService for loading template catalog"
```

---

# Phase 4 — Project model

## Task 11: Project fields + resolution helpers

**Files:**
- Modify: `backend/app/models/project.py`
- Test: `backend/tests/test_project_resolution.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_project_resolution.py
"""Tests for Project resolution helpers (LLM preset, template, speed)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.project import Project


def test_default_fields_are_none():
    p = Project()
    assert p.llm_preset is None
    assert p.template is None
    assert p.min_playback_speed is None


def test_speed_validator_accepts_valid_range():
    Project(min_playback_speed=0.5)
    Project(min_playback_speed=1.0)


def test_speed_validator_rejects_invalid():
    with pytest.raises(ValueError):
        Project(min_playback_speed=0.0)
    with pytest.raises(ValueError):
        Project(min_playback_speed=1.5)
    with pytest.raises(ValueError):
        Project(min_playback_speed=-0.1)


def test_resolved_min_playback_speed_uses_project_value(monkeypatch):
    monkeypatch.setattr(
        "app.models.project.settings.min_playback_speed_factor", 0.75
    )
    p = Project(min_playback_speed=0.6)
    assert p.resolved_min_playback_speed() == 0.6


def test_resolved_min_playback_speed_falls_back_to_settings(monkeypatch):
    monkeypatch.setattr(
        "app.models.project.settings.min_playback_speed_factor", 0.75
    )
    p = Project()
    assert p.resolved_min_playback_speed() == 0.75


def test_resolved_llm_preset_uses_project_value(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_config_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "claude"),
    )
    p = Project(llm_preset="gemini")
    assert p.resolved_llm_preset_key() == "gemini"


def test_resolved_llm_preset_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_config_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "claude"),
    )
    p = Project()
    assert p.resolved_llm_preset_key() == "claude"


def test_resolved_template_key_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "app.services.template_service.TemplateService.default_key",
        classmethod(lambda cls: "classic"),
    )
    p = Project()
    assert p.resolved_template_key() == "classic"
```

- [ ] **Step 2: Run test (expect failures — fields missing)**

```bash
cd backend && uv run pytest tests/test_project_resolution.py -v
```

- [ ] **Step 3: Update `backend/app/models/project.py`**

Add at the top of the file (with other imports):

```python
from pydantic import field_validator

from ..config import settings
```

Add three new fields to the `Project` class (after `voice_key`):

```python
    llm_preset: str | None = None
    template: str | None = None
    min_playback_speed: float | None = None
```

Add the validator and resolution helpers as class methods:

```python
    @field_validator("min_playback_speed")
    @classmethod
    def _validate_min_playback_speed(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if value <= 0.10 or value > 1.0:
            raise ValueError(
                "min_playback_speed must be greater than 0.10 and at most 1.0"
            )
        return value

    def resolved_min_playback_speed(self) -> float:
        if self.min_playback_speed is not None:
            return self.min_playback_speed
        return settings.min_playback_speed_factor

    def resolved_llm_preset_key(self) -> str:
        from ..services.llm_config_service import LLMConfigService
        return self.llm_preset or LLMConfigService.default_preset_key()

    def resolved_template_key(self) -> str:
        from ..services.template_service import TemplateService
        return self.template or TemplateService.default_key()
```

(Local imports inside the methods avoid a circular import between `models.project` and the services.)

- [ ] **Step 4: Run tests, verify pass**

```bash
cd backend && uv run pytest tests/test_project_resolution.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/project.py backend/tests/test_project_resolution.py
git commit -m "feat: add llm_preset, template, min_playback_speed to Project with resolution helpers"
```

---

## Task 7 (resumed): finish updating routes/processing.py LLM block

After Task 11 lands, return to the deferred Step 5 from Task 7:

- [ ] **Step 1: Update routes/processing.py**

Open `backend/app/api/routes/processing.py`. Locate the `get_script_automation_config` endpoint (around line 254). Find the LLM info block (lines 308-315):

```python
"llm": {
    "configured": LLMService.is_configured(),
    "provider": LLMService.provider_name(),
    "model": LLMService.active_model(),
    "light_model": LLMService.active_light_model(),
},
```

Replace with:

```python
preset_key = project.resolved_llm_preset_key()
preset = LLMConfigService.get_preset(preset_key)
"llm": {
    "configured": LLMService.is_configured(),
    "preset_key": preset_key,
    "preset_label": preset.label,
    "big_model": preset.big.openrouter_id,
    "light_model": preset.light.openrouter_id,
},
```

Add at the top of the file:

```python
from ..services.llm_config_service import LLMConfigService
```

- [ ] **Step 2: Run all backend tests**

```bash
cd backend && uv run pytest tests/ -v
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/routes/processing.py
git commit -m "refactor: surface project-resolved LLM preset in /script/automation/config"
```

---

# Phase 5 — Title image renderer registry

## Task 12: Refactor title_image_generator into renderer registry

**Files:**
- Modify: `backend/app/services/title_image_generator.py`
- Test: `backend/tests/test_title_image_generator_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_title_image_generator_registry.py
"""Verify the renderer registry dispatches to the right function."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.title_image_generator import (
    CATEGORY_RENDERERS,
    TITLE_RENDERERS,
    TitleImageGeneratorService,
)


def test_classic_renderers_registered():
    assert "classic" in TITLE_RENDERERS
    assert "classic" in CATEGORY_RENDERERS


def test_minimal_renderers_registered():
    assert "minimal" in TITLE_RENDERERS
    assert "minimal" in CATEGORY_RENDERERS


def test_generate_writes_files_for_classic(tmp_path):
    out = TitleImageGeneratorService.generate(
        title="HELLO",
        category="ACTION",
        output_dir=tmp_path,
        title_style="classic",
        category_style="classic",
    )
    assert out["title"].exists()
    assert out["category"].exists()


def test_generate_unknown_style_raises(tmp_path):
    with pytest.raises(ValueError):
        TitleImageGeneratorService.generate(
            title="HELLO",
            category="ACTION",
            output_dir=tmp_path,
            title_style="bogus",
            category_style="classic",
        )
```

- [ ] **Step 2: Run test (expect failures)**

```bash
cd backend && uv run pytest tests/test_title_image_generator_registry.py -v
```

- [ ] **Step 3: Refactor the file**

Open `backend/app/services/title_image_generator.py`. Rename the existing `_render_title` and `_render_category` (currently methods on the class) to module-level functions named `_render_title_classic` and `_render_category_classic`. Move the layout constants from module scope into a `_CLASSIC_TITLE_CONFIG` and `_CLASSIC_CATEGORY_CONFIG` dataclass for grouping. Then add the new minimal stubs and the registry.

Concretely, after the existing constants, add:

```python
def _render_title_classic(text: str, output_path: Path) -> None:
    """Existing classic title renderer (white rounded panel)."""
    # Move the body of the previous TitleImageGeneratorService._render_title here.
    # All references that used `cls.` become module-level helpers (also moved).
    ...


def _render_category_classic(text: str, output_path: Path) -> None:
    """Existing classic category renderer (white text + black outline)."""
    ...


def _render_title_minimal(text: str, output_path: Path) -> None:
    """Placeholder minimal title renderer — gold cream text, no panel.

    Refine renderer constants once compared against reference screenshot.
    """
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(
        str(TITLE_FONT_PATH), TITLE_FONT_SIZE
    ) if TITLE_FONT_PATH.exists() else ImageFont.load_default()
    color = (242, 213, 138, 255)  # cream gold
    shadow = (0, 0, 0, 180)
    # Center horizontally near same y position as classic
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (WIDTH - w) // 2
    y = CENTER_FRAME_TOP - h - TITLE_GAP_ABOVE_CENTER
    # Drop shadow
    draw.text((x + 3, y + 3), text, fill=shadow, font=font)
    # Main text
    draw.text((x, y), text, fill=color, font=font)
    img.save(output_path)


def _render_category_minimal(text: str, output_path: Path) -> None:
    """Placeholder minimal category renderer — semi-transparent gold.

    Refine renderer constants once compared against reference screenshot.
    """
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(
        str(CAT_FONT_PATH), CAT_FONT_SIZE
    ) if CAT_FONT_PATH.exists() else ImageFont.load_default()
    color = (242, 213, 138, 200)  # cream gold ~78% opacity
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    x = (WIDTH - w) // 2
    y = CENTER_FRAME_BOT + CAT_GAP_BELOW_CENTER
    draw.text((x, y), text, fill=color, font=font)
    img.save(output_path)


TITLE_RENDERERS: dict[str, callable] = {
    "classic": _render_title_classic,
    "minimal": _render_title_minimal,
}

CATEGORY_RENDERERS: dict[str, callable] = {
    "classic": _render_category_classic,
    "minimal": _render_category_minimal,
}
```

Then change `TitleImageGeneratorService.generate` to:

```python
class TitleImageGeneratorService:
    @classmethod
    def generate(
        cls,
        title: str,
        category: str,
        output_dir: Path,
        *,
        title_style: str = "classic",
        category_style: str = "classic",
    ) -> dict[str, Path]:
        title_render = TITLE_RENDERERS.get(title_style)
        category_render = CATEGORY_RENDERERS.get(category_style)
        if title_render is None:
            raise ValueError(
                f"Unknown title style '{title_style}'. "
                f"Available: {sorted(TITLE_RENDERERS.keys())}"
            )
        if category_render is None:
            raise ValueError(
                f"Unknown category style '{category_style}'. "
                f"Available: {sorted(CATEGORY_RENDERERS.keys())}"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        title_path = output_dir / "title_overlay.png"
        category_path = output_dir / "category_overlay.png"
        title_render(title, title_path)
        category_render(category, category_path)
        return {"title": title_path, "category": category_path}
```

Keep the existing helpers (`_load_font`, `_split_emoji_segments`, `_is_emoji`, etc.) intact — they're used by the classic renderer. Existing callers passing only positional args continue to work because the new style kwargs default to `"classic"`.

- [ ] **Step 4: Run tests, verify pass**

```bash
cd backend && uv run pytest tests/test_title_image_generator_registry.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/title_image_generator.py backend/tests/test_title_image_generator_registry.py
git commit -m "feat: split title/category renderers into a style registry with minimal stubs"
```

---

# Phase 6 — Templates wiring

## Task 13: Template-driven asset names in export_service.py

**Files:**
- Modify: `backend/app/services/export_service.py:214-221`
- Modify: any callers that need to thread the resolved Template

- [ ] **Step 1: Locate the asset reference block**

Open `backend/app/services/export_service.py`. Find the section around lines 214-221 that currently has:

```python
border_mogrt = (
    "White border 10px.mogrt" if settings.grand_mode_enabled else "White border 5px.mogrt"
)
# ...
"SPM Anime Background.prfpset"
"SPM Anime Foreground.prfpset"
"SPM Anime Category Title.prfpset"
```

- [ ] **Step 2: Find the function this block is inside**

```bash
grep -n "def " backend/app/services/export_service.py | head -30
```
Identify the surrounding function (likely something like `build_assets_payload` or similar). Note its signature.

- [ ] **Step 3: Thread `template: Template` through that function**

Add `template: Template` (imported from `..models.template`) as a parameter. Update each caller to pass `project.resolved_template_key()` -> `TemplateService.get(...)`. Inside, replace the literals:

```python
border_mogrt = template.white_border.mogrt or "White border 5px.mogrt"
background_prfpset = template.background.prfpset
foreground_prfpset = template.foreground.prfpset
overlay_title_prfpset = template.overlay.title.prfpset  # may be None
overlay_category_prfpset = template.overlay.category.prfpset  # may be None
```

- [ ] **Step 4: Add the imports at the top of the file**

```python
from ..models.template import Template
```

- [ ] **Step 5: Update callers**

```bash
grep -rn "export_service" backend/app/ | grep -v __pycache__
```
For each caller, fetch the project's template:

```python
from .template_service import TemplateService
template = TemplateService.get(project.resolved_template_key())
# ... pass template= where needed
```

- [ ] **Step 6: Run boot smoke test**

```bash
cd backend && uv run python -c "from app.main import app; print('OK')"
```
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/export_service.py
git commit -m "refactor: read asset names from active template in export_service"
```

---

## Task 14: processing.py — drop grand_mode, read from template

**Files:**
- Modify: `backend/app/services/processing.py:1198-1215`, plus the surrounding JSX-template-string section

- [ ] **Step 1: Locate the grand-mode patch block**

Open `backend/app/services/processing.py` around lines 1198-1215. The current block:

```python
if not settings.grand_mode_enabled:
    # Replace "White border 10px.mogrt" -> "White border 5px.mogrt"
    jsx_str = jsx_str.replace(
        "White border 10px.mogrt", "White border 5px.mogrt"
    )
    # Replace V3 scale 76% -> 68% (setScaleOnItem)
    jsx_str = jsx_str.replace(...)
    # Replace V3 scale 76% -> 68% (setScaleAndPosition)
    jsx_str = jsx_str.replace(...)
```

- [ ] **Step 2: Replace the patch block with template-aware substitutions**

The JSX string template uses placeholder tokens. Replace the post-string-replace patches with direct format-time substitutions BEFORE `jsx_str` is finalized. Identify the JSX template string (the large multi-line `jsx = """..."""` block in this file; near the SCENES_JSON / SOURCE_FPS_NUM placeholders — lines ~1230-1276).

In that template, replace hardcoded asset names with placeholder tokens (e.g. `__BORDER_MOGRT__`, `__FG_PRFPSET__`, `__BG_PRFPSET__`, `__FG_ZOOM_PCT__`, `__OVERLAY_TITLE_PRFPSET__`, `__OVERLAY_CATEGORY_PRFPSET__`). Do this with surgical Edit calls (search for the exact strings and replace).

After the template is finalized as a Python string, before `jsx_str = template.format(...)`, build the substitution map from the active `Template`:

```python
from ..services.template_service import TemplateService

template_obj = TemplateService.get(project.resolved_template_key())
subs = {
    "__BORDER_MOGRT__": template_obj.white_border.mogrt or "",
    "__FG_PRFPSET__": template_obj.foreground.prfpset,
    "__BG_PRFPSET__": template_obj.background.prfpset,
    # zoom is a percentage in JSX (76 not 0.76)
    "__FG_ZOOM_PCT__": f"{template_obj.foreground.zoom * 100:.0f}",
    "__OVERLAY_TITLE_PRFPSET__": template_obj.overlay.title.prfpset or "",
    "__OVERLAY_CATEGORY_PRFPSET__": template_obj.overlay.category.prfpset or "",
}
for token, value in subs.items():
    jsx_str = jsx_str.replace(token, value)
```

Delete the entire `if not settings.grand_mode_enabled:` block — it is no longer needed.

- [ ] **Step 3: Verify no remaining `grand_mode_enabled` references**

```bash
grep -rn "grand_mode_enabled" backend/app/
```
Expected: no results.

- [ ] **Step 4: Boot smoke test**

```bash
cd backend && uv run python -c "from app.main import app; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/processing.py
git commit -m "refactor: drive JSX asset names from active template, remove grand mode"
```

---

## Task 15: White border / overlay enable toggles in JSX gen

**Files:**
- Modify: `backend/app/services/processing.py` (V2 white-border track section, overlay placement section)

- [ ] **Step 1: Locate the V2 white-border track in the JSX template**

In the JSX string template, find the block that imports and places the white border on V2. It is identifiable by referencing `__BORDER_MOGRT__` (after Task 14) and inserting clips on track index 2.

Identify the full block — typically a contiguous `// V2: white border` ... `// end V2` region or a dedicated function call in the JSX. Wrap that region with conditional template substitution: replace the static block with a token like `__V2_BORDER_BLOCK__`. In Python, conditionally fill that token:

```python
if template_obj.white_border.enabled:
    v2_block = (
        # the original V2 white-border code, parameterized on __BORDER_MOGRT__ already
        ORIGINAL_V2_BLOCK
    )
else:
    v2_block = ""  # skip V2 entirely

jsx_str = jsx_str.replace("__V2_BORDER_BLOCK__", v2_block)
```

Move the V2 block contents into a Python constant `_V2_BORDER_JSX` at module top so we can swap it cleanly.

- [ ] **Step 2: Locate the overlay (title + category) placement in JSX**

Same approach for the overlay layer. Find where `title_overlay.png` and `category_overlay.png` are placed in JSX (search `title_overlay.png` and `category_overlay.png`). Wrap that region with `__OVERLAY_BLOCK__`:

```python
if template_obj.overlay.enabled and project.video_overlay:
    overlay_block = _OVERLAY_JSX  # the template fragment
else:
    overlay_block = ""

jsx_str = jsx_str.replace("__OVERLAY_BLOCK__", overlay_block)
```

- [ ] **Step 3: Update overlay PNG generation to pass styles**

Find the call site of `TitleImageGeneratorService.generate(...)` in `processing.py` (around line 2965-2990). Update it to pass `title_style` and `category_style` from the active template:

```python
overlay_paths = TitleImageGeneratorService.generate(
    title=overlay["title"],
    category=overlay["category"],
    output_dir=project_overlay_dir,
    title_style=template_obj.overlay.title.style,
    category_style=template_obj.overlay.category.style,
)
```

Wrap the entire `# Step 5: Generate title overlay images` block with `if template_obj.overlay.enabled and project.video_overlay:` so that disabling overlay skips both PNG generation and JSX placement.

- [ ] **Step 4: Boot smoke test**

```bash
cd backend && uv run python -c "from app.main import app; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/processing.py
git commit -m "feat: gate V2 border and overlay layers on template flags"
```

---

## Task 16: ScriptAutomationService — skip overlay LLM when disabled

**Files:**
- Modify: `backend/app/services/script_automation_service.py` — `generate_video_overlay` and the orchestrator that calls it

- [ ] **Step 1: Locate the overlay generation entry point**

In `script_automation_service.py`, find the orchestrator method (typically `automate(...)` or `run(...)`) where `generate_video_overlay(...)` is invoked.

- [ ] **Step 2: Skip the call when overlay is disabled in the active template**

Wrap the invocation:

```python
from .template_service import TemplateService

template_obj = TemplateService.get(project.resolved_template_key())
if template_obj.overlay.enabled:
    overlay_data = cls.generate_video_overlay(
        project=project,
        preset_key=project.resolved_llm_preset_key(),
        # ... existing kwargs
    )
    project.video_overlay = overlay_data
else:
    project.video_overlay = None
```

- [ ] **Step 3: Boot smoke test**

```bash
cd backend && uv run python -c "from app.main import app; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/script_automation_service.py
git commit -m "feat: skip overlay LLM call entirely when template disables overlay"
```

---

# Phase 7 — Speed factor wiring

## Task 17: Replace settings.min_playback_speed_factor reads

**Files:**
- Modify: `backend/app/services/otio_timing.py:222`, `:232`
- Modify: `backend/app/services/gap_resolution.py:288`, `:292`, `:296`
- Modify: `backend/app/api/routes/gaps.py:31`, `:96`, `:109`, `:120`, `:132`

- [ ] **Step 1: Inspect each call site to determine where Project is available**

```bash
grep -n "min_playback_speed_factor\|min_playback_speed_fraction" backend/app/services/otio_timing.py backend/app/services/gap_resolution.py backend/app/api/routes/gaps.py
```

- [ ] **Step 2: Update otio_timing.py**

In `otio_timing.py:222-232`, the `min_speed` parameter already has a fallback default to `settings.min_playback_speed_fraction`. Add an optional `project_min_speed_factor: float | None = None` argument and prefer it over the global default:

```python
@classmethod
def _resolve_min_speed_fraction(
    cls, *, project_min_speed_factor: float | None = None
) -> Fraction:
    if project_min_speed_factor is not None:
        return Fraction(str(project_min_speed_factor)).limit_denominator(100000)
    return settings.min_playback_speed_fraction
```

Update the constructor (around line 232) to accept the same optional argument and forward it.

- [ ] **Step 3: Update gap_resolution.py**

Same pattern: add an optional `project_min_speed_factor` argument to the public functions/classes that currently call `settings.min_playback_speed_factor` or `settings.min_playback_speed_fraction` (3 references). Prefer the argument when not None.

- [ ] **Step 4: Update routes/gaps.py**

Each of the 5 references already has access to `project_id` (it's a path parameter). Look up the project and use the resolved value:

```python
project = ProjectService.load(project_id)
if project is None:
    raise HTTPException(404, ...)
min_speed_factor = project.resolved_min_playback_speed()
# ... pass min_speed_factor= to downstream calls
```

- [ ] **Step 5: Run all tests**

```bash
cd backend && uv run pytest tests/ -v
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/otio_timing.py backend/app/services/gap_resolution.py backend/app/api/routes/gaps.py
git commit -m "refactor: thread per-project min_playback_speed through gap resolution"
```

---

## Task 18: Trigger gap recompute when speed changes

**Files:**
- Modify: `backend/app/api/routes/processing.py` — extend the script settings endpoint

This is wired in Task 19 alongside the new POST endpoint. Skip ahead to Task 19.

---

# Phase 8 — Endpoints

## Task 19: Extend /script/automation/config + new POST /script/settings endpoint

**Files:**
- Modify: `backend/app/api/routes/processing.py` — both endpoints

- [ ] **Step 1: Extend the response of `/script/automation/config`**

Locate `get_script_automation_config` (around line 254) in `routes/processing.py`. Add three blocks to the response payload:

```python
from ..services.template_service import TemplateService
from ..services.llm_config_service import LLMConfigService

# ... inside the function:
templates_payload = [
    {
        "key": key,
        "label": tpl.label,
        "overlay_enabled": tpl.overlay.enabled,
    }
    for key, tpl in TemplateService.list_templates()
]
presets_payload = [
    {"key": key, "label": preset.label}
    for key, preset in LLMConfigService.list_presets()
]
current = {
    "llm_preset": project.resolved_llm_preset_key(),
    "template": project.resolved_template_key(),
    "min_playback_speed": project.resolved_min_playback_speed(),
}
defaults = {
    "llm_preset": LLMConfigService.default_preset_key(),
    "template": TemplateService.default_key(),
    "min_playback_speed": settings.min_playback_speed_factor,
}
```

Add to the returned dict:

```python
"templates": templates_payload,
"llm_presets": presets_payload,
"current": current,
"defaults": defaults,
```

- [ ] **Step 2: Add the new POST /script/settings endpoint**

After the existing PATCH endpoint at line 673 (or as a sibling), add:

```python
class ScriptPhaseSettingsRequest(BaseModel):
    llm_preset: str | None = None
    template: str | None = None
    min_playback_speed: float | None = None


class ScriptPhaseSettingsResponse(BaseModel):
    llm_preset: str
    template: str
    min_playback_speed: float
    gaps_recomputing: bool


@router.post("/projects/{project_id}/script/settings", response_model=ScriptPhaseSettingsResponse)
async def update_script_phase_settings(
    project_id: str, request: ScriptPhaseSettingsRequest
) -> ScriptPhaseSettingsResponse:
    project = ProjectService.load(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    speed_changed = False

    if request.llm_preset is not None:
        # Validate preset exists.
        LLMConfigService.get_preset(request.llm_preset)
        project.llm_preset = request.llm_preset

    if request.template is not None:
        TemplateService.get(request.template)
        project.template = request.template

    if request.min_playback_speed is not None:
        prior = project.resolved_min_playback_speed()
        # Reuse Project's validator by assignment (raises ValueError on bad value).
        project.min_playback_speed = request.min_playback_speed
        if abs(prior - request.min_playback_speed) > 1e-9:
            speed_changed = True

    ProjectService.save(project)

    gaps_recomputing = False
    if speed_changed and project.phase >= ProjectPhase.SCRIPT_RESTRUCTURE:
        # Kick the existing gap auto-fill-and-resolve job.
        from ..api.routes.gaps import auto_fill_and_resolve  # late import
        await auto_fill_and_resolve(project_id)
        gaps_recomputing = True

    return ScriptPhaseSettingsResponse(
        llm_preset=project.resolved_llm_preset_key(),
        template=project.resolved_template_key(),
        min_playback_speed=project.resolved_min_playback_speed(),
        gaps_recomputing=gaps_recomputing,
    )
```

Add imports at the top of the file:

```python
from ..services.template_service import TemplateService
from ..services.llm_config_service import LLMConfigService
from ..models.project import ProjectPhase
```

- [ ] **Step 3: Boot smoke test**

```bash
cd backend && uv run python -c "from app.api.routes.processing import router; print('OK')"
```

- [ ] **Step 4: Manual API smoke test**

```bash
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 &
sleep 2
PROJECT=$(ls backend/data/projects | head -1)
curl -s "http://127.0.0.1:8765/projects/$PROJECT/script/automation/config" | python -m json.tool | head -40
kill %1 2>/dev/null
```
Expected: response includes `templates`, `llm_presets`, `current`, `defaults` keys.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/processing.py
git commit -m "feat: extend /script/automation/config and add POST /script/settings"
```

---

# Phase 9 — Frontend

## Task 20: Frontend types + API client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Extend `ProjectManagerRow` in types**

In `frontend/src/types/index.ts` (around line 173-193), add fields:

```ts
export interface ProjectManagerRow {
  // ... existing fields ...
  llm_preset_resolved: string;
  llm_preset_is_default: boolean;
  min_playback_speed_resolved: number;
  min_playback_speed_is_default: boolean;
  template_resolved: string;
  template_is_default: boolean;
}
```

Add new types for the script settings panel:

```ts
export interface ScriptAutomationConfig {
  // ... existing fields ...
  templates: Array<{ key: string; label: string; overlay_enabled: boolean }>;
  llm_presets: Array<{ key: string; label: string }>;
  current: {
    llm_preset: string;
    template: string;
    min_playback_speed: number;
  };
  defaults: {
    llm_preset: string;
    template: string;
    min_playback_speed: number;
  };
}

export interface ScriptPhaseSettingsRequest {
  llm_preset?: string;
  template?: string;
  min_playback_speed?: number;
}

export interface ScriptPhaseSettingsResponse {
  llm_preset: string;
  template: string;
  min_playback_speed: number;
  gaps_recomputing: boolean;
}
```

- [ ] **Step 2: Add API client method**

In `frontend/src/api/client.ts` near the existing `updateScriptSettings` (around line 997), add:

```ts
async updateScriptPhaseSettings(
  projectId: string,
  payload: ScriptPhaseSettingsRequest,
): Promise<ScriptPhaseSettingsResponse> {
  return request<ScriptPhaseSettingsResponse>(
    `/projects/${projectId}/script/settings`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}
```

(The existing PATCH endpoint stays — it handles tts_speed/music/voice. The new POST endpoint handles the three new dials.)

- [ ] **Step 3: Verify TypeScript build**

```bash
cd frontend && npm run typecheck 2>&1 | tail -20
```
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts
git commit -m "feat: frontend types and client for /script settings + manager row fields"
```

---

## Task 21: ProjectSettingsPanel component

**Files:**
- Create: `frontend/src/components/script/ProjectSettingsPanel.tsx`
- Modify: `frontend/src/pages/ScriptRestructurePage.tsx`

- [ ] **Step 1: Create the panel component**

```tsx
// frontend/src/components/script/ProjectSettingsPanel.tsx
import { useState } from "react";
import type {
  ScriptAutomationConfig,
  ScriptPhaseSettingsRequest,
  ScriptPhaseSettingsResponse,
} from "../../types";

interface Props {
  config: ScriptAutomationConfig;
  onChange: (
    payload: ScriptPhaseSettingsRequest,
  ) => Promise<ScriptPhaseSettingsResponse>;
  disabled?: boolean;
}

export function ProjectSettingsPanel({ config, onChange, disabled }: Props) {
  const [llmPreset, setLlmPreset] = useState(config.current.llm_preset);
  const [template, setTemplate] = useState(config.current.template);
  const [speed, setSpeed] = useState(config.current.min_playback_speed);
  const [busy, setBusy] = useState(false);

  const isDefault = (key: string, def: string) => key === def;

  async function commit(payload: ScriptPhaseSettingsRequest) {
    setBusy(true);
    try {
      const result = await onChange(payload);
      setLlmPreset(result.llm_preset);
      setTemplate(result.template);
      setSpeed(result.min_playback_speed);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="project-settings-panel">
      <h3>Project settings</h3>

      <label>
        Template
        <select
          value={template}
          disabled={disabled || busy}
          onChange={(e) => commit({ template: e.target.value })}
        >
          {config.templates.map((t) => (
            <option key={t.key} value={t.key}>
              {t.label}
              {isDefault(t.key, config.defaults.template) ? " (default)" : ""}
            </option>
          ))}
        </select>
      </label>

      <label>
        LLM
        <select
          value={llmPreset}
          disabled={disabled || busy}
          onChange={(e) => commit({ llm_preset: e.target.value })}
        >
          {config.llm_presets.map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
              {isDefault(p.key, config.defaults.llm_preset) ? " (default)" : ""}
            </option>
          ))}
        </select>
      </label>

      <label>
        Min playback speed: {speed.toFixed(2)}
        <input
          type="range"
          min={0.20}
          max={1.00}
          step={0.05}
          value={speed}
          disabled={disabled || busy}
          onChange={(e) => setSpeed(parseFloat(e.target.value))}
          onMouseUp={(e) =>
            commit({
              min_playback_speed: parseFloat(
                (e.target as HTMLInputElement).value,
              ),
            })
          }
          onTouchEnd={(e) =>
            commit({
              min_playback_speed: parseFloat(
                (e.target as HTMLInputElement).value,
              ),
            })
          }
        />
        <button
          type="button"
          disabled={disabled || busy}
          onClick={() =>
            commit({ min_playback_speed: config.defaults.min_playback_speed })
          }
        >
          Reset
        </button>
      </label>
    </div>
  );
}
```

- [ ] **Step 2: Mount the panel in ScriptRestructurePage.tsx**

Open `frontend/src/pages/ScriptRestructurePage.tsx`. Locate the existing settings/config rendering (around line 671 — `api.getScriptAutomationConfig`). The fetched config now includes the new fields. Insert the panel near the top of the editor body:

```tsx
import { ProjectSettingsPanel } from "../components/script/ProjectSettingsPanel";

// ... in the JSX, near the top of the page body:
{automationConfig && (
  <ProjectSettingsPanel
    config={automationConfig}
    onChange={async (payload) => {
      const result = await api.updateScriptPhaseSettings(projectId, payload);
      if (result.gaps_recomputing) {
        // Refresh transcription/scenes after gap recompute.
        await refreshScenes();
      }
      return result;
    }}
  />
)}
```

`refreshScenes` is whatever method already exists to reload the script editor data after a server-side change (search for it in the page).

- [ ] **Step 3: Add basic CSS**

Append to the page's existing CSS file (or `ScriptRestructurePage.css` if present):

```css
.project-settings-panel {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
  padding: 1rem;
  background: var(--surface-2, #f5f5f7);
  border-radius: 8px;
  margin-bottom: 1rem;
}
.project-settings-panel label {
  display: flex;
  flex-direction: column;
  font-size: 0.85rem;
  gap: 0.25rem;
}
```

- [ ] **Step 4: Build and smoke test**

```bash
cd frontend && npm run build 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/script/ProjectSettingsPanel.tsx frontend/src/pages/ScriptRestructurePage.tsx frontend/src/pages/ScriptRestructurePage.css 2>/dev/null
git commit -m "feat: add ProjectSettingsPanel with template/LLM/speed controls"
```

---

## Task 22: Grey out overlay row when template disables overlay

**Files:**
- Modify: `frontend/src/pages/ScriptRestructurePage.tsx`

- [ ] **Step 1: Locate the overlay row**

Find the existing UI block that renders the title/category overlay controls and the "Generate overlay" button. It's reached from the same automation config response. Search for `video_overlay` or `generateOverlay` callsites in `ScriptRestructurePage.tsx`.

- [ ] **Step 2: Compute the active template's overlay-enabled flag**

Near the top of the component:

```tsx
const overlayEnabled = automationConfig?.templates.find(
  (t) => t.key === automationConfig.current.template,
)?.overlay_enabled ?? true;
```

- [ ] **Step 3: Apply disabled state to the overlay UI**

Wrap the overlay block:

```tsx
<div className={overlayEnabled ? "" : "disabled-section"}>
  {/* existing overlay buttons / title/category fields */}
  <button
    disabled={!overlayEnabled || /* existing disabled conditions */}
    onClick={...}
  >
    Generate overlay
  </button>
</div>
```

- [ ] **Step 4: Add CSS for the disabled state**

```css
.disabled-section {
  opacity: 0.4;
  pointer-events: none;
}
```

- [ ] **Step 5: Build smoke test**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/ScriptRestructurePage.tsx frontend/src/pages/ScriptRestructurePage.css 2>/dev/null
git commit -m "feat: grey out overlay UI when active template disables overlay"
```

---

# Phase 10 — Project Manager

## Task 23: Backend — extend list_manager_rows with resolved fields

**Files:**
- Modify: `backend/app/services/upload_phase.py:293-349`

- [ ] **Step 1: Find the row builder**

Open `backend/app/services/upload_phase.py` and locate `list_manager_rows` (lines 252-349) and the inner row dict construction (lines 293-349).

- [ ] **Step 2: Add the six resolved fields per row**

Inside the row dict construction, after the existing field assignments:

```python
from ..services.llm_config_service import LLMConfigService
from ..services.template_service import TemplateService

# ... in the loop building each row:
default_preset = LLMConfigService.default_preset_key()
default_template = TemplateService.default_key()
default_speed = settings.min_playback_speed_factor

row["llm_preset_resolved"] = project.resolved_llm_preset_key()
row["llm_preset_is_default"] = project.llm_preset is None
row["template_resolved"] = project.resolved_template_key()
row["template_is_default"] = project.template is None
row["min_playback_speed_resolved"] = project.resolved_min_playback_speed()
row["min_playback_speed_is_default"] = project.min_playback_speed is None
```

(Compute `default_*` once outside the loop — pull them above the loop body.)

- [ ] **Step 3: Run any existing tests for this module**

```bash
cd backend && uv run pytest tests/ -v -k upload_phase
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/upload_phase.py
git commit -m "feat: include resolved llm/template/speed in project manager rows"
```

---

## Task 24: Frontend — three new columns in ProjectTable

**Files:**
- Modify: `frontend/src/components/project-manager/ProjectTable.tsx:25-34`
- Modify: `frontend/src/components/project-manager/ProjectRow.tsx:95-242`

- [ ] **Step 1: Add columns to ProjectTable**

In `ProjectTable.tsx` find the `COLUMNS` array (lines 25-34). After the `library_type` (Type) column, before `scheduled_at`, insert:

```tsx
{ key: "llm_preset", label: "LLM", className: "narrow" },
{ key: "min_playback_speed", label: "Speed", className: "narrow" },
{ key: "template", label: "Template", className: "narrow" },
```

- [ ] **Step 2: Render the three new cells in ProjectRow**

In `ProjectRow.tsx`, after the existing Type `<td>` and before the scheduled-at `<td>`, add:

```tsx
<td className={`narrow ${row.llm_preset_is_default ? "muted" : ""}`}>
  {row.llm_preset_resolved}
</td>
<td className={`narrow ${row.min_playback_speed_is_default ? "muted" : ""}`}>
  {row.min_playback_speed_resolved.toFixed(2)}
</td>
<td className={`narrow ${row.template_is_default ? "muted" : ""}`}>
  {row.template_resolved}
</td>
```

- [ ] **Step 3: Add CSS**

In the project manager CSS (search for `ProjectTable.css` or the existing styles next to those components), add:

```css
.muted {
  font-style: italic;
  color: var(--text-muted, #888);
}
.narrow {
  white-space: nowrap;
  font-size: 0.85em;
}
```

- [ ] **Step 4: Build smoke test**

```bash
cd frontend && npm run build 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/project-manager/
git commit -m "feat: surface llm/speed/template columns in project manager"
```

---

# Phase 11 — Manual integration smoke test

## Task 25: End-to-end manual verification

- [ ] **Step 1: Boot the backend**

```bash
cd backend && uv run uvicorn app.main:app --reload --port 8000 &
sleep 3
```

- [ ] **Step 2: Boot the frontend**

```bash
cd frontend && npm run dev &
sleep 3
```

- [ ] **Step 3: Verify the /script/automation/config endpoint**

Pick an existing project ID, then:

```bash
PROJECT_ID=$(ls backend/data/projects | head -1)
curl -s "http://127.0.0.1:8000/projects/$PROJECT_ID/script/automation/config" | python -m json.tool
```
Expected fields present: `templates`, `llm_presets`, `current`, `defaults`.

- [ ] **Step 4: Verify Project Manager shows resolved values**

```bash
curl -s "http://127.0.0.1:8000/project-manager/projects" | python -m json.tool | grep -E "llm_preset|min_playback|template"
```
Expected: each row has the six resolved/is_default fields.

- [ ] **Step 5: In the browser**

- Open `http://localhost:5173`, navigate to /script for a project.
- Confirm Project Settings panel appears at top with Template, LLM, Speed.
- Switch template to `minimal` — overlay UI greys out (if you also flip overlay.enabled in the YAML; otherwise this is just a visual switch).
- Switch LLM preset to `gemini` — confirm the LLM info displayed in /script reflects the new preset.
- Drag the speed slider — confirm a "Recomputing gaps…" toast or behavior fires (only if project is past `SCRIPT_RESTRUCTURE` phase).
- Open Project Manager — confirm three new columns appear with values italicized when default.

- [ ] **Step 6: Stop dev servers**

```bash
kill %1 %2 2>/dev/null
```

- [ ] **Step 7: Final commit (no code changes — just trigger CI if applicable)**

If everything passed, no commit needed.

If any issue surfaces, capture and fix in a follow-up commit; do not bypass.

---

## Self-Review

**1. Spec coverage:** Walked each spec section.
- LLM full migration → Tasks 1-8 ✓
- Reasoning passthrough → Task 5 (`_build_reasoning`) ✓
- LLM config file → Tasks 1, 3, 4 ✓
- Settings cleanup → Task 8 ✓
- Templates models + service + YAML → Tasks 9-10 ✓
- Renderer registry + minimal stubs → Task 12 ✓
- Per-project state on `Project` → Task 11 ✓
- Speed factor wiring → Task 17 ✓
- Auto-rerun gap recompute on speed change → Task 19 ✓
- Endpoints (extended config + new settings) → Task 19 ✓
- /script UI panel + overlay greying → Tasks 21-22 ✓
- Project Manager columns → Tasks 23-24 ✓
- JSX template-driven asset names → Tasks 13-15 ✓
- Automate skips overlay LLM when disabled → Task 16 ✓
- Boot warning for legacy env vars → Task 8 ✓
- Migration: optional fields fall through → Task 11 (default `None`) ✓

**2. Placeholder scan:** Re-read each task. Replaced "implement later" / "similar to X" / "handle edge cases" — none present. Each code block is concrete. The minimal renderers in Task 12 are explicitly scoped as placeholders with comments noting "refine once compared against reference screenshot" — that's a documented design decision, not a plan placeholder.

**3. Type consistency:**
- `Tier = Literal["big", "light"]` consistent across LLMService and OpenRouterService.
- `preset_key` named identically in LLMService, OpenRouterService, ScriptAutomationService.
- `resolved_llm_preset_key()` / `resolved_template_key()` / `resolved_min_playback_speed()` consistent across Project, routes/processing.py, upload_phase.py, ScriptAutomationService.
- `TITLE_RENDERERS` / `CATEGORY_RENDERERS` referenced consistently in tests and service.
- Frontend: `ScriptPhaseSettingsRequest` / `ScriptPhaseSettingsResponse` / `ProjectManagerRow` field names match backend exactly.
- `__BORDER_MOGRT__` / `__FG_PRFPSET__` / etc tokens introduced in Task 14, consumed in Tasks 14-15. Not referenced elsewhere — single-source-of-truth ✓.
- One ordering note: Task 7's Step 5 depends on Task 11. Task 7 explicitly says "do this AFTER Task 11" and Task 11 has a "Task 7 (resumed)" subsection. Acceptable.
