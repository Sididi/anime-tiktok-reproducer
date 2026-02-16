# Upload Automation Setup Guide

This guide is a concrete runbook to obtain every credential required by this project:

- Discord webhook
- Google OAuth refresh tokens (Drive + YouTube, can be split)
- Google Drive parent folder ID
- Meta tokens/IDs for Facebook + Instagram (two supported modes)

It is written to match the current backend implementation and helper scripts in `scripts/`.

## 1. Token Strategy (Choose First)

You must choose one Meta token mode before filling `.env`:

1. `system_user` (recommended)
2. `long_lived_user` (auto-refresh in app, but still user-token based)

`system_user` is the closest to "setup once and run forever" because it avoids user-session token decay.

## 2. Prerequisites

1. You can run project Python from repository root.
2. `.env` exists at project root.
3. Required APIs/products enabled:
   - Google Drive API
   - YouTube Data API v3
   - Meta Graph API products for Facebook + Instagram

If you use `pixi`, prefer running scripts with:

```bash
pixi run python scripts/<script_name>.py ...
```

## 3. Scripts Added For Token Setup

- `scripts/google_oauth_refresh_token.py`
  - Runs local OAuth browser flow and supports `--target drive|youtube|shared`.
  - Uses shared OAuth client values (`ATR_GOOGLE_CLIENT_ID`, `ATR_GOOGLE_CLIENT_SECRET`, `ATR_GOOGLE_TOKEN_URI`).
  - Prints target-specific refresh token env keys.
  - Verifies Drive user and/or YouTube channel based on requested scopes.
- `scripts/google_drive_folder_setup.py`
  - Finds/creates a root Drive folder and prints `ATR_GOOGLE_DRIVE_PARENT_FOLDER_ID`.
- `scripts/meta_token_helper.py`
  - `exchange-user-token`: short-lived/initial user token -> long-lived user token
  - `resolve-page-assets`: resolve page token + IG business ID from user token
  - `resolve-from-page-token`: resolve IDs directly from a page token
  - `debug-token`: call Meta `/debug_token`
  - `verify`: verify page token + IG token/ID pair

## 4. Exact `.env` Keys You Must End With

Always required:

- `ATR_DISCORD_WEBHOOK_URL`
- `ATR_GOOGLE_CLIENT_ID`
- `ATR_GOOGLE_CLIENT_SECRET`
- `ATR_GOOGLE_TOKEN_URI` (normally `https://oauth2.googleapis.com/token`)
- `ATR_GOOGLE_DRIVE_REFRESH_TOKEN`
- `ATR_GOOGLE_YOUTUBE_REFRESH_TOKEN`
- `ATR_GOOGLE_DRIVE_PARENT_FOLDER_ID`
- `ATR_YOUTUBE_CATEGORY_ID`
- `ATR_YOUTUBE_CHANNEL_ID` (strongly recommended if the Google account has multiple channels)
- `ATR_META_GRAPH_API_VERSION`
- `ATR_META_TOKEN_MODE`
- `ATR_FACEBOOK_PAGE_ID`
- `ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID`

Required when `ATR_META_TOKEN_MODE=system_user`:

- `ATR_FACEBOOK_PAGE_ACCESS_TOKEN`
- `ATR_INSTAGRAM_ACCESS_TOKEN` (optional in code, but recommended to set explicitly)

Required when `ATR_META_TOKEN_MODE=long_lived_user`:

- `ATR_META_APP_ID`
- `ATR_META_APP_SECRET`
- `ATR_META_USER_ACCESS_TOKEN`
- `ATR_META_USER_ACCESS_TOKEN_EXPIRES_AT` (recommended)

### 4.1 Recommended Values Guide

Use this as the practical baseline for your `.env`:

- `ATR_INSTAGRAM_PUBLISH_TIMEOUT_SECONDS`
  - Role: max wait time for IG container status to reach `FINISHED`.
  - Recommended: `900` (15 min).
  - Increase to `1200-1800` if videos are heavier/longer and you hit timeout.
  - Do not set too low (`<300`) unless you accept frequent false timeouts.

- `ATR_INSTAGRAM_PUBLISH_POLL_INTERVAL_SECONDS`
  - Role: delay between IG status polls.
  - Recommended: `5`.
  - Safe range: `3-10`.
  - Lower = faster detection but more API calls; higher = slower feedback.

