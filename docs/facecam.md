# Facecam

Facecam is a separate project mode for anime sources. Create it beside Duplicate on the ordinary script page. The confirmation opens an independent French project using Minimal (`zoomed`) and GPT. It keeps upstream source matching but starts with fresh editorial, recording, scheduling, and delivery state.

## Workflow and persisted data

Choose/edit one of eight hooks and eight opinions, generate, edit the full script, approve, record, review transcription discrepancies, prepare the mixed audio preview, and process. Protected original-dialogue markers cannot be removed. The fixed closing phrase introduces a visual title reveal; do not speak the anime title.

`facecam/state.json` stores revisioned drafts, choices, research citations, approvals, word assignments, recording hashes, the silence-cut map, alignment references, preview identity, and VPS delivery status. `facecam/reference*.json` retain the original script and matches even when approval creates extra hook/outro pipeline scenes. Research runs automatically after generation and otherwise only on request. Contradictions and uncertainty are advisory. Edited revisions lose stale annotations; failed research never counts as a successful check.

One frame-quantized cut map drives both silent camera clips and narration. Original-dialogue gaps are inserted into this common timeline. Human speech is neither sped up nor passed through the TTS voice alteration stage. Replacing the recording or editing narration invalidates dependent results. Changing music requires a new mixed preview. Processing/export verifies the current revision and settings.

The AniList identity and cover remain under `facecam/`, outside `output/`. Ambiguous searches require a selection or uploaded image. The final card is 1080×1920 with a readable title, occupies V3 for 60 frames at 60 fps, and follows all camera/narration clips.

## Editor delivery

Facecam exports `SPM_facecam_<anime>_<project_id>.zip` with ZIP64, streamed files, complete episodes, original recording, silent normalized camera, real narration, music, cover/title card, presets, expanded subtitle MOGRTs/timings, JSX, Windows launcher, script, and French instructions. Media references are checked before delivery. It bypasses shared-source prewarming, folder synchronization and Premiere Link. Only the ZIP is uploaded or updated; editor files are preserved.

Install the independent [Facecam Import extension](../premiere-extension/facecam-import/README.md). Build its distributable with `python scripts/package_facecam_extension.py`. Extracted projects must work without shared source folders or the primary extension. Premiere and its Facecam Import panel must be open.

Only the individual project folder receives an `anyone` / `writer` permission with `allowFileDiscovery=false`. Parent permissions are never modified. This is edit/delete/upload access within the project folder.

The VPS stores delivery tasks in its persistent data volume under `facecam/`. Its Discord interaction handler routes persisted task IDs after restart. Confirmation checks MP4s only in the registered folder root, rejects zero/multiple/empty files privately, renames the sole file `output.mp4`, records completion before updating Discord, and disables the button. Per-project locks serialize clicks; retries recover partial failures. Re-export retains completion. “Demander un nouveau montage” archives the old file under `montages_precedents` and reopens the task.

The local project manager synchronizes delivery readiness from the VPS. Completion does not publish or schedule anything. Account YAML `supported_types` requires explicit `facecam` opt-in; anime-only accounts remain incompatible. Source library operations still use `anime`.

## Configuration and rollout

Deploy `server/` first, preserving its data volume. Set these on the VPS (never in the editor extension):

- `ATR_FACECAM_DRIVE_CLIENT_ID`
- `ATR_FACECAM_DRIVE_CLIENT_SECRET`
- `ATR_FACECAM_DRIVE_REFRESH_TOKEN`
- Optional `ATR_DISCORD_FACECAM_CHANNEL_ID`

The Drive credentials must access the project folders. Without an explicit channel ID, the bot finds or creates `montage-facecam` in the configured guild and needs the corresponding Discord permissions. Authenticated `GET /api/internal/facecam/health` verifies Drive access and resolves the channel before any package delivery.

Then update the local backend/frontend and distribute Facecam Import. Both active/example GPT presets use Astra with low reasoning for the big tier and Luna for the light tier. Ordinary generation uses Chat Completions. Research uses Responses with `openrouter:web_search`, preserving returned citations. The explicit Astra output budget avoids reserving the provider's entire maximum response. Insufficient OpenRouter credits surface as retryable generation errors.

## Validation

Automated tests cover revision conflicts, independent copies, raw boundaries (including first/last scenes), exact ordered word assignment, advisory research failures, recording differences, actual offset-stream normalization, cut-map composition, ZIP contents, folder-scoped ACLs, concurrent Discord confirmations, restart/retry recovery, re-export and reopening. Browser tests cover both eight-choice steps, editing/autosave/reload, preserved raw markers, annotation expiry and manual rechecking.

Acceptance on Windows still requires importing an extracted package into Premiere 2025+ and checking camera visibility, subtitle stacking, raw dialogue, one-frame A/V tolerance, and the exact 60-frame cover. This cannot be established by Linux unit tests alone.

### Rollout verification — 2026-09-16

VPS backend deployed at `/opt/tiktok/server`, with the existing persistent volume retained. The prior image is tagged `tiktok-server:before-facecam-20260916`; the private backup is under `server/.deploy-backups/facecam-20260916/`. Channel `1549888612722671696` is configured explicitly. Drive authentication and direct channel access pass the deployed preflight.

Verified locally: 267 backend regression tests; 22 Facecam tests; ordinary duplication tests with the spacing feature enabled; Facecam browser creation/editing/selection tests and ordinary script-repair browser tests; production frontend build. The new VPS delivery tests pass. A full VPS suite also exposed one unrelated existing Premiere Link test whose fixed August 2026 expiry date is now in the past (`test_post_while_disconnected_stores_and_marks_waiting`).

Live checks passed for Astra's eight hooks/eight opinions, script writing and exact-word scene assignment, structured metadata, researched citations, and Luna generation. A temporary Drive folder confirmed repeatable writer ACL creation with discovery disabled and unchanged parent permissions; it was removed afterward. No test delivery messages or publication jobs were created.
