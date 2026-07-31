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

## 3. Choose and connect the TikTok API

For every TikTok account:

1. Prefer **TikTok Business API** (Post for Me platform `tiktok_business`) for
   production publishing. It uses TikTok's Accounts/Business API and does not share
   the consumer Direct Post API's active-publishing-user quota.
2. Log in to the existing TikTok profile and approve the OAuth permissions.
   `tiktok_business` can connect a **Personal TikTok Account**. It does not require
   changing the TikTok profile to a Business Account.
3. If TikTok asks to *switch the profile type* to Business or Organization, cancel.
   That is not part of OAuth and can affect creator monetisation. The profile must
   remain Personal and enrolled in Creator Rewards.
4. Request only Post for Me's `posts` permission. Do not request `feeds` unless this
   application later starts consuming TikTok analytics. The Business connector still
   presents broader TikTok scopes than the consumer connector; review them before
   approving.
5. The account appears in the dashboard with an ID starting with `spc_`.

Generate a fresh Business OAuth URL for each connection and open it on the device where
that TikTok session lives:

```bash
curl -s -X POST https://api.postforme.dev/v1/social-accounts/auth-url \
  -H "Authorization: Bearer $ATR_PFM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "tiktok_business",
    "external_id": "anime_fr_2",
    "permissions": ["posts"]
  }'
# → open the returned url on the device
```

Use the matching account key as `external_id` (`anime_fr_2`, `anime_fr_4`, or
`anime_fr_bis`). List the resulting Business connections and record their `spc_` IDs:

```bash
curl -s "https://api.postforme.dev/v1/social-accounts?platform=tiktok_business" \
  -H "Authorization: Bearer $ATR_PFM_API_KEY"
```

Before changing local configuration, verify each result has `status: connected` and
the expected username/user id. Also record the old `platform=tiktok` connection. Do
not disconnect or delete it during rollout; it is the rollback path.

## 4. Fill the accounts config

For each account in `config/accounts/config.yaml`, add the id to the `tiktok:` block:

```yaml
anime_fr:
  # …
  tiktok:
    slots:
      - "13:00"
    post_for_me_account_id: "spc_..."   # from the selected OAuth flow
    post_for_me_platform: "tiktok_business"
    # optional overrides (defaults shown):
    # privacy_status: "public"
    # allow_comment: true
    # allow_duet: true
    # allow_stitch: true
```

Accounts without `post_for_me_account_id` are skipped at upload time with an explicit
"no Post for Me account configured" status — nothing fails silently.

`post_for_me_account_id` and `post_for_me_platform` are an atomic pair: never combine
an ID from a consumer `tiktok` connection with `tiktok_business`, or the reverse.
Omitting `post_for_me_platform` preserves the legacy `tiktok` behavior.

## 5. Safe migration and smoke test

Before authorizing each account, record that TikTok still shows **Personal Account**,
Creator Rewards is enrolled, and current earnings are visible. Repeat those checks
immediately after OAuth. Stop and restore the consumer connection if any changed.

After deploying the dual-connector code:

1. Migrate `anime_fr_2` first and run its next normal public upload with only `tiktok`
   requested and a slot a few minutes ahead.
2. Watch the Discord embed: the TikTok line should go ⏳ → ✅ with the published URL.
3. Verify public visibility, caption, comments/duet/stitch, Personal Account status,
   and Creator Rewards eligibility in TikTok Studio.
4. Only after that post passes, change `anime_fr_4` and `anime_fr_bis` to their new
   Business connection IDs and repeat the verification.
5. Retain all consumer mappings for at least seven days. Roll back by restoring the
   old ID and `post_for_me_platform: "tiktok"`; no code rollback is needed.

Existing VPS jobs snapshot the connector and account ID. Leave jobs with a scheduled,
publishing, timed-out, or successful `post_id` on their original connector. A request
that tries to switch such a job returns HTTP 409 `tiktok_target_locked`. Definitively
failed jobs (including `reached_active_user_cap`) and pending jobs without a `post_id`
can be resubmitted after the account configuration changes.

## Notes & limits

- **Connector limits:** the consumer Direct Post API has both per-creator posting caps
  and an app-wide active-creator quota. `tiktok_business` avoids the latter, but normal
  Post for Me and TikTok rate limits still apply.
- **Reach:** the Business connector publishes an organic public post, but TikTok does
  not guarantee identical distribution for any posting method. Compare several posts
  with the account's own baseline; one low-view post alone is not a rollback signal.
- **Token health:** if a TikTok session is revoked (password change, security event),
  the account shows `disconnected` in Post for Me and publishes fail with a clear
  error → the server pings Discord after 5 attempts. Reconnect via step 3 (same
  `spc_` id is kept on reconnect).
- **Do not enable `is_draft`:** drafts land in the TikTok inbox and require manual
  in-app publishing — the exact flow we're removing.
- API reference: https://api.postforme.dev/docs