- `ATR_META_USER_TOKEN_REFRESH_LEAD_SECONDS`
  - Role: in `long_lived_user` mode, refresh user token this many seconds before expiry.
  - Recommended: `604800` (7 days).
  - Typical range: `259200` (3 days) to `1209600` (14 days).
  - Ignored in `system_user` mode.

- `ATR_META_USER_ACCESS_TOKEN_EXPIRES_AT`
  - Role: seed expiry timestamp for `long_lived_user` token lifecycle.
  - Format: ISO datetime UTC, e.g. `2026-03-01T12:00:00+00:00`.
  - Recommended:
    - `long_lived_user`: set it from `meta_token_helper.py exchange-user-token` output.
    - `system_user`: leave empty.

- `ATR_META_GRAPH_API_VERSION`
  - Role: Meta Graph base version used by backend calls.
  - Recommended now: `v22.0` (current project default).
  - Keep pinned; update intentionally (not automatically) and retest flows when bumping.

- `ATR_YOUTUBE_CATEGORY_ID`
  - Role: YouTube metadata category on upload.
  - Current default: `22` (`People & Blogs`).
  - For anime edit/repost style content, many teams prefer `24` (`Entertainment`).
  - Recommended baseline: keep `22` initially; switch to `24` if it better matches your content policy.

- `ATR_YOUTUBE_CHANNEL_ID`
  - Role: explicit target channel guardrail for uploads.
  - Why: one Google account can own/access multiple YouTube channels; without this guard you can upload to the wrong one.
  - Recommended: always set it when more than one channel appears in script verification output.
  - Format: channel id like `UCxxxxxxxxxxxxxxxxxxxxxx`.

## 5. Google Credentials Runbook

### 5.1 Create OAuth client in Google Cloud

1. Create/select Google Cloud project.
2. Enable Drive API + YouTube Data API v3.
3. Configure OAuth consent screen.
4. Create OAuth client credentials and copy:
   - client id -> `ATR_GOOGLE_CLIENT_ID`
   - client secret -> `ATR_GOOGLE_CLIENT_SECRET`

### 5.2 Generate refresh token with script

Run:

```bash
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target drive
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target youtube
```

If your authorized redirect URI in Google Cloud uses another host/port, run (for each target):

```bash
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target drive --host 127.0.0.1 --port 8080
pixi run python scripts/google_oauth_refresh_token.py --env-file .env --target youtube --host 127.0.0.1 --port 8080
```

What this does:

1. Opens browser consent flow.
2. Requests target-specific scopes by default:
   - `--target drive`:
     - `https://www.googleapis.com/auth/drive`
   - `--target youtube`:
     - `https://www.googleapis.com/auth/youtube.upload`
     - `https://www.googleapis.com/auth/youtube.force-ssl`
   - `--target shared`:
     - `https://www.googleapis.com/auth/drive`
     - `https://www.googleapis.com/auth/youtube.upload`
     - `https://www.googleapis.com/auth/youtube.force-ssl`
3. Prints verified account/channel and the exact `.env` lines to paste.
4. If multiple YouTube channels are listed, copy one id into:
   - `ATR_YOUTUBE_CHANNEL_ID=<target_channel_id>`

If it says no refresh token was returned:

1. Remove app access in your Google account security page.
2. Re-run script.

If you get `Erreur 400: redirect_uri_mismatch`:

1. Copy the `client_id` and redirect URI printed by the script.
2. In Google Cloud Console, open that exact OAuth client ID.
3. If it is a `Web application` client, add the exact URI printed by the script to Authorized redirect URIs.
4. If it is a `Desktop app` client, use script defaults (`--host 127.0.0.1 --port 8765`) and replace `.env` with this Desktop client ID/secret.
5. Re-run script.

### 5.3 Create or find Drive parent folder

Run:

```bash
pixi run python scripts/google_drive_folder_setup.py --env-file .env --create-if-missing
```

For a nested target folder (example `Mon Drive > Tiktok > Anime SPM`), run:

```bash
pixi run python scripts/google_drive_folder_setup.py --env-file .env --folder-path "Tiktok/Anime SPM" --create-if-missing
```

Copy output into:

- `ATR_GOOGLE_DRIVE_PARENT_FOLDER_ID=<id>`

## 6. Meta Credentials Runbook

You can run this section with either `system_user` or `long_lived_user`.

### 6.1 Required Meta permissions

For Facebook:

- `pages_manage_posts`
- `pages_read_engagement`
- `pages_show_list`

