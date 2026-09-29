# Facecam

Facecam is a separate project mode for anime sources. Create it beside Duplicate on the ordinary script page. The confirmation opens an independent French project using Minimal (`zoomed`) and GPT. It keeps upstream source matching but starts with fresh editorial, recording, scheduling, and delivery state.

## Workflow and persisted data

Opening a Facecam does not start generation. Choose the writing model above the story section, then click “Analyser les histoires” to detect the stories present in its source. Model changes are saved before analysis can start. A single story is selected automatically; when several distinct arcs exist, choose one before the hook. Cards show the premise, progression, endpoint and omitted material. Alternative hooks on the same arc are not separate stories. Existing drafts are preserved: detection is explicitly started, and choosing a story resets the draft and its dependent results. Choices persist across reloads; failed detection is retryable.

Then choose/edit one of eight hooks and, optionally, one of eight opinions, generate, edit the full script, approve, record, review transcription discrepancies, prepare the mixed audio preview, and process. “Sans point de vue” clears the selected opinion; an empty opinion omits that block when generating and takes the story directly to the fixed title-reveal phrase. This choice survives reloads and can be changed before regenerating. Retained original-dialogue markers cannot be removed in the text editor. The fixed closing phrase introduces a visual title reveal; do not speak the anime title.

## Narrative generation

The September 27 revision adds an editorial selection before source extraction and writing. Story detection reads the immutable source and selects a subset of clips for each real arc. It may trim the beginning or end of a narration clip using source ASR word indices (start inclusive, end exclusive). Invalid, reversed, duplicated or unknown scene references fail before persisting options. Raw dialogue can be omitted with an unrelated branch, but every retained raw clip stays whole. Scenes without word timings can only be retained whole or omitted.

After selection, a model call builds a neutral factual brief in `facecam/narrative_brief.json` from **only the selected, trimmed source text**. Every retained non-raw scene must be covered exactly once, in order, within its body block. Missing, duplicated or crossed raw boundaries reject the brief before any draft changes. The brief is cached by selected source, block structure and extraction-prompt hash; changed references/prompts or a damaged cache trigger extraction again. Source fingerprints reject stale story selections.

Hooks, opinions and body writing receive this brief and the chosen story's scope rather than the source narration. All eight new hook choices are original proposals; no slot is reserved for translating the source opening. Older saved choices remain readable. Opinions receive the selected hook and focus on a supported narrative interest. The writer uses a concrete premise, protagonist, obstacle, escalating consequences and a payoff to the hook. It can omit descriptive gestures without dropping their useful illustration. Instructions live in `backend/prompts/facecam/{stories,brief,narration,hooks,opinions,script}.md`.

This adds one detection call per source analysis and one cached brief extraction per selected clip set. Both use the configured big-model tier. These are editorial extraction calls, not fact checks: source errors can survive into the brief. The existing researched check still runs after generation. Structural validation does not guarantee correct arc detection, good prose or perfect factual accuracy.

New scripts target 65–70 seconds including retained dialogue and the one-second card. The initial estimate uses 3.35 spoken words/second, including normal pauses. The body budget subtracts the selected hook, optional opinion and fixed ending from the total word budget. An overlong generation receives one targeted condensation retry, then fails without replacing the draft if still too long. Thin source material may yield a shorter text rather than invented padding. Manual edits remain unrestricted, with word count and an estimate shown; the prepared preview reports the actual duration including the card. No recorded voice is sped up. A timed recording is still needed to establish whether a video exceeds one minute.

Approval physically filters and trims the matched episode clips. Word timestamps on the original TikTok clock map proportionally into each confirmed episode interval; the export later snaps these bounds to source frames. This assumes the existing match's linear time mapping, not a fresh visual cut detector. A cut is therefore as accurate as the source ASR and match. Every output scene is reindexed with a contiguous provisional timeline, while `montage_map` retains original scene IDs, source bounds, episode bounds and cut word indices. The immutable `reference*.json` files are not rewritten. Selected clips keep their original relative order. A single sentence may span several retained shots.

Hook/outro illustrations reuse retained footage rather than extending into an excluded event. Automatic gap filling is disabled for these editorial selections so it cannot silently undo a cut; the continuous camera track covers narration when an anime illustration runs short at the playback speed floor. Original dialogue retains native timing. Older drafts without a selected story keep their existing montage behavior.

The research method, observations and implementation tradeoffs are documented in [the French corpus analysis](facecam-narration-analysis-2026-09-26.md). Research media/transcripts remain under ignored `backend/data/research/facecam-2026-09-26/`; production does not retrieve or paste these creators' scripts.

