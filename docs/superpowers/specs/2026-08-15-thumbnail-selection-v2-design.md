# Thumbnail Selection v2 — Design

**Date:** 2026-08-15
**Status:** Approved by owner (design conversation 2026-08-15)
**Supersedes/extends:** `2026-08-11-thumbnail-selection-design.md` (v1, shipped)

## Purpose

Three improvements to the shipped thumbnail-selection feature:
1. More candidates (up to 11, covering scenes 4–6 starts and the last three scene ends).
2. Visible download progress for the output.mp4 source cache (floating mini-card).
3. **Clean covers**: candidate frames extracted from the *source episodes*
   (no baked-in hook/subtitles), composed into 9:16 covers, delivered as
   images to every platform that accepts one.

## Background facts (verified)

- Each project's Drive export bundle contains `sources/` with the source
  episodes used by its scenes (`export_service.py` manifest).
- Single-frame extraction from a Drive-hosted mp4 without a full download
  works via ffmpeg HTTP range-reads: `-ss <t>` against the authorized
  `https://www.googleapis.com/drive/v3/files/{id}?alt=media` endpoint with a
  Bearer header (~3–8 MB per frame for HEVC 5–12s-GOP episodes).
- `matches.json` (`SceneMatch`) provides per-scene `episode`,
  `start_time`/`end_time` in the **episode timeline**, and `speed_ratio`.
- Platform cover matrix (all verified in production this week):
  YouTube/Facebook accept image files locally; Instagram accepts `cover_url`
  (public image URL, wins over `thumb_offset`); PFM TikTok business accepts
  `thumbnail_url`; TikTok personal accepts ONLY `thumbnail_timestamp_ms`.

## Design

### 1. Candidate set (up to 11)

| # | Anchor | Position |
|---|---|---|
| 0 | Scène 1 | début |
| 1 | Scène 1 | milieu |
| 2 | Scène 1 | fin |
| 3–7 | Scènes 2, 3, 4, 5, 6 | début |
| 8–10 | dernière, avant-dernière, avant-avant-dernière scène | fin |

- Candidates are **scene-anchored**: `(scene_index, position)` with
  `position ∈ {start, mid, end}`.
- Scenes that don't exist are dropped; duplicate `(scene_index, position)`
  pairs are deduped (short videos shrink the set naturally; a 1-scene video
  yields 3 candidates).
- Candidate 0 (Scène 1 début) remains the default selection.
- Labels stay French: "Scène N · début/milieu/fin", "Dernière scène · fin",
  "Avant-dernière scène · fin", "Avant-avant-dernière scène · fin".

### 2. Dual coordinates per candidate

- **Output timestamp** (final timeline, `transcription_timing.json`, 0.05 s
  shift clamped to scene midpoint — unchanged v1 math). Used for: TikTok
  personal cover, IG `thumb_offset` fallback, output-frame fallback
  extraction.
- **Source coordinate** from `matches.json` by `scene_index`:
  `episode` + `start_time`/`end_time` (episode timeline). Position mapping:
  début → `src_start + 0.05 × speed_ratio`; milieu → `(src_start+src_end)/2`;
  fin → `src_end − 0.05 × speed_ratio`; shifts clamp to the source midpoint.
- Raw scenes (`is_raw`) and scenes without a match have no source
  coordinate.

### 3. Extraction ladder (per candidate)

1. **Local episode file** (library path or pure-mode absolute path) →
   `AnimeMatcherService.extract_frame` (cv2, PTS-accurate).
2. **Drive `sources/` fallback** → ffmpeg single-frame range-fetch
   (authorized `alt=media` URL + Bearer header, `-ss` before `-i`,
   `-frames:v 1`). Episode file id resolved by filename inside the
   project's Drive folder.
3. **Output.mp4 frame** at the output timestamp — used for raw scenes,
   unmatched scenes, and any step-1/2 failure. These tiles carry an
   **"aperçu sortie"** badge in the modal (`source: "output"`).

All failures degrade down the ladder, never fatal. Zero extractable
candidates → modal error state with "Continuer sans miniature" (v1
behavior).

