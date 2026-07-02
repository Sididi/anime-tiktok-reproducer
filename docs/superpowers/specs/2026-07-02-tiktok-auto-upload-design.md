# TikTok Automatic Upload & Scheduling — Design

**Date:** 2026-07-02
**Status:** Approved by Sid (provider, trigger model, and design sections validated in brainstorming)

## Goal

Replace the manual TikTok posting flow (Discord reminder alert + ✅ reaction acknowledgment)
with fully automated publishing, bringing TikTok to parity with YouTube, Facebook, and
Instagram. The Discord reminder alert system is **removed**; the reaction listener code is
**commented out but kept** in the codebase.

## Provider decision

**Post for Me** (`postforme.dev`) — chosen after a deep survey of the TikTok posting API
landscape. Requirements: TikTok-only, target of 10 accounts × 1 post/day (~300 posts/month),
public direct posting (no draft/inbox step), API-first, cost-efficient, no provider-specific
reach impact.

| Provider | Verdict |
|---|---|
| **Post for Me** | ✅ $10/mo for 1,000 posts, unlimited accounts, managed "Quickstart" credentials (no own TikTok app, no audit), URL + signed-upload media, webhooks, full OpenAPI spec |
| Zernio (ex-Late) | Best docs, but per-account pricing → $48/mo at 10 accounts |
| upload-post | Full TikTok options, but per-profile tiers → $50/mo at 10 accounts |
| PostPeer | $17/mo, but newest player; docs site unreachable during evaluation |
| Outstand (original choice) | ❌ BYOK-only: requires own TikTok developer app + TikTok audit (2–4 weeks, posts forced SELF_ONLY/private until approved) |
| Postiz / Mixpost | ❌ BYOK → same audit problem |
| Ayrshare ($299/mo), Blotato ($97/mo + 3-account/day cap), bundle.social ($100/mo) | ❌ price/limits |

**Reach note:** every audited provider rides the same official TikTok Content Posting API;
reach treatment is decided by TikTok, not the provider. The only reach trap is draft/inbox
mode (`MEDIA_UPLOAD`), which we exclude — we always use direct post (`is_draft: false`).

### Post for Me API surface (verified from their OpenAPI spec, 2026-07-02)

- Base URL `https://api.postforme.dev/v1`, auth via `Authorization: Bearer <API key>`.
- `POST /v1/social-accounts/auth-url` → OAuth URL for connecting a TikTok account
  (managed system credentials on Quickstart projects). Accounts also connectable from
  their dashboard. `GET /v1/social-accounts?platform=tiktok` lists `spc_…` ids.
- `POST /v1/media/create-upload-url` → `{ upload_url, media_url }`; PUT binary to
  `upload_url`, then reference `media_url` in the post.
- `POST /v1/social-posts` → `{ caption, social_accounts: ["spc_…"], media: [{url}],
  scheduled_at?, platform_configurations: { tiktok: {…} } }`. Omitting `scheduled_at`
  publishes immediately. Post statuses: `draft | scheduled | processing | processed`.
- TikTok configuration object: `privacy_status` ("public" | "private", default public),
  `allow_comment`, `allow_duet`, `allow_stitch` (default true), `disclose_your_brand`,
  `disclose_branded_content`, `is_ai_generated` (default false), `is_draft` (default
  false = direct post), `title`, `caption`/`media` overrides, `auto_add_music` (photo
  posts only).
- `GET /v1/social-post-results?post_id=…` → per-account `{ success, error, details,
  platform_data: { id, url } }` — `platform_data.url` is the published TikTok URL.
- Webhooks available (`social.post.result.created` etc.) — **not used**; we poll,
  consistent with the Instagram publisher.

## Trigger model (decided)

**Server-triggered only.** The VPS scheduler publishes via Post for Me at
`platform_scheduled_at["tiktok"]`, exactly like Instagram today. Post for Me's own
`scheduled_at` is not used. Consequences: one code path, `jobs.json` remains the single
source of truth, job deletion never requires a remote cancel, scheduling horizon is
unlimited. Accepted risk: if the VPS is down at slot time, the post fires late (same as
current Instagram behavior).

## Architecture & data flow

```
backend execute_upload()
  └─ builds tiktok_payload (caption + PFM account id + post options)
  └─ creates job on VPS internal API (existing flow, new `tiktok` field)

VPS scheduler (reminder_scheduler.py)
  └─ at platform_scheduled_at["tiktok"]:
       1. download original video from Drive (job.drive_video_url — original
          audio, same file the manual flow used)
       2. POST /v1/media/create-upload-url → PUT binary → media_url
       3. POST /v1/social-posts (no scheduled_at → publish now)
       4. poll GET /v1/social-post-results?post_id=… until a result exists
       5. platform_statuses["tiktok"] = uploaded(url=platform_data.url)
          → re-render Discord embed (⏳ → ✅ TikTok — url)
```

### Failure handling

Mirrors `_dispatch_instagram_publish`:

- Up to **5 attempts**; transient failure resets status to `pending` with `detail`
  preserved, retried next scheduler tick.
