# Empty narration repair

The generation prompt uses the transcription's `is_raw` flag. Empty source text
alone does not mean a scene should be silent. Only raw scenes must stay empty.

Automation repairs eligible drafts before TTS. Pasting a complete draft triggers
the same repair once; typing and reloading do not trigger paid calls. Existing
drafts have **Repair empty scenes / Retry repair**. Any applied repair requires
review, even when “Validate script before TTS” is unchecked. The editor highlights
changed scenes, shows before/after text, and marks scenes without source text.
Changed narration clears previously prepared audio, including after reload.

## Limits and validation

- `config/llm/config.yaml` has an independent `script_repair` entry, defaulting to
  `google/gemini-3.1-flash-lite` with minimal reasoning. Old configurations get the
  same default; generation presets do not select the repair model.
- One batched attempt edits empty scenes and at most two neighbors per side.
  One retry expands unresolved neighborhoods to four neighbors. Raw scenes are
  boundaries. Each completion is capped at 4,096 tokens and each request at 45
  seconds, with SDK transport retries disabled for these calls.
- The model returns only text patches. Indices and raw flags are authoritative
  server data. Duplicate/out-of-range patches are rejected. Neighborhoods are
  applied atomically, so a failed redistribution cannot empty a donor scene.
- A source-empty repair must shorten existing narration in its neighborhood.
  This rejects responses that simply append filler sentences to untouched text.
  Source-present omissions may restore missing source meaning without a donor.
- Narration needs a Unicode letter or number; punctuation alone does not pass.
  Structural and raw-text violations are reported but are not sent for repair.
- Timing remains estimated. Anchors are inferred from source text, not verified
  against video. Review remains necessary for semantics and visual alignment.

## API and recovery

`POST /api/projects/{id}/script/repair` accepts `script_json` and `target_language`.
It returns `run_id`, the candidate `script_json`, and a `repair_report` containing
status, attempts, before/after changes, unresolved indices, source gaps, and issues.

`POST /api/projects/{id}/script/repair/review` accepts the same fields plus `run_id`.
It validates and persists the reviewed text without another model call. Source or
language mismatches are rejected. Automation emits `script_repair` progress and
includes the report on its `script_ready` or recoverable `error` event.

Run directories retain `draft.original.json`, `draft.json`, `repair.json`, and
`draft_state.json`. `script.json` contains only validated script data. Latest-run
recovery distinguishes unresolved drafts, required review, and validated scripts;
empty run directories no longer hide earlier drafts. Source fingerprints prevent
restoring stale drafts or audio after transcription changes.

## Verification

`backend/tests/test_script_repair.py` covers validation, bounds, atomic changes,
cost controls, cancellation, persistence, and source changes. Its source-gap
fixtures come from projects `7f03a59d7302`, `27cd1cecf30f`, and `cb5ed12bdee5`.
Their failed drafts were not saved by the old implementation, so those output
fixtures are explicitly reconstructed.

`frontend/e2e/script-repair.spec.ts` covers automation, paste, review, reload,
retry, stale responses, and audio invalidation with mocked APIs.

A small live assessment in French, English, and Spanish filled both gaps in each
case in one call. Raw text and narration beyond the raw boundary stayed unchanged;
the action verbs stayed on their source indices. Repairs redistributed object
phrases across adjacent cuts, which still needs visual review. Combined narration
remained coherent, with no added events in the retained samples. Inputs and model
responses are in `backend/tests/fixtures/script_repair_languages.json`; this is
three representative cases, not a measured production success rate.