### 4. Cover composition (WYSIWYG)

- Every clean 16:9 frame is composed **at extraction time** into a
  **1080×1920 blurred-extend vertical cover**: frame scaled full-width and
  vertically centered; background = the same frame scaled-to-fill, gaussian
  blurred and darkened (the videos' visual language, minus text). PIL.
- The modal tile displays the composed image — what you pick is exactly
  what platforms receive.
- Output-fallback frames are already 9:16 and pass through unchanged.
- Cache layout as v1 (`upload_thumbs/<project>/<version>/`), plus a
  `meta.json` recording per-candidate `{source, output_timestamp_ms}`;
  same per-project build lock and 7200 s eviction.

### 5. Per-platform delivery

| Platform | Cover |
|---|---|
| YouTube | Composed JPEG via existing `thumbnails.set` path (incl. processing-wait, `988582d`) |
| Facebook | Composed JPEG via existing immediate + scheduled-Reel attempt paths (`ea8f539`) |
| Instagram | **`cover_url`** — composed JPEG upserted as `thumbnail_cover.jpg` into the project's Drive folder, `set_public_read`, direct-download URL. Automatic fallback to `thumb_offset` (output timestamp) when hosting fails; VPS publisher retries the container without `cover_url` if Meta rejects the URL |
| TikTok business | PFM media item **`thumbnail_url`** (same Drive URL); fallback `thumbnail_timestamp_ms` |
| TikTok personal | `thumbnail_timestamp_ms` (output timestamp) — platform ceiling, unchanged |

- Upload request gains `thumbnail_candidate_index: int | null` alongside
  `thumbnail_timestamp_ms`. Backend resolves the composed image from the
  candidate cache; if evicted, re-extracts deterministically (same ladder).
- VPS changes: `InstagramPayload.cover_url`, `TikTokPayload.thumbnail_url`,
  publisher plumbing for both. **VPS redeploy required.**

### 6. Progressive modal

- Candidates endpoint returns per-tile state:
  `{state: "ready"|"partial"|"in_progress"|"error", candidates: [...],
  pending: N}` where each candidate carries
  `source: "clean" | "output" | "pending"`.
- Clean tiles are extracted and served **without waiting for output.mp4**
  (local episodes ≈ seconds). Fallback tiles show placeholders and resolve
  when the output cache lands.
- Confirm is enabled as soon as ≥1 tile is ready; polling continues until
  `state: "ready"` or the user confirms.
- Default selection = candidate 0 when ready, otherwise the lowest-index
  ready tile (re-evaluated only until the user clicks a tile themselves).

### 7. Download progress (floating mini-card)

- `upload-source-status` response gains `bytes_done` / `bytes_total`
  (`.part` file size vs Drive metadata size; local-copy case reports
  complete immediately).
- New app-level floating mini-card (bottom-right): visible whenever any
  registered project has a source download in flight — independent of any
  modal (duration modals warm the same cache). One filling bar per project
  with the project title; disappears on completion. Zustand store; the
  existing polling hooks register/unregister project ids.

## Out of scope

- Custom (non-frame) cover images or text overlays on covers.
- Per-platform distinct candidate choices.
- Changing covers after publish (manual/experimental only).
- Non-YPP YouTube Shorts feed coverage (platform-gated).

## Testing

- Unit: candidate-set v2 (11-candidate shape, dedupe, 1/2/4/6/8-scene
  videos), source-coordinate mapping (shift × speed_ratio, mid, clamps),
  extraction-ladder degradation (local → Drive → output → dropped),
  blurred-extend composition (output size 1080×1920, no exception on tiny
  frames), progress fields (bytes math), payload plumbing (candidate index,
  IG cover_url + fallback, TT business thumbnail_url + fallback, TT
  personal unchanged).
- Server (VPS): cover_url container param + retry-without-cover_url on
  rejection; PFM body `thumbnail_url` shape.
- Frontend: `tsc -b` + eslint; manual E2E by owner.
- Suites: `pixi run -e dev pytest` (backend), `server/.venv` pytest, never
  two pytest runs concurrently.
