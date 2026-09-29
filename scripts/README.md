# Token Setup Scripts

## Facecam narrative corpus

Run these from the repository root with the project's Python (`pixi run python` or
`.pixi/envs/default/bin/python`). Outputs are local and ignored by Git.

```bash
pixi run python scripts/facecam_corpus.py
pixi run python scripts/analyze_facecam_corpus.py
pixi run python scripts/summarize_facecam_corpus.py
```

`facecam_corpus.py` freezes the latest 75 unique public publications per creator,
sorts by publication timestamp instead of pinned display order, downloads full
videos and runs local French large-v3 transcription with the existing speech
coverage repair. It discovers 15 extra candidates to account for pinned entries.
The original ASR text remains intact alongside an analysis version excluding
theomcx's branded opening. Raw ASR timestamps are segment estimates. Missing or
failed samples are reported and are never silently replaced with older videos.

The default snapshot is `backend/data/research/facecam-2026-09-26`. Re-running
resumes this frozen snapshot. Use `--output <new-directory>` to collect a new one.
Downloads use two workers; ASR runs serially on CUDA (`--device cpu` is available).
The script ignores global yt-dlp configuration and uses the project's existing
non-impersonated TikTok fallback. Detailed download diagnostics stay beside each video.

`analyze_facecam_corpus.py` uses the configured GPT big tier, in batches of three
transcripts, and therefore makes billable API calls. `--watch` can annotate while
transcription runs. Each annotation includes two exact excerpts checked against
its transcript, format, hook, progression, ending, limitations and a transferable
technique. Labels remain model annotations until editorially reviewed. Re-run to
retry failed batches. `summarize_facecam_corpus.py` exports `summary.json` and
`analysis-index.csv`; it distinguishes presentations from lists, promotions and
account announcements. It never interprets public engagement as measured retention.

## Token setup

Run from repository root (preferably through pixi):

```bash
pixi run python scripts/<script>.py ...
```

## Scripts

1. `google_oauth_refresh_token.py`
   - Browser OAuth flow for Google.
   - Supports `--target drive|youtube|shared`.
   - Uses shared `ATR_GOOGLE_CLIENT_ID/SECRET/TOKEN_URI`.
   - Prints target-specific refresh token keys and helps set `ATR_YOUTUBE_CHANNEL_ID`.

2. `google_drive_folder_setup.py`
   - Finds or creates Drive folder (root or nested path).
   - Prints `ATR_GOOGLE_DRIVE_PARENT_FOLDER_ID`.

3. `meta_token_helper.py`
   - `exchange-user-token`: exchange Meta user token to long-lived token.
   - `resolve-page-assets`: resolve page id/token and IG business ID.
   - `resolve-from-page-token`: resolve IDs from known page token.
   - `debug-token`: inspect token details with `/debug_token`.
   - `verify`: verify page + IG credentials.

4. `meta_reel_duration_probe.py`
   - Generates tiny synthetic videos and asks Meta to process them only as
     unpublished Instagram containers or Facebook drafts.
   - Requires `--confirm-nonpublic-uploads`, emits JSON Lines, removes temporary
     Drive files/Facebook drafts, and never prints access tokens.
   - Keep new accounts at the conservative 90-second default until probed.
   - Instagram has no draft publish operation: a `FINISHED` container above the
     documented 900-second ceiling proves ingestion only, not publishability.
     Do not configure above 900 without separate authoritative evidence.

## Examples

```bash
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target drive
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target youtube
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target shared
pixi run python scripts/google_drive_folder_setup.py --env-file .env --create-if-missing
pixi run python scripts/google_drive_folder_setup.py --env-file .env --folder-path "Tiktok/Anime SPM" --create-if-missing
pixi run python scripts/meta_token_helper.py exchange-user-token --env-file .env --app-id "$ATR_META_APP_ID" --app-secret "$ATR_META_APP_SECRET" --user-token "<TOKEN>"
pixi run python scripts/meta_token_helper.py resolve-page-assets --env-file .env --user-token "<TOKEN>" --page-id "<PAGE_ID>"
pixi run python scripts/meta_token_helper.py resolve-from-page-token --env-file .env --page-id "<PAGE_ID>" --page-token "<PAGE_TOKEN>"
pixi run python scripts/meta_token_helper.py verify --env-file .env
pixi run python scripts/meta_reel_duration_probe.py --account-id anime_fr --platform instagram --candidate 899 900 901 --confirm-nonpublic-uploads
```
