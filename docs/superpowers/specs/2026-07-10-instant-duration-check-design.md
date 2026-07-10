# Instant upload duration check + shared preview download

Date: 2026-07-10
Status: Approved

## Problem

The upload "Checking" phase (copyright → Facebook duration → YouTube duration) can
take minutes. The cause is **not** sped-up transcoding — that was removed in
`3cf6907` (2026-04); both duration modals already preview the sped-up version with
browser-native `video.playbackRate`. The real cost is in
`UploadPhaseService._check_platform_duration`
(`backend/app/services/upload_phase.py`): to ffprobe the duration and serve the
modal previews, it downloads the **entire final video from Google Drive** into a
per-platform prep dir — once for the Facebook check and again for the YouTube
check, sequentially, even when the video is under the limit. The upload job later
downloads the same file a third time.

## Goals

- "Checking" completes in ~1s regardless of video length.
- The duration-choice modals open instantly; Cut / Speed-up / Skip are usable
  immediately even before previews are playable.
- The final video is downloaded from Drive at most once per project across
  Facebook check, YouTube check, and the upload job.

## Design

### 1. Duration without download

In `_check_platform_duration`:

- If a LAN-local video exists (`readiness.local_video_path`): ffprobe it in place
  (no copy into a prep dir).
- Else: new `GoogleDriveService.get_video_metadata(file_id)` calling
  `files().get(fileId=..., fields="videoMediaMetadata(durationMillis),size,name",
  supportsAllDrives=True)` and returning duration in seconds.
- Fallback: if `videoMediaMetadata.durationMillis` is absent (Drive may not have
  processed a fresh upload yet), fall back to the legacy blocking path: download
  into the shared source cache (below) and ffprobe.

The check's response shape (`needed`, `duration_seconds`, `speed_factor`,
`sped_up_available`) is unchanged.

### 2. Shared source-video cache + background download

- One per-project cache dir (e.g. `upload-prep/{project_id}/source/`) replaces the
  two per-platform copies of the *original* video. `sped_up.mp4` handling at
  upload time is untouched.
- When a check returns `needed=true`, the backend starts a **background** download
  of the final video into the shared cache. A per-project in-flight lock
  deduplicates concurrent triggers (Facebook then YouTube check, retries).
- The `facebook-preview/{version}` and `youtube-preview/{version}` routes serve
  the `original` version from the shared cache. While the download is in flight
  they return **HTTP 202**; on failure they return an error status. The modals
  poll (~2s) and show a loading placeholder over the two video slots until 200.
- The upload job (`upload_phase.py` ~line 868) copies from the shared cache when
  the file is already present instead of re-downloading from Drive.
- The existing stale-prep cleanup mechanism (`_cleanup_stale_prep_cache`) covers
  the new dir with a max age.

### 3. Cleanup

- Fix the stale docstring on `facebook_duration_check`
  (`backend/app/api/routes/project_manager.py`) claiming the sped-up version is
  pre-generated during the check.

## Error handling

- Background download failure: preview endpoints report the error; modals show
  "aperçu indisponible" but the choice buttons keep working. The duration check
  itself never fails because of preview problems.
- Drive metadata missing: transparent fallback to download+probe (slow but
  correct, and now single-download).

## Testing

- Unit tests (mocked Drive): metadata-based duration path (under/over limit,
  missing metadata fallback), preview endpoint state machine (202 while in
  flight, 200 when cached, error on failure), download deduplication.
- Manual E2E: Drive-only project over 90s — modal opens instantly, previews load
  after single background download, upload reuses cache.
