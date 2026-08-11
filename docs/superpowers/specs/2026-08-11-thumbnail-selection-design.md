# Thumbnail Selection — Design

**Date:** 2026-08-11
**Status:** Approved by owner (design conversation 2026-08-11)

## Purpose

Add a "Thumbnail selection" step to the video upload flow. Before an upload is
enqueued, a modal shows 5 candidate thumbnails (frames from the final rendered
video); the chosen frame becomes the video's thumbnail/cover on as many
platforms as each platform's API allows.

## Platform support matrix (verified 2026-08-11)

| Platform | Publish path today | Thumbnail mechanism | Support |
|---|---|---|---|
| TikTok personal (`tiktok`) | VPS → Post for Me | `thumbnail_timestamp_ms` on the PFM media item | Full (timestamp is the consumer API's ceiling; no image option exists) |
| TikTok business (`tiktok_business`) | VPS → Post for Me | same `thumbnail_timestamp_ms` | Full (also supports `thumbnail_url`, not used — timestamp is equivalent for our frames) |
| Instagram Reels | VPS → Graph API | `thumb_offset` (ms) on the media container | Full — VPS plumbing already exists (`InstagramPayload.thumb_offset`), just never set by backend |
| YouTube Shorts | Local → googleapiclient | `thumbnails.set()` with a JPEG | Real since 2026-07-24 for YouTube Partner Program channels (official rollout); best-effort/no-op elsewhere. Non-fatal. |
| Facebook (immediate) | Local → Graph `/videos` | `thumb` multipart image param | Yes |
| Facebook (scheduled) | Local → Reels API `/video_reels` | none documented | Not supported — keep auto thumbnail, note in result detail |

Key references: PFM media schema (`thumbnail_url` / `thumbnail_timestamp_ms`
per media item, https://www.postforme.dev/resources/customizing-video-thumbnails);
TikTok direct-post `video_cover_timestamp_ms`; YouTube Shorts custom thumbnails
announcement (blog.youtube, 2026-07-24, Studio-first, YPP-only for now);
`thumbnails.set` docs list no Shorts restriction.

Note: Post for Me is used **only** for TikTok. YouTube, Facebook, and Instagram
use native API integrations (Instagram published by the VPS, YouTube/Facebook
locally).

## Chosen approach: timestamp-first hybrid

The selection primitive is a **timestamp in the final output video**
(`thumbnail_timestamp_ms`). Timestamp-native platforms (TikTok, Instagram) get
the timestamp; image-native platforms (YouTube, Facebook) get a JPEG frame
extracted server-side from the original output.mp4 at that timestamp.

Rejected alternatives:
- **Image-everywhere** — TikTok personal cannot accept images; forces image
  hosting for VPS-published platforms; no visual gain since candidates are
  frames of the video itself.
- **Client-side canvas capture** — compressed-preview quality, browser seek
  imprecision, and the backend still needs server-side extraction for YT/FB.

## Design

### 1. Candidate timestamps (backend)

New `backend/app/services/thumbnail_service.py`, reading the authoritative
final-video scene timeline from `output/transcription_timing.json`
(via `ProjectService`):

| # | Label | Timestamp |
|---|---|---|
| 0 | Scene 1 — first frame | `scene[0].start + shift` |
| 1 | Scene 1 — middle | `(scene[0].start + scene[0].end) / 2` (no shift) |
| 2 | Scene 1 — last frame | `scene[0].end − shift` |
| 3 | Scene 2 — first frame | `scene[1].start + shift` |
| 4 | Scene 3 — first frame | `scene[2].start + shift` |

- `shift` = **3 timeline frames (50 ms at 60 fps)**, snapped via `otio_timing`
  helpers — absorbs off-by-a-frame scene-cut boundaries, visually
  indistinguishable otherwise.
- Fewer than 3 scenes → emit only the computable candidates.
- All timestamps clamped to `[0, video_duration]`.
- Candidate 0 (Scene 1 first frame) is the default.

### 2. API endpoints (backend, `project_manager.py` routes)

- `GET /project-manager/projects/{id}/thumbnail-candidates`
  — ensures the `upload_source` cache (same machinery the duration modals
  use), extracts all candidate frames in **one**
  `AnimeMatcherService.extract_frames` pass (PTS-accurate OpenCV), caches
  JPEGs alongside the upload_source cache, returns
  `[{index, label, timestamp_ms, image_url}]`.
- `GET /project-manager/projects/{id}/thumbnail-frame/{index}.jpg`
  — `FileResponse` of the cached JPEG.

### 3. Frontend modal

- `frontend/src/components/project-manager/ThumbnailSelectionModal.tsx`,
  cloned from the `FacebookDurationModal` pattern (framer-motion
  `AnimatePresence` card, Escape handling, `stacked` prop).
- 5-up grid of 9:16 frame previews with labels; candidate 0 preselected;
  Confirm proceeds.
- Inserted as `awaiting_thumbnail_choice` in `ProjectManagerModal`'s upload
  session machine, **after** the duration checks (source cache already warm)
  and **before** `enqueueUpload`.
- Shown on every upload, immediate and scheduled.
- Skip/close = proceed with the default candidate; the modal never blocks an
  upload.
- Chosen `thumbnail_timestamp_ms` added to `runProjectUpload`'s request body
  (`frontend/src/api/client.ts`).

### 4. Per-platform propagation

- `UploadProjectRequest` gains `thumbnail_timestamp_ms: int | None` →
  `UploadPhaseService.execute_upload`.
- **TikTok:** `_build_tiktok_payload` includes it → `TikTokPayload`
  (`server/app/api/internal.py`) → job → `create_tiktok_post` sets it on the
  PFM media item: `media: [{url, thumbnail_timestamp_ms}]`. Identical for
  both connectors; the mp4 staged to PFM is the untouched output.mp4 so the
  timestamp maps 1:1.
- **Instagram:** `ig_payload_base` sets
  `thumb_offset = round(ts_ms / speed_factor)` where `speed_factor` comes
  from the IG prep result (1.0 when not sped up), clamped to the prepared
  video's duration (cut case). VPS side already fully wired.
- **YouTube:** after a successful insert in `upload_youtube`, call
  `thumbnails().set(videoId, media_body=<frame.jpg>)`. Failures (non-YPP
  channel, missing phone verification, quota) are non-fatal warnings in the
  platform result detail.
- **Facebook:** immediate `/videos` path adds the `thumb` multipart image;
  scheduled Reels path unchanged (unsupported — result detail notes the
  thumbnail was skipped).
- YT/FB frame JPEGs are the same server-side extractions from the original
  output.mp4 (reuse cached candidate JPEGs).

### 5. Error handling

- Missing/unreadable `transcription_timing.json` or video → modal error state
  with an explicit "proceed without thumbnail" path; upload never blocks.
- Single-candidate extraction failure → drop that candidate, keep the rest.
- All platform-side thumbnail failures are non-fatal; they surface as
  warnings in the per-platform result detail (existing partial-failure
  pattern).

### 6. Testing

- Unit — candidate computation: shift application, clamping, <3 scenes,
  mid-scene math, against a synthetic `transcription_timing.json`.
- Unit — Instagram offset scaling: sped_up (`speed_factor > 1`), cut
  (clamping), passthrough (1.0).
- Unit — TikTok payload: `thumbnail_timestamp_ms` present end-to-end in the
  PFM `social-posts` body (existing `post_for_me_publisher` test patterns).
- Unit — YouTube/Facebook: thumbnail failure does not fail the upload.
- Suite runs via `pixi -e dev` (known pre-existing-failure baseline applies).

## Out of scope

- Custom (non-frame) cover images, text overlays on covers.
- Facebook scheduled/Reels covers (no API support).
- Per-platform distinct thumbnail choices (one timestamp for all platforms).
- Changing thumbnails after publish.
