# Raw-Scene Subtitle Auto-Translation — Design

**Date:** 2026-07-06
**Status:** Approved

## Problem

Raw scenes keep their original source subtitles (text cues extracted from the
anime episode's subtitle tracks). Track selection prefers the project's
`output_language`, then `en`, then the remaining tracks
(`AnimeLibraryService.get_preferred_subtitle_language_groups`, pinned for raw
scenes by `ProcessingService._preferred_raw_scene_language_groups`). When no
project-language track exists, the viewer sees foreign-language subtitles
(usually English) on raw scenes.

## Goal

When the selected text track's language differs from the project's
`output_language`, automatically translate the raw-scene text cues to the
project language via a cheap OpenRouter model. Translations are cached per
project so re-entering the /script or /processing phases never re-triggers
LLM calls unless the underlying cues actually changed.

Out of scope: image (PGS) subtitle cues — they stay on the existing render
path untouched. No OCR.

## Decisions (owner-confirmed)

1. **Trigger:** always translate when selected track language ≠ project
   language (no toggle, no source-language restriction). The existing
   fallback order already prefers `en` as the translation source when the
   target track is missing.
2. **Cache:** per-project JSON file keyed by content hash — invalidation is
   automatic, no scene-change detection logic.
3. **Model:** dedicated top-level `translation` entry in
   `config/llm/config.yaml`, independent of the big/light presets. Default:
   `google/gemini-2.5-flash-lite`.
4. **Failure:** fall back silently to untranslated cues (warning log). The
   pipeline never blocks on translation.
5. **Approach:** one batched JSON call per processing run for all cache
   misses (chunked at 100 cues as a safety valve).

Cost note: raw scenes contribute ~5–50 short cues per project (a few hundred
tokens). At Flash-Lite pricing this is well under $0.001 per project, so the
design optimizes for quality-per-dollar and zero redundant calls rather than
squeezing token counts.

## Architecture

### New service: `backend/app/services/subtitle_translation_service.py`

```python
class SubtitleTranslationService:
    @classmethod
    async def translate_texts(
        cls,
        *,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str] | None: ...
```

- Returns translated texts aligned 1:1 with `texts`, or `None` on any
  failure (caller keeps originals).
- Knows nothing about scenes or SRT — texts in, texts out.
- `source_language=None` (unknown track language) is allowed; the prompt
  asks the model to auto-detect the source.
- Internals: cache lookup → dedupe identical texts → batched LLM call for
  misses (via `asyncio.to_thread`, the OpenAI client is sync) → validate →
  write cache → re-expand and return.

### Model config

New top-level block in `config/llm/config.yaml` (and the `.example` twin):

```yaml
translation:
  openrouter_id: google/gemini-2.5-flash-lite
```

- `LLMConfigService.translation_entry() -> LLMPresetEntry` parses it;
  when the block is absent, falls back to the default preset's `light`
  entry (thinking stays `null` — translation needs none).
- `OpenRouterService` gains a small method to run a JSON-array call with an
  explicit `LLMPresetEntry` (reusing `_chat` and `_parse_json_value`), so
  the translation model bypasses preset/tier resolution.

### LLM call

- **System prompt:** professional subtitle translator for anime dialogue;
  translate from `{source_language}` (or "detect the source language") to
  `{target_language}`; preserve tone, character names, and honorifics; keep
  each line concise enough to read as an on-screen subtitle; respond with
  valid JSON only, same array shape, no explanations.
- **User message:** `[{"i": 0, "t": "cue text"}, ...]` — indices are the
  deduped-cue positions.
- **Validation:** parse with `OpenRouterService._parse_json_value`; require
  a list containing exactly the input `i` indices, each with a non-empty
  string `t`. One retry on malformed or misaligned output, then give up.
- **Chunking:** ≤100 cues per call; chunks run sequentially (this
  practically never triggers).
- The cache is only written when the whole batch validates — a failed call
  never poisons the cache, and the next run retries automatically.

### Cache

File: `data/projects/<project_id>/subtitle_translations.json` — project
root, **not** `output/` (output artifacts are wiped on re-runs; e.g.
`raw_scene_subtitles/` is `rmtree`'d each processing pass).

```json
{
  "version": 1,
  "entries": {
    "<sha256(source_lang|target_lang|text)>": {
      "source_text": "…",
      "translated_text": "…",
      "source_language": "en",
      "target_language": "fr",
      "model": "google/gemini-2.5-flash-lite",
      "translated_at": "2026-07-06T12:00:00"
    }
  }
}
```

- Key = `sha256(f"{normalized_source_lang}|{normalized_target_lang}|{text}")`
  with `None` source normalized to `"und"`. Because the key hashes the cue
  text itself, unchanged scenes always hit and changed scenes miss exactly
  on their new cues — no scene-change detection code exists anywhere.
- The model ID is stored as metadata but is **not** part of the key:
  changing the configured model never invalidates existing translations.
- Corrupt/unreadable cache file → treated as empty and rewritten (log a
  warning).

### Integration point (single touch in `processing.py`)

1. `_resolve_raw_scene_sidecar_subtitles` additionally returns the winning
   language group's language (the `_language` loop variable it already
   iterates), so the caller knows what language the resolved text cues are
   in.
2. `_collect_raw_scene_source_subtitles` — after the per-scene loop, before
   returning — gathers all text entries whose language differs from the
   normalized project target language and calls
   `SubtitleTranslationService.translate_texts(...)` once, swapping
   `SrtEntry.text` in place on success. Entries already in the target
   language, and all image entries, are untouched.
3. Both SRT outputs (merged `srt_content` and `raw_scene_srt_content`)
   render from the same entries list, so they pick up translations with no
   further changes.

If entries from multiple source languages appear in one run (different
scenes resolving to different tracks), group by source language and make one
call per group.

### Error handling

- OpenRouter not configured, network error, timeout, malformed response
  after retry, count/index mismatch → `translate_texts` returns `None`;
  caller logs a warning and keeps original texts. Processing continues.
- Cache write failures are logged and ignored (translation still applied
  for the current run).

## Testing

Unit tests (`backend/tests/test_subtitle_translation_service.py`), with
`OpenRouterService` mocked:

- Cache round-trip: miss → call → hit on second invocation with **zero**
  LLM calls.
- Dedupe: identical texts translated once, re-expanded correctly.
- Validation: misaligned indices / wrong count / empty strings → one retry
  → `None` fallback.
- Unknown source language path (`source_language=None`).
- Corrupt cache file → treated as empty, no crash.
- Partial cache: only misses are sent to the LLM.

Integration-level test for the `processing.py` hook: resolved raw-scene
entries in `en` with a `fr` project get swapped text; target-language
entries and image entries untouched; service returning `None` leaves
originals.