For Instagram:

- `instagram_basic`
- `instagram_content_publish`

### 6.2 Mode A: `system_user` (recommended)

Manual setup in Meta Business Manager:

1. Create/use Business portfolio.
2. Create Meta app with required products.
3. Create system user (admin system user).
4. Assign your Facebook Page asset to system user with required permissions.
5. Generate system user access token with required scopes.

Then resolve IDs/tokens:

```bash
pixi run python scripts/meta_token_helper.py resolve-page-assets --env-file .env --user-token "<SYSTEM_USER_OR_PAGE_MANAGEABLE_TOKEN>" --page-id "<YOUR_PAGE_ID>"
```

If `/me/accounts` is not available for your token, use direct page-token resolution:

```bash
pixi run python scripts/meta_token_helper.py resolve-from-page-token --env-file .env --page-id "<YOUR_PAGE_ID>" --token "<PAGE_TOKEN_OR_SYSTEM_USER_TOKEN>"
```

If you get `code=100 / subcode=33`:

1. Verify `<YOUR_PAGE_ID>` is the Facebook Page ID (not Instagram business ID).
2. In Meta Business Settings, assign this Page asset to your system user.
3. Regenerate the system user token with required permissions.
4. Retry the command above.

Copy output into `.env`:

- `ATR_META_TOKEN_MODE=system_user`
- `ATR_FACEBOOK_PAGE_ID=...`
- `ATR_FACEBOOK_PAGE_ACCESS_TOKEN=...`
- `ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID=...`
- `ATR_INSTAGRAM_ACCESS_TOKEN=...`

Verify:

```bash
pixi run python scripts/meta_token_helper.py verify --env-file .env
```

### 6.3 Mode B: `long_lived_user`

Use this only if system user is not possible yet.

1. Obtain initial user token via Meta auth flow (with permissions listed above).
2. Exchange to long-lived user token:

```bash
pixi run python scripts/meta_token_helper.py exchange-user-token --env-file .env --app-id "<APP_ID>" --app-secret "<APP_SECRET>" --user-token "<INITIAL_USER_TOKEN>"
```

3. Resolve page token + IG business ID:

```bash
pixi run python scripts/meta_token_helper.py resolve-page-assets --env-file .env --user-token "<LONG_LIVED_USER_TOKEN>" --page-id "<YOUR_PAGE_ID>"
```

4. Fill `.env`:
   - `ATR_META_TOKEN_MODE=long_lived_user`
   - `ATR_META_APP_ID=...`
   - `ATR_META_APP_SECRET=...`
   - `ATR_META_USER_ACCESS_TOKEN=...`
   - `ATR_META_USER_ACCESS_TOKEN_EXPIRES_AT=...`
   - `ATR_FACEBOOK_PAGE_ID=...`
   - `ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID=...`

Notes:

1. Backend auto-refreshes user token and persists state to `backend/data/meta_token_state.json`.
2. Backend derives page access token via `/me/accounts`.

## 7. Discord Webhook

1. Create webhook in target Discord channel.
2. Set:

- `ATR_DISCORD_WEBHOOK_URL=<full webhook url>`

## 8. Final Validation

Start backend, then call:

```bash
curl -s http://127.0.0.1:8000/api/integrations/health | jq
```

Expected:

1. `status` is `ok` or `partial`.
2. `checks.google_drive.status` is `ok`.
3. `checks.youtube.status` is `ok`.
4. `checks.meta.status` is `ok`.
5. `checks.discord.status` is `ok` if webhook is configured.

If any check is `error`, fix env/token and restart backend (health runs once per server process).

## 9. Production Checklist

1. `.env` has all required keys for chosen mode.
2. `/api/integrations/health` has no `error` checks.
3. Run one upload end-to-end with a test project.
4. Confirm Discord receives generation and upload messages.
5. Store tokens securely and rotate app secrets if exposed.

## 10. Official Documentation

- Google Drive API overview: https://developers.google.com/workspace/drive/api/guides/about-sdk
- Google Drive folder management: https://developers.google.com/workspace/drive/api/guides/folder
- YouTube videos insert: https://developers.google.com/youtube/v3/docs/videos/insert
- YouTube captions insert: https://developers.google.com/youtube/v3/docs/captions/insert
- Meta Facebook Video API: https://developers.facebook.com/docs/video-api
- Meta Instagram content publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing
- Discord webhook API: https://discord.com/developers/docs/resources/webhook
