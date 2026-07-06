# Raw-Scene Subtitle Auto-Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a raw scene's selected subtitle text track is not in the project's output language, translate its cues via a cheap OpenRouter model, with a per-project content-hash cache so re-runs never re-translate.

**Architecture:** A new `SubtitleTranslationService` (texts in → translated texts out, `None` on failure) owns the cache and the batched JSON LLM call. `LLMConfigService` gains a dedicated `translation` model entry (default `google/gemini-2.5-flash-lite`, falls back to the default preset's `light` tier). `ProcessingService._collect_raw_scene_source_subtitles` is the single integration point: it collects resolved text cues with their source language, then swaps in translations grouped by source language. Image (PGS) cues untouched.

**Tech Stack:** Python 3 / FastAPI backend, Pydantic models, OpenRouter via `openai` client (sync, wrapped in `asyncio.to_thread`), pytest + pytest-asyncio (strict mode: use `@pytest.mark.asyncio`).

**Spec:** `docs/superpowers/specs/2026-07-06-raw-scene-subtitle-translation-design.md`

## Global Constraints

- Run tests from repo root: `pixi run test tests/<file>.py -v` (task runs pytest with cwd=backend). If pixi is unavailable, `cd backend && pytest tests/<file>.py -v`.
- Test files start with `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` then import from `app.…` (repo convention).
- Cache file lives at `settings.projects_dir / <project_id> / "subtitle_translations.json"` — NOT under `output/` (output artifacts are wiped between runs).
- Cache key = `sha256(f"{source_lang}|{target_lang}|{text}")` with `None` source normalized to `"und"`. Model ID is metadata only, never part of the key.
- Translation failure must NEVER fail processing: service returns `None`, caller keeps original texts.
- Default translation model ID (copy verbatim): `google/gemini-2.5-flash-lite`.
- Max cues per LLM call: `100`. One retry per chunk on malformed/misaligned output.
- `SrtEntry` is a frozen dataclass (`processing.py:258`) — never mutate; build new instances.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `translation` entry in LLM config

**Files:**
- Modify: `backend/app/models/llm_config.py` (add field to `LLMConfig`, ~line 43)
- Modify: `backend/app/services/llm_config_service.py` (new method `translation_entry`)
- Modify: `config/llm/config.yaml` (add `translation:` block)
- Modify: `config/llm/config.example.yaml` (same block)
- Test: `backend/tests/test_llm_config_service.py` (append tests)

**Interfaces:**
- Consumes: existing `LLMPresetEntry`, `LLMConfig`, `LLMConfigService.get_config()`.
- Produces: `LLMConfigService.translation_entry() -> LLMPresetEntry` — used by Task 3. Returns the config's `translation` entry, or the **default preset's `light` entry** when the block is absent.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_llm_config_service.py`:

```python
TRANSLATION_YAML = VALID_YAML + """\
translation:
  openrouter_id: google/gemini-2.5-flash-lite
  thinking: null
"""


def test_translation_entry_from_config(tmp_path, monkeypatch):
    path = _write(tmp_path, TRANSLATION_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    entry = LLMConfigService.translation_entry()
    assert entry.openrouter_id == "google/gemini-2.5-flash-lite"
    assert entry.thinking is None


def test_translation_entry_falls_back_to_default_light(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    entry = LLMConfigService.translation_entry()
    # default preset is "claude"; its light model is haiku
    assert entry.openrouter_id == "anthropic/claude-haiku-4.5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/test_llm_config_service.py -v`
Expected: the two new tests FAIL with `AttributeError: ... has no attribute 'translation_entry'` (or Pydantic `extra: forbid` validation error for the first). Existing tests still PASS.

- [ ] **Step 3: Add the `translation` field to `LLMConfig`**

In `backend/app/models/llm_config.py`, replace the `LLMConfig` class body's field block:

```python
class LLMConfig(BaseModel):
    """Root of config/llm/config.yaml."""

    default: str = Field(..., min_length=1)
    presets: dict[str, LLMPreset]
    translation: LLMPresetEntry | None = None
    model_config = {"extra": "forbid"}
```

(The `_default_must_exist` validator below stays unchanged.)

- [ ] **Step 4: Add `translation_entry()` to `LLMConfigService`**

In `backend/app/services/llm_config_service.py`, add after `get_preset`:

```python
    @classmethod
    def translation_entry(cls) -> LLMPresetEntry:
        """Model used for subtitle translation; falls back to the default preset's light tier."""
        cfg = cls.get_config()
        if cfg.translation is not None:
            return cfg.translation
        return cfg.presets[cfg.default].light
```

And extend the models import at the top of the file:

```python
from ..models.llm_config import LLMConfig, LLMPreset, LLMPresetEntry
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run test tests/test_llm_config_service.py tests/test_llm_config_models.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Add the block to both YAML files**

Append to `config/llm/config.yaml` AND `config/llm/config.example.yaml`:

```yaml

translation:
  openrouter_id: google/gemini-2.5-flash-lite
  thinking: null
```

Sanity check it loads: `pixi run test tests/test_llm_config_service.py -v` (still PASS — these tests use tmp files, this step is just config hygiene; optionally verify with `cd backend && python -c "from app.services.llm_config_service import LLMConfigService; print(LLMConfigService.translation_entry().openrouter_id)"` → `google/gemini-2.5-flash-lite`).

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/llm_config.py backend/app/services/llm_config_service.py config/llm/config.yaml config/llm/config.example.yaml backend/tests/test_llm_config_service.py
git commit -m "feat(llm-config): dedicated translation model entry with light-tier fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `OpenRouterService.generate_json_value_with_entry`

**Files:**
- Modify: `backend/app/services/openrouter_service.py` (add one public method after `generate_json_value`, ~line 172)
- Test: `backend/tests/test_openrouter_service.py` (append test)

**Interfaces:**
- Consumes: existing private `OpenRouterService._chat(prompt, *, entry, system, max_output_tokens)` and `_parse_json_value(raw)`.
- Produces: `OpenRouterService.generate_json_value_with_entry(prompt: str, *, entry: LLMPresetEntry, system: str | None = None, max_output_tokens: int | None = None) -> Any` — bypasses preset/tier resolution so the translation model is used directly. Raises `RuntimeError` on empty/unparseable responses (same contract as `generate_json_value`). Used by Task 3.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_openrouter_service.py` (reuses the module's existing `_make_chat_response` helper):

```python
def test_generate_json_value_with_entry_uses_given_model(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response(
        '[{"i": 0, "t": "Bonjour"}]'
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )
    entry = LLMPresetEntry(
        openrouter_id="google/gemini-2.5-flash-lite", thinking=None
    )
    parsed = OpenRouterService.generate_json_value_with_entry(
        '[{"i": 0, "t": "Hello"}]',
        entry=entry,
        system="translate",
    )
    assert parsed == [{"i": 0, "t": "Bonjour"}]
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "google/gemini-2.5-flash-lite"
    assert kwargs["messages"][0] == {"role": "system", "content": "translate"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test tests/test_openrouter_service.py -v`
Expected: new test FAILS with `AttributeError: ... has no attribute 'generate_json_value_with_entry'`.

- [ ] **Step 3: Implement the method**

In `backend/app/services/openrouter_service.py`, add right after `generate_json_value` (before `generate_json`):

```python
    @classmethod
    def generate_json_value_with_entry(
        cls,
        prompt: str,
        *,
        entry: LLMPresetEntry,
        system: str | None = None,
        max_output_tokens: int | None = None,
    ) -> Any:
        """JSON call with an explicit model entry, bypassing preset/tier resolution."""
        raw = cls._chat(
            prompt,
            entry=entry,
            system=system,
            max_output_tokens=max_output_tokens,
        )
        return cls._parse_json_value(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test tests/test_openrouter_service.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/openrouter_service.py backend/tests/test_openrouter_service.py
git commit -m "feat(openrouter): JSON call with explicit model entry

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `SubtitleTranslationService` — cache, key, chunk validation

**Files:**
- Create: `backend/app/services/subtitle_translation_service.py`
- Create: `backend/tests/test_subtitle_translation_service.py`

**Interfaces:**
- Consumes: `settings.projects_dir` (`app.config`), `LLMConfigService.translation_entry()` (Task 1), `OpenRouterService.generate_json_value_with_entry` (Task 2) — the latter two only referenced in Task 4's methods, but the imports land here.
- Produces (used inside Task 4 and by its tests):
  - `SubtitleTranslationService._cache_path(project_id: str) -> Path`
  - `SubtitleTranslationService._cache_key(source_language: str | None, target_language: str, text: str) -> str`
  - `SubtitleTranslationService._load_cache(project_id: str) -> dict[str, dict[str, Any]]`
  - `SubtitleTranslationService._save_cache(project_id: str, entries: dict[str, dict[str, Any]]) -> None`
  - `SubtitleTranslationService._validate_chunk(parsed: Any, *, expected_count: int) -> list[str]` (raises `ValueError` on any shape problem)
  - Constants `CACHE_VERSION = 1`, `MAX_CUES_PER_CALL = 100`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_subtitle_translation_service.py`:

```python
"""Unit tests for SubtitleTranslationService — OpenRouter is always mocked."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.subtitle_translation_service import SubtitleTranslationService


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.subtitle_translation_service.settings.projects_dir", tmp_path
    )
    (tmp_path / "proj1").mkdir()
    return tmp_path / "proj1"


# --- cache key ---

def test_cache_key_is_content_hash():
    k1 = SubtitleTranslationService._cache_key("en", "fr", "Hello")
    k2 = SubtitleTranslationService._cache_key("en", "fr", "Hello")
    k3 = SubtitleTranslationService._cache_key("en", "fr", "Hello!")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64  # sha256 hex


def test_cache_key_none_source_normalized_to_und():
    k_none = SubtitleTranslationService._cache_key(None, "fr", "Hello")
    k_und = SubtitleTranslationService._cache_key("und", "fr", "Hello")
    assert k_none == k_und


def test_cache_key_varies_by_languages():
    base = SubtitleTranslationService._cache_key("en", "fr", "Hello")
    assert SubtitleTranslationService._cache_key("ja", "fr", "Hello") != base
    assert SubtitleTranslationService._cache_key("en", "de", "Hello") != base


# --- cache load/save ---

def test_cache_roundtrip(project_dir):
    entries = {
        "abc": {
            "source_text": "Hello",
            "translated_text": "Bonjour",
            "source_language": "en",
            "target_language": "fr",
            "model": "google/gemini-2.5-flash-lite",
            "translated_at": "2026-07-06T12:00:00",
        }
    }
    SubtitleTranslationService._save_cache("proj1", entries)
    cache_file = project_dir / "subtitle_translations.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert SubtitleTranslationService._load_cache("proj1") == entries


def test_load_cache_missing_file_returns_empty(project_dir):
    assert SubtitleTranslationService._load_cache("proj1") == {}


def test_load_cache_corrupt_file_returns_empty(project_dir):
    (project_dir / "subtitle_translations.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert SubtitleTranslationService._load_cache("proj1") == {}


# --- chunk validation ---

def test_validate_chunk_happy_path():
    parsed = [{"i": 1, "t": "deux"}, {"i": 0, "t": "un"}]
    assert SubtitleTranslationService._validate_chunk(parsed, expected_count=2) == [
        "un",
        "deux",
    ]


@pytest.mark.parametrize(
    "parsed",
    [
        {"i": 0, "t": "x"},  # not a list
        [{"i": 0, "t": "un"}],  # wrong count (expected 2)
        [{"i": 0, "t": "un"}, {"i": 0, "t": "deux"}],  # duplicate index
        [{"i": 0, "t": "un"}, {"i": 5, "t": "deux"}],  # out-of-range index
        [{"i": 0, "t": "un"}, {"i": 1, "t": "   "}],  # blank translation
        [{"i": 0, "t": "un"}, {"i": 1}],  # missing text
        [{"i": 0, "t": "un"}, "deux"],  # non-dict item
    ],
)
def test_validate_chunk_rejects_bad_shapes(parsed):
    with pytest.raises(ValueError):
        SubtitleTranslationService._validate_chunk(parsed, expected_count=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/test_subtitle_translation_service.py -v`
Expected: FAIL at import time with `ModuleNotFoundError: No module named 'app.services.subtitle_translation_service'`.

- [ ] **Step 3: Create the service with cache + validation**

Create `backend/app/services/subtitle_translation_service.py`:

```python
"""Translates raw-scene subtitle cues to the project language.

Per-project JSON cache keyed by content hash: unchanged cues always hit,
changed cues miss — no scene-change detection needed. LLM calls go through
OpenRouter using the dedicated translation model entry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from .llm_config_service import LLMConfigService
from .openrouter_service import OpenRouterService

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
MAX_CUES_PER_CALL = 100


class SubtitleTranslationService:
    """Texts in, translated texts out; ``None`` on failure so callers keep originals."""

    @classmethod
    def _cache_path(cls, project_id: str) -> Path:
        return settings.projects_dir / project_id / "subtitle_translations.json"

    @classmethod
    def _cache_key(
        cls,
        source_language: str | None,
        target_language: str,
        text: str,
    ) -> str:
        source = source_language or "und"
        return hashlib.sha256(
            f"{source}|{target_language}|{text}".encode("utf-8")
        ).hexdigest()

    @classmethod
    def _load_cache(cls, project_id: str) -> dict[str, dict[str, Any]]:
        path = cls._cache_path(project_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Subtitle translation cache unreadable (%s): %s", path, exc)
            return {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            logger.warning("Subtitle translation cache malformed (%s); ignoring", path)
            return {}
        return entries

    @classmethod
    def _save_cache(cls, project_id: str, entries: dict[str, dict[str, Any]]) -> None:
        path = cls._cache_path(project_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"version": CACHE_VERSION, "entries": entries},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to write subtitle translation cache (%s): %s", path, exc)

    @staticmethod
    def _validate_chunk(parsed: Any, *, expected_count: int) -> list[str]:
        """Enforce the [{"i": idx, "t": text}] contract; raise ValueError otherwise."""
        if not isinstance(parsed, list) or len(parsed) != expected_count:
            raise ValueError(
                f"expected a JSON array of {expected_count} items, got {type(parsed).__name__}"
            )
        out: list[str | None] = [None] * expected_count
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("translation item is not an object")
            index = item.get("i")
            text = item.get("t")
            if not isinstance(index, int) or not 0 <= index < expected_count:
                raise ValueError(f"translation index out of range: {index!r}")
            if out[index] is not None:
                raise ValueError(f"duplicate translation index: {index}")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"empty translation for index {index}")
            out[index] = text.strip()
        return [text for text in out if text is not None]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test tests/test_subtitle_translation_service.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/subtitle_translation_service.py backend/tests/test_subtitle_translation_service.py
git commit -m "feat(translation): subtitle translation cache and response validation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `SubtitleTranslationService.translate_texts` — full flow

**Files:**
- Modify: `backend/app/services/subtitle_translation_service.py` (add prompt builder, chunk call with retry, sync flow, async facade)
- Modify: `backend/app/services/__init__.py` (register lazy export next to the other LLM services, ~line 42)
- Test: `backend/tests/test_subtitle_translation_service.py` (append tests)

**Interfaces:**
- Consumes: Task 3 helpers; `LLMConfigService.translation_entry() -> LLMPresetEntry`; `OpenRouterService.generate_json_value_with_entry(prompt, *, entry, system, max_output_tokens) -> Any`.
- Produces (used by Task 5):
  ```python
  async def translate_texts(
      *, project_id: str, texts: list[str],
      source_language: str | None, target_language: str,
  ) -> list[str] | None
  ```
  Returns translations aligned 1:1 with `texts`; `[]` for empty input; `None` on any failure (cache untouched on failure).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_subtitle_translation_service.py`:

```python
# --- translate_texts flow (OpenRouter mocked) ---

def _mock_llm(monkeypatch, responses):
    """Queue raw parsed-JSON responses for generate_json_value_with_entry; records prompts."""
    calls: list[dict] = []

    def fake_call(prompt, *, entry, system=None, max_output_tokens=None):
        calls.append({"prompt": prompt, "entry": entry, "system": system})
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "app.services.subtitle_translation_service."
        "OpenRouterService.generate_json_value_with_entry",
        fake_call,
    )
    return calls


@pytest.fixture
def translation_entry(monkeypatch):
    from app.models.llm_config import LLMPresetEntry

    entry = LLMPresetEntry(
        openrouter_id="google/gemini-2.5-flash-lite", thinking=None
    )
    monkeypatch.setattr(
        "app.services.subtitle_translation_service."
        "LLMConfigService.translation_entry",
        classmethod(lambda cls: entry),
    )
    return entry


@pytest.mark.asyncio
async def test_translate_texts_miss_then_hit(project_dir, translation_entry, monkeypatch):
    calls = _mock_llm(
        monkeypatch, [[{"i": 0, "t": "Bonjour"}, {"i": 1, "t": "Monde"}]]
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out == ["Bonjour", "Monde"]
    assert len(calls) == 1
    sent = json.loads(calls[0]["prompt"])
    assert sent == [{"i": 0, "t": "Hello"}, {"i": 1, "t": "World"}]
    assert "en" in calls[0]["system"] and "fr" in calls[0]["system"]

    # second run: pure cache hit, zero LLM calls
    out2 = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out2 == ["Bonjour", "Monde"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_translate_texts_dedupes_identical_cues(project_dir, translation_entry, monkeypatch):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "Hello", "Hello"],
        source_language="en",
        target_language="fr",
    )
    assert out == ["Bonjour", "Bonjour", "Bonjour"]
    assert json.loads(calls[0]["prompt"]) == [{"i": 0, "t": "Hello"}]


@pytest.mark.asyncio
async def test_translate_texts_partial_cache_sends_only_misses(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    calls2 = _mock_llm(monkeypatch, [[{"i": 0, "t": "Monde"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out == ["Bonjour", "Monde"]
    assert json.loads(calls2[0]["prompt"]) == [{"i": 0, "t": "World"}]


@pytest.mark.asyncio
async def test_translate_texts_retries_once_then_succeeds(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(
        monkeypatch,
        [
            [{"i": 0, "t": "   "}],  # invalid → retry
            [{"i": 0, "t": "Bonjour"}],
        ],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    assert out == ["Bonjour"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_translate_texts_fails_after_retry_returns_none_and_no_cache(
    project_dir, translation_entry, monkeypatch
):
    _mock_llm(
        monkeypatch,
        [RuntimeError("boom"), RuntimeError("boom again")],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    assert out is None
    assert not (project_dir / "subtitle_translations.json").exists()


@pytest.mark.asyncio
async def test_translate_texts_unknown_source_language(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language=None, target_language="fr"
    )
    assert out == ["Bonjour"]
    assert "detect the source language" in calls[0]["system"]


@pytest.mark.asyncio
async def test_translate_texts_empty_input(project_dir, translation_entry):
    assert await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=[], source_language="en", target_language="fr"
    ) == []


@pytest.mark.asyncio
async def test_translate_texts_chunks_large_batches(
    project_dir, translation_entry, monkeypatch
):
    monkeypatch.setattr(
        "app.services.subtitle_translation_service.MAX_CUES_PER_CALL", 2
    )
    calls = _mock_llm(
        monkeypatch,
        [
            [{"i": 0, "t": "T0"}, {"i": 1, "t": "T1"}],
            [{"i": 0, "t": "T2"}],
        ],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["a", "b", "c"],
        source_language="en",
        target_language="fr",
    )
    assert out == ["T0", "T1", "T2"]
    assert len(calls) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/test_subtitle_translation_service.py -v`
Expected: new tests FAIL with `AttributeError: ... has no attribute 'translate_texts'`. Task 3 tests still PASS.

- [ ] **Step 3: Implement prompt, chunk call, and flow**

Append to the `SubtitleTranslationService` class in `backend/app/services/subtitle_translation_service.py`:

```python
    @staticmethod
    def _system_prompt(source_language: str | None, target_language: str) -> str:
        source_clause = (
            f"from '{source_language}' "
            if source_language
            else "(detect the source language) "
        )
        return (
            "You are a professional subtitle translator for anime dialogue. "
            f"Translate each cue {source_clause}to '{target_language}'. "
            "Preserve tone, character names, and honorifics. "
            "Keep each translation concise enough to read as an on-screen subtitle. "
            'Respond with a JSON array of objects {"i": <same index as input>, '
            '"t": <translated text>} covering every input cue, in any order. '
            "No markdown fences, no explanations."
        )

    @classmethod
    def _translate_chunk(
        cls,
        chunk: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str]:
        entry = LLMConfigService.translation_entry()
        system = cls._system_prompt(source_language, target_language)
        prompt = json.dumps(
            [{"i": index, "t": text} for index, text in enumerate(chunk)],
            ensure_ascii=False,
        )
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                parsed = OpenRouterService.generate_json_value_with_entry(
                    prompt,
                    entry=entry,
                    system=system,
                )
                return cls._validate_chunk(parsed, expected_count=len(chunk))
            except (RuntimeError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "Subtitle translation chunk failed (attempt %d, %d cues): %s",
                    _attempt + 1,
                    len(chunk),
                    exc,
                )
        raise RuntimeError(f"subtitle translation failed after retry: {last_error}")

    @classmethod
    def _translate_texts_sync(
        cls,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str]:
        cache = cls._load_cache(project_id)
        keys = [
            cls._cache_key(source_language, target_language, text) for text in texts
        ]

        missing_keys: list[str] = []
        missing_texts: list[str] = []
        seen: set[str] = set()
        for key, text in zip(keys, texts):
            if key in cache or key in seen:
                continue
            seen.add(key)
            missing_keys.append(key)
            missing_texts.append(text)

        if missing_texts:
            translated: list[str] = []
            for start in range(0, len(missing_texts), MAX_CUES_PER_CALL):
                chunk = missing_texts[start : start + MAX_CUES_PER_CALL]
                translated.extend(
                    cls._translate_chunk(chunk, source_language, target_language)
                )
            model_id = LLMConfigService.translation_entry().openrouter_id
            translated_at = datetime.now().isoformat()
            for key, source_text, translated_text in zip(
                missing_keys, missing_texts, translated
            ):
                cache[key] = {
                    "source_text": source_text,
                    "translated_text": translated_text,
                    "source_language": source_language or "und",
                    "target_language": target_language,
                    "model": model_id,
                    "translated_at": translated_at,
                }
            cls._save_cache(project_id, cache)
            logger.info(
                "Translated %d raw-scene subtitle cues (%s -> %s) with %s",
                len(missing_texts),
                source_language or "und",
                target_language,
                model_id,
            )

        return [cache[key]["translated_text"] for key in keys]

    @classmethod
    async def translate_texts(
        cls,
        *,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str] | None:
        """Translate cue texts, aligned 1:1 with the input; ``None`` on failure."""
        if not texts:
            return []
        try:
            return await asyncio.to_thread(
                cls._translate_texts_sync,
                project_id,
                list(texts),
                source_language,
                target_language,
            )
        except Exception as exc:
            logger.warning(
                "Raw-scene subtitle translation unavailable (%s -> %s), keeping originals: %s",
                source_language or "und",
                target_language,
                exc,
            )
            return None
```

Note: `MAX_CUES_PER_CALL` must be read as a module attribute for the chunking test's monkeypatch to work — since `_translate_texts_sync` references the module-level name directly, the monkeypatch on the module attribute applies. No change needed; just don't copy it into a class attribute.

- [ ] **Step 4: Register the lazy export**

In `backend/app/services/__init__.py`, add below the `"LLMConfigService"` line (~line 42):

```python
    "SubtitleTranslationService": (
        ".subtitle_translation_service",
        "SubtitleTranslationService",
    ),
```

(Match the file's existing mapping style exactly — it maps name → (module, attr).)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run test tests/test_subtitle_translation_service.py -v`
Expected: ALL PASS (Task 3 + Task 4 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/subtitle_translation_service.py backend/app/services/__init__.py backend/tests/test_subtitle_translation_service.py
git commit -m "feat(translation): batched cached translate_texts flow with retry and fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire translation into raw-scene subtitle collection

**Files:**
- Modify: `backend/app/services/processing.py`:
  - `_resolve_raw_scene_sidecar_subtitles` (~line 1783): also return the winning language
  - call site in `_build_raw_scene_image_render_plan` (~line 2007): unpack 3-tuple
  - `_collect_raw_scene_source_subtitles` (~line 2106): collect (entry, language) pairs, translate after the loop
  - new helper `_translate_raw_scene_text_entries` (place directly before `_collect_raw_scene_source_subtitles`)
  - import `SubtitleTranslationService`
- Test: Create `backend/tests/test_processing_raw_scene_translation.py`

**Interfaces:**
- Consumes: `SubtitleTranslationService.translate_texts(*, project_id, texts, source_language, target_language) -> list[str] | None` (Task 4); frozen dataclass `SrtEntry(start: float, end: float, text: str)` (`processing.py:258`).
- Produces:
  - `_resolve_raw_scene_sidecar_subtitles` now returns `tuple[list[SrtEntry], list[_RawSceneImageCueCandidate], str | None]` (third element = normalized language of the winning group, `None` when nothing resolved or language unknown).
  - `async _translate_raw_scene_text_entries(project_id: str, target_language: str | None, pending_entries: list[tuple[SrtEntry, str | None]]) -> list[SrtEntry]` classmethod on `ProcessingService`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_processing_raw_scene_translation.py`:

```python
"""Tests for the raw-scene subtitle translation hook in ProcessingService."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.processing import ProcessingService, SrtEntry


def _install_fake_translate(monkeypatch, results):
    """results: maps source_language -> list[str] | None."""
    calls: list[dict] = []

    async def fake_translate(**kwargs):
        calls.append(kwargs)
        return results[kwargs["source_language"]]

    monkeypatch.setattr(
        "app.services.processing.SubtitleTranslationService.translate_texts",
        fake_translate,
    )
    return calls


@pytest.mark.asyncio
async def test_translates_only_non_target_entries(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {"en": ["FR-Hello", "FR-World"]})
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="Hello"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="Déjà bon"), "fr"),
        (SrtEntry(start=2.0, end=3.0, text="World"), "en"),
    ]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["FR-Hello", "Déjà bon", "FR-World"]
    # timing preserved
    assert [(entry.start, entry.end) for entry in out] == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
    ]
    assert calls == [
        {
            "project_id": "proj1",
            "texts": ["Hello", "World"],
            "source_language": "en",
            "target_language": "fr",
        }
    ]


@pytest.mark.asyncio
async def test_groups_by_source_language(monkeypatch):
    calls = _install_fake_translate(
        monkeypatch, {"en": ["FR-en"], None: ["FR-unknown"]}
    )
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="english"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="mystery"), None),
    ]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["FR-en", "FR-unknown"]
    assert {call["source_language"] for call in calls} == {"en", None}


@pytest.mark.asyncio
async def test_failure_keeps_originals(monkeypatch):
    _install_fake_translate(monkeypatch, {"en": None})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Hello"), "en")]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["Hello"]


@pytest.mark.asyncio
async def test_no_target_language_skips_translation(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Hello"), "en")]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language=None, pending_entries=pending
    )
    assert [entry.text for entry in out] == ["Hello"]
    assert calls == []


@pytest.mark.asyncio
async def test_all_entries_already_target_language(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Bonjour"), "fr")]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["Bonjour"]
    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/test_processing_raw_scene_translation.py -v`
Expected: FAIL with `AttributeError: ... has no attribute '_translate_raw_scene_text_entries'` (import of `processing` is heavy — spacy etc. — a slow collection phase is normal).

- [ ] **Step 3: Implement in `processing.py`**

3a. Add the import next to the other service imports (~line 44):

```python
from .subtitle_translation_service import SubtitleTranslationService
```

3b. In `_resolve_raw_scene_sidecar_subtitles` (~line 1783): the loop currently reads
`for _language, language_entries in cls._preferred_raw_scene_language_groups(...)`.
Rename `_language` → `language`, update the return type annotation to
`tuple[list[SrtEntry], list[_RawSceneImageCueCandidate], str | None]`, and change the two returns:

```python
            if resolved_text_entries or resolved_image_entries:
                return resolved_text_entries, resolved_image_entries, language
        return [], [], None
```

3c. Fix the other call site in `_build_raw_scene_image_render_plan` (~line 2007):

```python
                _, resolved_image_entries, _ = await cls._resolve_raw_scene_sidecar_subtitles(
```

3d. Add the helper directly before `_collect_raw_scene_source_subtitles`:

```python
    @classmethod
    async def _translate_raw_scene_text_entries(
        cls,
        *,
        project_id: str,
        target_language: str | None,
        pending_entries: list[tuple[SrtEntry, str | None]],
    ) -> list[SrtEntry]:
        """Swap raw-scene cue texts to the project language, grouped by source track language."""
        results = [entry for entry, _language in pending_entries]
        if not target_language:
            return results

        groups: dict[str | None, list[int]] = {}
        for index, (_entry, language) in enumerate(pending_entries):
            if language == target_language:
                continue
            groups.setdefault(language, []).append(index)

        for source_language, indices in groups.items():
            translated = await SubtitleTranslationService.translate_texts(
                project_id=project_id,
                texts=[results[index].text for index in indices],
                source_language=source_language,
                target_language=target_language,
            )
            if translated is None:
                logger.warning(
                    "Keeping %d untranslated raw-scene subtitle cues (%s)",
                    len(indices),
                    source_language or "und",
                )
                continue
            for index, new_text in zip(indices, translated):
                original = results[index]
                results[index] = SrtEntry(
                    start=original.start,
                    end=original.end,
                    text=new_text,
                )
        return results
```

3e. In `_collect_raw_scene_source_subtitles` (~line 2106):

- Replace the accumulator declaration `text_entries: list[SrtEntry] = []` with:

```python
        pending_text_entries: list[tuple[SrtEntry, str | None]] = []
```

- Update the resolve call inside the scene loop to unpack the 3-tuple, and replace `text_entries.extend(scene_text_entries)`:

```python
            scene_text_entries, scene_image_entries, scene_language = (
                await cls._resolve_raw_scene_sidecar_subtitles(
                    resolved_source=resolved_source,
                    timeline_scene_start=scene.start_time,
                    target_language=target_language,
                    sidecar_entries=sidecar_entries,
                    parsed_text_cache=parsed_text_cache,
                    parsed_cue_cache=parsed_cue_cache,
                    rendered_cue_cache=rendered_cue_cache,
                    resolve_image_assets=True,
                )
            )
            pending_text_entries.extend(
                (entry, scene_language) for entry in scene_text_entries
            )
```

- After the `for scene in raw_scenes:` loop ends (before the `if image_entries:` manifest block), materialize the translated list — the function's existing `target_language` local is exactly the translation target:

```python
        text_entries = await cls._translate_raw_scene_text_entries(
            project_id=project.id,
            target_language=target_language,
            pending_entries=pending_text_entries,
        )
```

The function's existing `return text_entries, image_entries` stays unchanged; both SRT outputs downstream render from `text_entries`, so they pick up translations automatically.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test tests/test_processing_raw_scene_translation.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Run the full backend suite for regressions**

Run: `pixi run test`
Expected: ALL PASS (notably `tests/test_processing_overlay_jsx.py`, which also imports processing.py).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/processing.py backend/tests/test_processing_raw_scene_translation.py
git commit -m "feat(processing): auto-translate raw-scene subtitles to project language

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Verification (manual, after all tasks)

1. Pick a project whose `output_language` is `fr` with raw scenes whose episode only has an `en` text track.
2. Run the processing phase; check the log line `Translated N raw-scene subtitle cues (en -> fr) with google/gemini-2.5-flash-lite`.
3. Inspect `backend/data/projects/<id>/subtitle_translations.json` — entries present, French text sensible.
4. Open `output/<...>.srt` — raw-scene cues are French; TTS-word cues unaffected.
5. Re-run processing: no `Translated N ...` log line (pure cache hits), SRT identical.