- Terminal failure → status `failed` + Discord role ping in the (former reminder,
  now alerts) channel.
- **Double-post guard:** `tiktok_publish_state` persists the PFM post id as soon as
  the post is created. On retry/restart, if a post id exists, poll its results first;
  only create a new PFM post when the previous one has a failed result (or none after
  its processing window). Never create a second post while one is `processing`.

## Accounts configuration

`config/accounts/config.yaml` — extend the existing `tiktok:` block:

```yaml
tiktok:
  slots: ["20:00"]                      # unchanged replace-semantics inheritance
  post_for_me_account_id: "spc_..."     # from PFM after connecting the account
  privacy_status: "public"              # optional, default "public"
  allow_comment: true                   # optional, default true
  allow_duet: true                      # optional, default true
  allow_stitch: true                    # optional, default true
```

Inheritance/consistency changes:

- `slots_for("tiktok")` semantics unchanged (per-platform `slots` replaces top-level).
- **Pooling:** `pool_key_for("tiktok")` returns `f"tiktok:{post_for_me_account_id}"`
  when set (was `None`, "manual post"). Matches Instagram's pooling by business-account
  id: two config accounts sharing one TikTok identity can't double-book a slot.
- **`device` becomes optional** in `AccountConfig` parsing (it existed only for the
  manual-phone flow). The Discord embed shows the device line only when present.
  Existing configs keep working unchanged.
- No TikTok credentials in the server's `config.yaml`; the backend passes everything
  per-job. The PFM API key lives **only in the server's `.env`**
  (`ATR_PFM_API_KEY`) — never in `jobs.json` (deliberately better than
  `instagram_payload`, which embeds tokens).

## Backend changes

- `services/account_service.py`: `AccountTikTokConfig` gains
  `post_for_me_account_id`, `privacy_status`, `allow_comment`, `allow_duet`,
  `allow_stitch`; parsing + `pool_key_for` + optional `device`.
- `services/upload_phase.py`: build `tiktok_payload` when TikTok is requested and the
  account has `tiktok.post_for_me_account_id`:
  ```json
  {
    "pfm_social_account_id": "spc_…",
    "caption": "<metadata.tiktok.description>",
    "privacy_status": "public",
    "allow_comment": true,
    "allow_duet": true,
    "allow_stitch": true
  }
  ```
  Without a configured PFM id, TikTok is marked `skipped`
  ("no Post for Me account configured") — nothing silently disappears.
- `services/discord_service.py` (`create_job` client): pass the new `tiktok` field.
- Reschedule/retry services (`platform_reschedule_service`,
  `reschedule_retry_service`) already drive `platform_scheduled_at["tiktok"]` —
  unchanged.

## Server changes (/server)

- **New** `app/services/post_for_me_publisher.py` — sibling of
  `instagram_publisher.py`: download from Drive → create-upload-url → PUT →
  create post → poll results. Returns `TikTokPublishResult(success, url, detail,
  publish_state)` with a resumable `TikTokPublishState` (PFM post id, stage,
  timestamps, last error).
- `app/services/reminder_scheduler.py`: `_dispatch_tiktok_reminder` →
  `_dispatch_tiktok_publish` (structure copied from `_dispatch_instagram_publish`).
  All reminder-posting code paths removed.
- `app/services/reminder_service.py` **deleted** with its tests (failure pings
  already use `discord.post_message` directly).
- `app/services/reaction_listener.py` **entirely commented out** (kept in repo),
  including its wiring in `app/main.py`; its tests commented out likewise.
- `app/models/job.py`: add `tiktok_payload: dict | None` and
  `tiktok_publish_state: TikTokPublishState | None`. Reminder fields
  (`reminder_message_id`, `reminder_forward_message_id`, `reminder_cancelled`)
  remain readable for existing `jobs.json` entries but are no longer written.
- `app/api/internal.py`: job creation accepts the `tiktok` payload.
- `app/config.py`: `ATR_PFM_API_KEY` (+ optional base URL override for tests).
- Embed builder: TikTok status line behaves like Instagram's (pending/uploading/
  uploaded-with-URL/failed).

## Migration & rollout

- Existing pending jobs with `tiktok` in `platforms_requested` but no
  `tiktok_payload`: scheduler logs a warning and skips (same pattern as Instagram
  missing payload). In-flight reminder messages are left as-is.
- Rollout order: deploy server (accepts+ignores new field gracefully) → connect
  accounts on PFM → fill config → deploy backend.

## Testing

- Publisher unit tests (mocked httpx): happy path, upload failure, post-creation
  failure, result polling (success/failed/timeout), resume-from-state double-post
  guard.
- Scheduler tests: due-time dispatch, retry increments, terminal failure ping,
  missing-payload skip.
- Job model round-trip with new fields; internal API accepts `tiktok` payload.
- Backend: account parsing (new fields, optional device, pooling), payload
  building, skip-when-unconfigured.
- Reminder tests removed with the feature; reaction-listener tests commented out.

## Documentation deliverables

- `docs/POST_FOR_ME_SETUP.md` — account setup guide (see companion file).
- README/DEPLOYMENT sections mentioning reminders updated.
