# Pure Mode

Pure mode reproduces one of **our own published TikToks**. It has two media
workflows, selected at creation:

- **Without an original source:** the downloaded TikTok is the only source.
  Cleanup removes existing overlays, then identity matching maps each scene
  to the same time range of that file.
- **With an optional temporary source:** the original footage is copied and
  indexed inside the project. Media operations follow the anime workflow:
  source matching, manual search, zoom search, source audio and gap resolution.
  There is no subtitle-removal step or identity matching.

Both workflows retain Pure prompts, titles, publishing accounts and metadata.
Existing Pure projects keep their original behavior.

## Flow without a temporary source

1. **Project Setup** — select library type `Pure`; no source/series required.
   Paste the TikTok URL and start.
2. **Cleanup** (new phase, `/project/{id}/cleanup`) — the startup job stops
   after download. Draw:
   - exactly one **subtitle zone** (static position; repaired only on frames
     where text is visually detected inside the rect),
   - optional **watermark zones** (static logo or `@username`; repaired for
     the whole video — noticeably slower).
   Use *Preview at playhead* (~4 s inpaint) to validate rectangle + quality,
   then *Launch full cleanup*. The cleaned video becomes `tiktok_clean.mp4`
   and replaces `project.video_path`; the raw download is preserved in
   `project.original_video_path`. *Skip cleanup* continues on the raw file.
3. **Scene detection** — Pure uses a confident-cuts detector
   (`threshold=27`, no sensitive reinject, no auto-dense, no SSCD tiebreak)
   because there is no automatic scene merging to absorb false cuts. Fix
   missed cuts with the split tool.
4. **Matching** — instant identity matches, auto-confirmed. Manual
   merge-with-previous still works (identity re-match). Manual episode search
   and alternatives are hidden (no library to search).
5. Transcription → raw scenes (kept: a raw scene's audio range in the tiktok
   IS the original raw audio) → script → processing → export, unchanged.

## Flow with a temporary source

1. In **Project Setup → Pure**, optionally provide either a single video URL
   (YouTube or another site supported by the downloader) or a folder accessible
   on the backend machine. This is separate from the TikTok URL to reproduce.
   The folder selector uses the existing server-side browser.
2. Folder import includes compatible videos directly inside that folder,
   excluding subfolders. The files are **copied**, and the originals are left
   untouched. A URL must identify one video; playlists and channels are rejected.
   Source downloads are capped at 1080p (video height), preferring H.264 video
   and AAC audio at the best available resolution within the cap. Compatible
   streams can be copied/remuxed instead of transcoding the entire video;
   other codecs still use normal anime import normalization. Local folders
   keep their existing resolution, and completed projects are not rebuilt.
3. The startup job downloads the reference TikTok, prepares the private source,
   and indexes it using the existing SSCD indexer and heavy-work scheduler.
   Index density defaults to **2 fps**; advanced settings offer **1, 2 or 4 fps**.
4. The job then performs anime scene detection (default threshold 16). The
   project becomes openable when preparation and scene detection finish.
5. Matching and subsequent media stages use only this project's index and
   videos. They support the normal anime tools and playback-speed defaults.
   Failed preparation produces an error, never identity-matching fallback.

The source is fixed at creation. **Retry** reuses valid downloads, prepared
files and index checkpoints. Incomplete imports/checkpoints are discarded;
an index explicitly reported as corrupt by the indexer is rebuilt on retry.
Once ready, the project no longer needs the original folder or URL.

### Storage and lifetime

Private data lives under `projects/{id}/temporary_source/`:

- `download/` and `download.json`: URL download and its completed-file marker;
- `library/anime/source/`: prepared media and import/subtitle sidecars;
- `library/anime/.index/` and `.episodes.json`: index and episode manifest;
- `previews/`, `hashes.json` and `ready.json`: private previews, source hash cache
  and readiness signatures.

There is no catalog registration, remote series ID, hydration, Storage Box
upload or Storage Box indexation job. The ordinary Drive export/prewarming
workflow remains available.

