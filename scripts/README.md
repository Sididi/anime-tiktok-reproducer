# Token Setup Scripts

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
```
