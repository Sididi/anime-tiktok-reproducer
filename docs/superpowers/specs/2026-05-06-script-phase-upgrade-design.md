# /script phase upgrade — design

**Date:** 2026-05-06
**Status:** Approved (proceed to implementation)

## Goals

1. Replace bespoke per-provider LLM adapters with a single OpenRouter-backed service so adding new providers is a config edit, not a code change.
2. Move LLM model selection out of `.env` into a versioned YAML catalog (`config/llm/config.yaml`) with explicit big/light tiers and per-tier reasoning configs.
3. Expose three per-project knobs in the /script phase — **LLM preset**, **min playback speed**, **template** — each with sensible defaults sourced from config.
4. Introduce a **template** system that captures the asset/visual choices currently hardcoded (foreground prfpset + zoom, background prfpset, subtitle mogrts, white border, overlay style). Ship two templates: `classic` (current production) and `minimal` (overlay-only variant matching the reference screenshot — placeholder renderer, refined later).
5. Surface the three per-project knobs in the Project Manager.

## Non-goals

- Authoring the actual Premiere prfpsets for the `minimal` template (user-owned).
- Pixel-perfect tuning of the `minimal` Pillow renderer; ship structurally complete stubs and refine in production.
- Per-call-site thinking overrides beyond the big/light tier split.
- Per-project light-model override (presets bundle big + light together).
- Any change to TTS, transcription, scene detection, or processing phases other than what's needed to consume template values.

---

## Architecture

### LLM layer — full OpenRouter migration

**Replace:** `claude_service.py`, `gemini_service.py` are deleted.

**New:** `app/services/openrouter_service.py` — uses the `openai` SDK pointed at `https://openrouter.ai/api/v1`. Implements the existing `LLMService` facade contract verbatim so call sites do not change shape:

- `generate_text(prompt, *, tier="big") -> str`
- `generate_json(prompt, *, tier="big") -> dict`
- `generate_json_value(prompt, *, tier="big") -> Any`
- `check_api_health() -> bool`
- `active_model() -> str` (resolves to current preset's big model id)
- `active_light_model() -> str` (resolves to current preset's light model id)

The `tier` argument is new. Default is `"big"`. Call sites that explicitly use the light model (overlay/title/category/metadata) are updated to pass `tier="light"` — concretely: `script_automation_service.generate_video_overlay`, `script_automation_service` title-hook calls, and `metadata.py` calls. The service consults the active preset (project-level override or config default) to pick the model id and reasoning shape. Endpoint signatures remain unchanged; only internal call sites add the kwarg.

**Reasoning passthrough:** the OpenRouter `reasoning` object is built from the preset entry and sent via `extra_body`. Always include `exclude: true` so we don't pay for transporting CoT we never display. Shapes:

- Anthropic models → `{"max_tokens": N, "exclude": true}`
- Gemini models → `{"effort": "low|medium|high|xhigh", "exclude": true}`
- `null` thinking config → no `reasoning` key sent

**Health check:** OpenRouter's `/api/v1/models` endpoint, with a HEAD or short GET. Single API key suffices.

### LLM config file

Path: `config/llm/config.yaml` (+ `config.example.yaml`).

```yaml
default: claude
presets:
  claude:
    label: "Claude (Opus 4.7 + Haiku 4.5)"
    big:
      openrouter_id: anthropic/claude-opus-4.7
      thinking: { max_tokens: 6000 }
    light:
      openrouter_id: anthropic/claude-haiku-4.5
      thinking: null
  gemini:
    label: "Gemini (3.1 Pro + 2.5 Flash)"
    big:
      openrouter_id: google/gemini-3-pro-preview
      thinking: { effort: high }
    light:
      openrouter_id: google/gemini-2.5-flash
      thinking: null
```

**Pydantic model:** `LLMPreset`, `LLMPresetEntry`, `LLMConfig` in `app/models/llm_config.py`. Loaded by a new `LLMConfigService` at startup, cached in-process. Path resolved through `Settings` (`llm_config_path: Path = PROJECT_ROOT / "config" / "llm" / "config.yaml"`).

**Thinking shape validation:** `thinking` field accepts either `{max_tokens: int}` (Anthropic) or `{effort: "low"|"medium"|"high"|"xhigh"}` (OpenAI-style) or `null`. Validated at load time.

### Settings cleanup

Remove from `Settings` and from `.env` / `.env.example`:

- `llm_provider` (`ATR_LLM_PROVIDER`)
- `gemini_api_key`, `gemini_model`, `gemini_light_model`, `gemini_timeout`
- `anthropic_api_key`, `anthropic_model`, `anthropic_light_model`, `anthropic_timeout`
- `grand_mode_enabled` (absorbed into `classic` template)

Add:

- `openrouter_api_key: str | None = None` (`ATR_OPENROUTER_API_KEY`)
- `openrouter_timeout: int = 600` (read timeout, generous for thinking)
- `llm_config_path: Path` (defaults under `config/llm/`)
- `templates_config_path: Path` (defaults under `config/templates/`)

Boot warning emitted to logs if any of the deleted env vars are still present in the user's environment, pointing them at the new config file.

### Templates

Path: `config/templates/config.yaml` (+ `config.example.yaml`).

```yaml
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

**Pydantic models** in `app/models/template.py`:

```python
class OverlaySideConfig(BaseModel):
    style: str
    prfpset: str | None = None

class OverlayConfig(BaseModel):
    enabled: bool
    title: OverlaySideConfig
    category: OverlaySideConfig

class WhiteBorderConfig(BaseModel):
    enabled: bool
    mogrt: str | None = None  # required when enabled

class ForegroundConfig(BaseModel):
    prfpset: str
    zoom: float  # 0 < zoom <= 2

class BackgroundConfig(BaseModel):
    prfpset: str

class SubtitlesConfig(BaseModel):
    mogrt: str
    raw_mogrt: str

class Template(BaseModel):
    label: str
    foreground: ForegroundConfig
    background: BackgroundConfig
    subtitles: SubtitlesConfig
    white_border: WhiteBorderConfig
    overlay: OverlayConfig

class TemplatesConfig(BaseModel):
    default: str
    templates: dict[str, Template]
```

**`TemplateService`** loads, validates (referenced asset files exist in `assets/`), caches at startup. Same shape as the existing `ConfigService` pattern used for voices/music/accounts.

**Renderer style registry:** `title_image_generator.py` is refactored. The current monolithic constants and `_render_title` / `_render_category` are split into `_render_title_classic` / `_render_category_classic` (verbatim current behavior). Two new functions `_render_title_minimal` / `_render_category_minimal` produce a placeholder gold-cream cursive title without panel and a semi-transparent gold category. A registry dict at module scope:

```python
TITLE_RENDERERS = {"classic": _render_title_classic, "minimal": _render_title_minimal}
CATEGORY_RENDERERS = {"classic": _render_category_classic, "minimal": _render_category_minimal}
```

`TitleImageGeneratorService.generate(...)` accepts `title_style: str, category_style: str` and looks up the renderer functions. Unknown style → raise. `Template` Pydantic validation rejects styles whose key is not present in the registry (validator imports the registry keys at load time).

### Per-project state

Three new optional fields on `Project` (in `app/models/project.py`), persisted in the existing `project.json`:

```python
llm_preset: str | None = None
min_playback_speed: float | None = None  # 0.10 < x <= 1.0
template: str | None = None
```

All optional; readers fall back to config defaults when `None`. Existing project files load unchanged.

**Resolution helpers** on `Project`:

- `resolved_llm_preset() -> str` → project value or `LLMConfigService.default_preset()`
- `resolved_min_playback_speed() -> float` → project value or `settings.min_playback_speed_factor`
- `resolved_template() -> Template` → project key or default key, then `TemplateService.get(...)`

Validation reuses existing rules; speed factor reuses `_validate_min_playback_speed_factor`.

### Speed factor wiring

Read sites currently going through `settings.min_playback_speed_factor` switch to `project.resolved_min_playback_speed()`:

- `otio_timing.py` (constructor default for `min_speed`)
- `gap_resolution.py`
- `routes/gaps.py` (5 references)
- `processing.py` and any callers passing this through

Where the call site has only a `Project` available, easy. Where it doesn't (helper functions), the project's resolved value is threaded as a parameter.

**Auto-rerun on change:** when `POST /projects/{id}/script/settings` mutates `min_playback_speed` AND `project.phase` is `>= SCRIPT_RESTRUCTURE` (i.e., gaps already computed and user is on the /script page), the endpoint enqueues the same gap-resolution job exposed by `/projects/{id}/gaps/recompute` (existing). Frontend polls until done before re-enabling Script controls. If the project is earlier than `SCRIPT_RESTRUCTURE`, just save the value.

### Endpoints

- `GET /projects/{id}/script/automation/config` — extend response with:
  - `templates: [{ key, label, overlay_enabled }]`
  - `llm_presets: [{ key, label }]`
  - `current: { llm_preset, template, min_playback_speed }`
  - `defaults: { llm_preset, template, min_playback_speed }`
- `POST /projects/{id}/script/settings` — new. Body `{ llm_preset?, template?, min_playback_speed? }`. Validates each. Persists. If speed changed and gaps exist, kicks gap re-run. Returns the updated `current` block plus a `gaps_recomputing: bool`.
- All LLM-consuming endpoints — no signature change. The `LLMService` consults `Project` (or the calling context) to pick preset.

### Frontend — /script UI

A new "Project settings" panel rendered at the top of `ScriptRestructurePage.tsx`, above the script editor:

- **Template** dropdown (key + label, marker for default).
- **LLM** dropdown (preset key + label, marker for default).
- **Min playback speed** slider, range 0.20 → 1.00, step 0.05, current value shown numerically alongside, "Reset" button to revert to config default.
- All three controls dispatch to `POST /projects/{id}/script/settings`. Speed-change shows a "Recomputing gaps…" toast and disables the script editor until the gap-resolve job completes.
- Overlay row in the script editor (existing UI for title + category) greys out and disables its action buttons when the active template's `overlay.enabled === false`.

### Frontend — Project Manager

`ProjectTable.tsx` adds three compact columns between **Type** and **Scheduled At**:

- **LLM** — preset key
- **Speed** — formatted as `0.75` etc.
- **Template** — template key

Default-fallback values render in italic to distinguish from explicit project-level overrides. Backend `ProjectManagerRow` (in `frontend types` and the `UploadPhaseService.list_manager_rows` builder) gains:

```ts
llm_preset_resolved: string;
llm_preset_is_default: boolean;
min_playback_speed_resolved: number;
min_playback_speed_is_default: boolean;
template_resolved: string;
template_is_default: boolean;
```

### JSX template generation (processing.py)

Every hardcoded asset name in the JSX generator is replaced with a lookup against the resolved template:

- Foreground prfpset + zoom → `template.foreground.prfpset` / `template.foreground.zoom`
- Background prfpset → `template.background.prfpset`
- Subtitle mogrts → `template.subtitles.mogrt` / `template.subtitles.raw_mogrt`
- White border:
  - `enabled === true` → import V2 track using `template.white_border.mogrt`
  - `enabled === false` → V2 track is omitted entirely from generated JSX
- Overlay:
  - `enabled === false` → no title/category PNG generation, no overlay tracks in JSX
  - `enabled === true` → call `TitleImageGeneratorService.generate(..., title_style=..., category_style=...)`, apply optional `overlay.title.prfpset` / `overlay.category.prfpset` if non-null when placing each overlay clip

### `Automate` interaction

`script_automation_service.ScriptAutomationService.run(...)`:

- Resolves project's preset and template at the start.
- Script generation always runs (big tier, with thinking).
- Title/category overlay generation is skipped entirely when `template.overlay.enabled === false` — no light-model call dispatched, no PNG generation, `project.video_overlay` stays `None`.
- Metadata generation continues to be gated by `automate_metadata_overlay_enabled` (orthogonal kill-switch).
- Switching templates mid-project does not retroactively re-render prior overlay PNGs or regenerate prior JSX. The next Automate / Generate run uses the current template.

---

## Data flow summary

1. Boot: `Settings` reads `.env`, `LLMConfigService` reads `config/llm/config.yaml`, `TemplateService` reads `config/templates/config.yaml`. Both fail loudly on schema errors. Boot warning if legacy LLM env vars are present.
2. User opens /script: `GET /script/automation/config` returns presets, templates, current+default values. UI renders the settings panel pre-populated.
3. User changes a setting: `POST /script/settings`. If speed changed past gaps phase, server kicks gap recompute.
4. User clicks Automate: orchestrator resolves preset + template; dispatches script gen → conditional overlay gen → conditional metadata gen → TTS.
5. User clicks Generate (processing phase): JSX builder reads resolved template, emits asset-correct JSX.

## Migration

- No data migration. Existing `project.json` files load unchanged; new fields default to `None`. First time a user opens /script, controls show config defaults.
- `.env` cleanup is a code change, not user-facing automatic — boot warning lists removed keys and points at the new config files. Users must populate `ATR_OPENROUTER_API_KEY` and the YAML files (example files committed alongside).
- Existing `config/llm/` and `config/templates/` directories created by this change include both `config.yaml` (with sensible defaults shipped) and `config.example.yaml`.

## Risks & mitigations

- **OpenRouter rate / pricing surprise:** mitigated by `exclude: true` on reasoning, sane thinking budgets (Opus 6k, Gemini high), and a single API key whose usage is dashboardable.
- **Template asset filenames mistyped:** `TemplateService` validates referenced asset files exist in `assets/` at load time and fails boot.
- **Gap recompute job races a script generation:** the speed-change endpoint awaits gap-resolve completion before returning. UI disables script controls during recompute.
- **Removed env vars in user's `.env`:** boot warning, no silent fallback. Cleaner than carrying compatibility shims.
- **`minimal` renderer is a placeholder:** documented; structure ships, fidelity comes later. User can iterate the renderer functions independently of any architecture work.

## Out-of-scope follow-ups

- New providers (gpt, deepseek) by adding presets.
- Authoring `minimal` prfpsets in Premiere.
- Pixel-tuning `_render_title_minimal` / `_render_category_minimal` against the reference screenshot.
- Per-call-site thinking overrides if a future call needs custom budget.
- A flat-catalog mode (preset.B) if mixing big/light providers ever becomes useful.