Deleting the project through **Project Manager** or the project API stops
preparation, settles source/preview writers, releases the loaded index and
removes the project's local data. Original files outside the project survive.
Duplication copies the private source and index into the new project, rebases
local paths and preserves Pure settings. Each duplicate remains usable after
deleting the mother project, with no shared temporary media to clean up later.

### Code routing

`project.library_type` remains `pure` for editorial and publishing decisions.
Media code uses `project.media_library` (an explicit `LocalLibrary` for a
temporary source) and `project.media_series_name`. Use
`project.uses_identity_source` for cleanup/identity-only behavior, rather than
testing the publishing library type. Never switch global library paths to
select a temporary source.

## Prompts

`backend/prompts/pure/` mirrors `default/` with every `[OEUVRE]` (series
name) reference removed — Pure knows no source work, and prompts instruct the
LLM to never guess or name it. Note the `PromptResolver` LRU cache: restart
the backend after editing prompt files.

## Inpainting subsystem

- Model: **ProPainter** (temporal video inpainting), vendored at
  `modules/propainter` (see `VENDORED.md` there; pinned commit, trimmed).
- Weights (~200 MB total: `ProPainter.pth`, `recurrent_flow_completion.pth`,
  `raft-things.pth`) are downloaded on first use from the official v0.1.0
  GitHub release into `backend/data/models/propainter/`. Manual download into
  that folder also works.
- The job reserves the **whole heavy-slot budget** (like fast matching): the
  models need the full 8 GB card.
- VRAM strategy: only a crop around each rect (+margin, ×16-aligned) is fed
  to the model; crops longer than 512 px are downscaled for the model and the
  repair is upscaled back (confined to the mask). OOM ladder:
  `subvideo_length` 80→40→24, then ×0.75 downscale steps, then a hard error.
- Text-presence detection (subtitle zones): white-text candidate mask
  (luma ≥ 190, saturation ≤ 60) gated by outline contrast, hysteresis
  (on 0.3 % / off 0.1 % of rect area), gap-close ≤ 4 frames, island-drop
  ≤ 2, span pad ± 3. Constants at the top of
  `backend/app/services/video_cleanup_service.py`.
  `PURE_CLEANUP_FULL_RECT_MASK=1` switches to full-rect masks per active
  frame if the text-mask union ever misbehaves.
- Subtitle masks are **per frame**: each active frame's mask is the union of
  the raw text masks over a ±5-frame window, dilated. Tight per-frame holes
  let the model propagate the real background revealed when the text changes
  (a per-clip union mask degenerates into a hallucinated full band). The
  skipped-frame stride blend is restricted per pixel to where each neighbour
  model frame was actually repaired; the composite alpha is eroded 2 px then
  feathered (σ 2.5) so the blend band sits inside the repaired margin.
- Per-clip results are cached under `{project}/cleanup/spans/` — re-running a
  failed job resumes from the cached clips. Cache filenames carry a
  `_v{CLIP_CACHE_VERSION}` suffix; bump it whenever mask/fill semantics
  change so stale clips are ignored.
- Assembly: raw frames piped to ffmpeg (NVENC `constqp 19`, libx264 CRF 16
  fallback), audio stream-copied from the original, BT.709 tags fixed.

## Operational notes

- **YouTube sources**: `pixi install --locked` installs yt-dlp's EJS solver
  and Deno runtime. The downloader resolves the managed runtime explicitly.
  YouTube metadata checks and downloads are anonymous from the first request:
  browser cookies and external yt-dlp configuration are disabled for YouTube.
  Videos requiring a signed-in account fail without an authenticated retry.
  TikTok keeps its configured browser cookies and existing format selection.
- **Accounts**: add `"pure"` to `supported_types` of the publishing accounts
  in `config/accounts/config.yaml`, otherwise the project manager shows no
  compatible account for Pure projects.
- The anime_searcher submodule enum intentionally does NOT know `pure`.
  Temporary sources reach it as an anime library with a project-owned path.
- `einops` was added to `pixi.toml` for ProPainter (run `pixi install`).
- Classic Pure duplication: the `cleanup/` working dir is excluded; `tiktok_clean.mp4` is
  hardlinked and all absolute paths (project fields + match episodes) are
  rebased into the duplicate's directory.
