# Post for Me — Account Setup Guide

Post for Me (https://www.postforme.dev) is the managed TikTok posting provider used for
automatic TikTok upload & scheduling. Its "Quickstart" projects use Post for Me's own
audited TikTok app, so **you do not need a TikTok developer account, app, or audit** —
public direct posting works as soon as an account is connected.

## 1. Create the account & project

1. Sign up at https://app.postforme.dev (email + password).
2. Create a **Quickstart** project (NOT "White Label" — White Label is the
   bring-your-own-credentials mode, which would require your own TikTok app + audit).
3. Pick the **$10/month** plan (1,000 posts/month). At 10 accounts × 1 post/day you'll
   use ~300/month. There is no free tier.

## 2. Get the API key

1. In the dashboard, open your project → **API Keys**.
2. Create a key and copy it.
3. On the VPS, add it to `/opt/tiktok/server/.env`:

   ```bash
   ATR_PFM_API_KEY="<your key>"
   ```

   The key is used only by the VPS server. Do not put it in the backend `.env` or in
   `config/accounts/config.yaml`.

## 3. Connect each TikTok account

For every TikTok account (currently 4, target 10):

1. In the Post for Me dashboard, choose **Connect account** → **TikTok**
   (platform `tiktok`, not `tiktok_business`).
2. Log in to the TikTok account in the OAuth window and approve. Default posting scopes
   are enough (`user.info.basic`, `video.list`, `video.upload`, `video.publish`).
3. The account appears in the dashboard with an id starting with `spc_`.

Tip — if you prefer doing it via API (e.g. to connect from the phone where the TikTok
session lives), generate the OAuth URL yourself and open it on that device:

```bash
curl -s -X POST https://api.postforme.dev/v1/social-accounts/auth-url \
  -H "Authorization: Bearer $ATR_PFM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"platform": "tiktok", "external_id": "anime_fr"}'
# → {"url": "https://…", "platform": "tiktok"}   open the url on the device
```

List connected accounts and their `spc_` ids:

```bash
curl -s "https://api.postforme.dev/v1/social-accounts?platform=tiktok" \
  -H "Authorization: Bearer $ATR_PFM_API_KEY"
```

## 4. Fill the accounts config

For each account in `config/accounts/config.yaml`, add the id to the `tiktok:` block:

```yaml
anime_fr:
  # …
  tiktok:
    slots:
      - "13:00"
    post_for_me_account_id: "spc_..."   # ← from step 3
    # optional overrides (defaults shown):
    # privacy_status: "public"
    # allow_comment: true
    # allow_duet: true
    # allow_stitch: true
```

Accounts without `post_for_me_account_id` are skipped at upload time with an explicit
"no Post for Me account configured" status — nothing fails silently.

## 5. Smoke test

After deploying the server with the key configured:

1. Run a normal upload for a test project with only `tiktok` requested, with a slot a
   few minutes ahead.
2. Watch the Discord embed: the TikTok line should go ⏳ → ✅ with the published
   TikTok URL at slot time.
3. Verify the video on TikTok: public visibility, comments/duet/stitch as configured.

## Notes & limits

- **TikTok daily caps:** TikTok limits API posts per creator per 24h (provider-
  independent). At 1 post/day/account this is never an issue.
- **Token health:** if a TikTok session is revoked (password change, security event),
  the account shows `disconnected` in Post for Me and publishes fail with a clear
  error → the server pings Discord after 5 attempts. Reconnect via step 3 (same
  `spc_` id is kept on reconnect).
- **Do not enable `is_draft`:** drafts land in the TikTok inbox and require manual
  in-app publishing — the exact flow we're removing.
- API reference: https://api.postforme.dev/docs
