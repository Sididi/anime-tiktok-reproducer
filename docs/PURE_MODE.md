# Pure Mode

Pure mode reproduces one of **our own published TikToks from its downloaded
output**: the tiktok itself is the only source. There is no series, no index,
no hydration — matching is an identity mapping (scene N → the same time range
of the tiktok file), and the script/TTS/subtitles/overlays/metadata are
regenerated as usual.

## Flow

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

- **Accounts**: add `"pure"` to `supported_types` of the publishing accounts
  in `config/accounts/config.yaml`, otherwise the project manager shows no
  compatible account for Pure projects.
- The anime_searcher submodule enum intentionally does NOT know `pure` —
  Pure never reaches indexing or the searcher.
- `einops` was added to `pixi.toml` for ProPainter (run `pixi install`).
- Duplication: the `cleanup/` working dir is excluded; `tiktok_clean.mp4` is
  hardlinked and all absolute paths (project fields + match episodes) are
  rebased into the duplicate's directory.