`facecam/state.json` stores revisioned drafts, choices, research citations, approvals, word assignments, recording hashes, the silence-cut map, alignment references, preview identity, and VPS delivery status. `facecam/reference*.json` retain the original script and matches even when approval creates extra hook/outro pipeline scenes. Research runs automatically after generation and otherwise only on request. Contradictions and uncertainty are advisory. Edited revisions lose stale annotations; failed research never counts as a successful check.

Fact checks also receive the actual library subtitle dialogue from the first to the last selected matched interval in each episode, padded by 30 seconds on both sides (all original matches for legacy drafts). Dialogue between selected shots is retained: scenes between 02:00 and 06:00 supply subtitles from 01:30 to 06:30. One text track is selected (French preferred, then English, then another available language). Episode names, source timestamps, original scene anchors, and complete overlapping cues go to both research and annotation steps. The panel exposes this snapshot and allows findings to cite individual dialogue excerpts. Missing/image-only subtitles and context-budget limits are disclosed. Dialogue is not proof of an unseen visual detail, and absence from an excerpt is not treated as a contradiction. Existing drafts are not automatically rechecked after this upgrade.

One frame-quantized cut map drives both silent camera clips and narration. Original-dialogue gaps are inserted into this common timeline. Human speech is neither sped up nor passed through the TTS voice alteration stage. Replacing the recording or editing narration invalidates dependent results. Changing music requires a new mixed preview. Processing/export verifies the current revision and settings.

The AniList identity and cover remain under `facecam/`, outside `output/`. Ambiguous searches require a selection or uploaded image. The final card is 1080×1920 with a readable title, occupies V3 for 60 frames at 60 fps, and follows all camera/narration clips.

Cover lookup searches parenthesized aliases as well as the full library title (for example `Kill Ao (Kill Blue)`), deduplicates AniList identities, and auto-selects only one exact title/synonym match. Processing preflight errors display the server's corrective message and a route back to Facecam.

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

Story-selection tests additionally cover single/multiple arcs, source fingerprints, concurrent edits, failed detection, changing an arc, cuts at both ends of a scene, rejection of raw-word cuts and invalid indices, physical scene removal, coordinate conversion, source immutability, word-budget retries, and reindexed voice/montage correspondence. Browser tests cover explicit detection after model selection, the one-story shortcut, required multi-story choice, retry and preservation of old drafts until a new choice.

The [atomezen comparison](facecam-atomezen-condensation-2026-09-27.md) motivated this rework. A live evaluation against the configured model on September 27 was blocked before generation by OpenRouter HTTP 402 (API key credit limit). This is not a completed model-quality evaluation; deterministic and mocked-model tests do not establish the quality of real story detection.

September 27 checks: 64 Facecam backend tests, 41 related export/timing/duplication regression tests, 10 browser tests, and the production frontend build pass. The duplication tests require `ATR_DUPLICATE_SAME_LANGUAGE_SPACING_ENABLED=true`; the local default disables that unrelated rule, so those tests were rerun with the flag enabled only for the test process. The real source project was also projected deterministically through the internal cut in scene 42: its selected text ends after `de Goblins`, at source time 70.415 s, before the receptionist's refusal. Script, transcription and matches of `d038484047ac` remain unchanged. Research evidence is under ignored `backend/data/research/facecam-stories-2026-09-27/`.

Acceptance on Windows still requires importing an extracted package into Premiere 2025+ and checking camera visibility, subtitle stacking, raw dialogue, one-frame A/V tolerance, and the exact 60-frame cover. This cannot be established by Linux unit tests alone.

### Rollout verification — 2026-09-16

VPS backend deployed at `/opt/tiktok/server`, with the existing persistent volume retained. The prior image is tagged `tiktok-server:before-facecam-20260916`; the private backup is under `server/.deploy-backups/facecam-20260916/`. Channel `1549888612722671696` is configured explicitly. Drive authentication and direct channel access pass the deployed preflight.

Verified locally: 267 backend regression tests; 22 Facecam tests; ordinary duplication tests with the spacing feature enabled; Facecam browser creation/editing/selection tests and ordinary script-repair browser tests; production frontend build. The new VPS delivery tests pass. A full VPS suite also exposed one unrelated existing Premiere Link test whose fixed August 2026 expiry date is now in the past (`test_post_while_disconnected_stores_and_marks_waiting`).

Live checks passed for Astra's eight hooks/eight opinions, script writing and exact-word scene assignment, structured metadata, researched citations, and Luna generation. A temporary Drive folder confirmed repeatable writer ACL creation with discovery disabled and unchanged parent permissions; it was removed afterward. No test delivery messages or publication jobs were created.
