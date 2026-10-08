# Bilibili setup

Checked on **1 October 2026**. This project targets the **bilibili.com creator ecosystem**, including its international creator experience. Start with [Bilibili Studio](https://member.bilibili.com/creator/), sign in with your existing account, and verify that you can reach its upload screen. If your current account cannot sign in there, create an account from that Studio login flow. Keep the account identifier/UID from that profile for the configuration below.

The similarly named **bilibili.tv** service has its own [official product page](https://www.bilibili.tv/en/about). We did not verify a supported public upload API for that service; its credentials cannot be used with this adapter. The August 2026 international expansion does not establish that every overseas creator can use Bilibili's developer APIs.

## Recommended starting mode: manual

Manual mode adds Bilibili to the project's scheduling, rescheduling, cancellation, and Discord reminder workflow without developer approval or a paid publishing service. You perform the final upload in Bilibili Studio.

Add this under the relevant existing account in `config/accounts/config.yaml`:

```yaml
accounts:
  anime_fr:
    # Keep the account's other existing settings.
    bilibili:
      mode: manual
      identity: "YOUR_BILIBILI_UID"
      slots: ["18:00"]
```

Slots use **Europe/Paris local time**, including daylight-saving changes. Set your preferred times. Omitting the entire `bilibili` block leaves Bilibili disabled; `slots: []` explicitly disables its slots. Use the same `identity` for account entries that publish to the same Bilibili profile, so reservations share the same pool.

Restart/reload the backend after changing the account configuration. The deployed VPS, Google Drive delivery, and Discord bot must already be working, as with the other platforms. Schedule a project with Bilibili enabled. Five minutes before its Bilibili slot, the bot posts a dedicated reminder containing the video and description. Download the video, upload it in Studio, and react **✅ on the Bilibili reminder** once you have posted it. Cancel or reschedule using the project's existing scheduling actions; these update the pending reminder.

Manual mode does not need your password, session cookies, OAuth client, or access token in this project. A reminder acknowledgment records your confirmation; it does not independently verify publication on Bilibili.

## Optional automatic publishing: obtain approval first

Automatic mode uses Bilibili's own Open Platform directly. The implementation adds no subscription or paid intermediary. **Availability to your French account and any commercial conditions of API access must be confirmed with Bilibili**; a free consumer account is not proof of approved developer access.

1. Sign in to the [Open Platform](https://open.bilibili.com/) with the intended account.
2. Complete the developer identity/organization information requested by the platform. Wait for approval.
3. Create an application under application management. Supply the requested application information and a callback URL you control; retain the issued **client ID** and **client secret**.
4. Request the **ARC_BASE** video-management permission and **USER_INFO** account-information permission for the app. This adapter uses account information to verify the token belongs to the configured creator before uploading. Wait until the application and permissions are approved. If your account cannot complete developer enrollment, continue in manual mode.
5. Authorize the actual publishing account to that app, including its video-management permission. Creating an app and authorizing a creator account are separate steps.

These approval requirements are documented by Bilibili's [Open Platform onboarding](https://open.bilibili.com/ticket) and [ARC_BASE video-upload documentation](https://open.bilibili.com/doc/4/733a520a-c50f-7bb4-17cb-35338ba20500). Approval is also reflected in the provider's [permission error codes](https://open.bilibili.com/doc/4/8673959e-f7bb-56e6-6e68-d225f971b81b).

## Obtain your OAuth token

The project does not host a Bilibili callback route. Register an HTTPS callback on a domain you control in the approved application. It can be a simple page for this one-time setup; keep its query string out of analytics and access logs. After authorization, copy the complete redirected URL from the address bar, including `code` and `state`.

Bilibili's [official authorization example](https://github.com/bilibili-openplatform/OpenPlatform_CSharpDemo/blob/main/OpenPlatform_Authorization/Program.cs) specifies the authorization URL and `gourl` callback parameter. Its [official token-exchange example](https://github.com/bilibili-openplatform/OpenPlatform_CSharpDemo/blob/main/OpenPlatform_Authorization/AccessToken.cs) uses a form-encoded authorization-code exchange.

Run the following from the repository root on your local machine with the server environment installed. It prompts for secrets, checks the returned callback and `state`, and saves the token result to a private temporary file. It does not publish anything.

```bash
server/.venv/bin/python -c '
import asyncio, getpass, json, os, secrets, sys, tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
sys.path.insert(0, "server")
from app.services.bilibili_publisher import BilibiliPublisher

client_id = input("Bilibili client ID: ").strip()
client_secret = getpass.getpass("Bilibili client secret: ")
callback = input("Registered HTTPS callback URL: ").strip()
target = urlsplit(callback)
if target.scheme != "https" or not target.hostname or target.query or target.fragment:
    raise SystemExit("Use a registered HTTPS callback without query parameters or fragment")
state = secrets.token_urlsafe(32)
query = urlencode({"client_id": client_id, "gourl": callback, "state": state})
print("Open this URL in your browser and authorize the intended account:")
print("https://account.bilibili.com/pc/account-pc/auth/oauth?" + query)
returned = urlsplit(getpass.getpass("Paste the complete redirected URL: "))
values = parse_qs(returned.query)
if (returned.scheme, returned.netloc, returned.path) != (target.scheme, target.netloc, target.path):
    raise SystemExit("Callback URL does not match")
if values.get("state") != [state] or len(values.get("code", [])) != 1:
    raise SystemExit("Missing code or mismatched state; repeat authorization")
tokens = asyncio.run(BilibiliPublisher.exchange_authorization_code(
    client_id=client_id, client_secret=client_secret, code=values["code"][0]))
folder = Path(tempfile.mkdtemp(prefix="bilibili-oauth-"))
path = folder / "tokens.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump(tokens, stream, indent=2)
print("Token values saved privately to", path)
'
```

Open that file locally and copy `access_token`, `expires_at`, and `openid` into your private account configuration. The OpenID identifies the creator within your app; it is distinct from the public profile UID used as the scheduling `identity`. The helper retrieves it using Bilibili's [official account-information API](https://github.com/bilibili-openplatform/OpenPlatform_CSharpDemo/blob/main/OpenPlatform_UserInfo/info.cs). Keep the returned `refresh_token` securely if you will manage renewal through Bilibili's approved tooling.

**This adapter does not automatically refresh Bilibili tokens.** It requires a known expiry and fails before publishing if expiry is missing or less than five minutes away. The initial upload also rejects a schedule later than the token expiry minus five minutes. Reauthorize before expiration, update the configuration, and retry affected queued jobs using the refreshed credentials. The configuration's `expires_at` is an absolute Unix timestamp, not a number of seconds from now. Delete the temporary token file after storing the values securely.

## Configure automatic mode

Use this block only after approval and successful authorization:

```yaml
accounts:
  anime_fr:
    bilibili:
      mode: api
      identity: "YOUR_BILIBILI_UID"
      slots: ["18:00"]
      client_id: "APP_CLIENT_ID"
      client_secret: "APP_CLIENT_SECRET"
      access_token: "AUTHORIZED_CREATOR_ACCESS_TOKEN"
      openid: "CREATOR_OPENID_RETURNED_BY_AUTHORIZATION_HELPER"
      expires_at: 0  # Replace with the actual token expiry timestamp.
      category_id: 0 # Replace with the provider category for your content.
      copyright: 1  # 1 = original work; 2 = repost (requires source below).
      # source: "https://original-publisher.example/video"
      # tags: ["anime", "review"]  # Optional account override of project tags.
```

The project supplies the video's title, tags, and description from its existing video metadata. Set `tags` in the account block only if you want a fixed override. The adapter requires a nonempty title of at most 80 characters and at least one tag. Choose the correct originality setting for the actual work; selecting `1` does not establish ownership or monetization eligibility.

Select `category_id` using the approved application's [category-list endpoint](https://open.bilibili.com/doc/4/4f13299b-5316-142f-df6a-87313eaf85a9). After saving the credentials above, run this read-only command from the repository root, replacing `anime_fr` with your configured account key if necessary. Inspect the returned categories and copy the numeric ID for the appropriate category. The provider's [official upload example](https://github.com/bilibili-openplatform/demo/blob/main/java/Demo.java) also shows this selection step. Do not reuse a YouTube category number or assume the example value `0` is valid.

```bash
server/.venv/bin/python -c '
import json, sys, httpx, yaml
from pathlib import Path
sys.path.insert(0, "server")
from app.services.bilibili_publisher import BilibiliPublisher
config = yaml.safe_load(Path("config/accounts/config.yaml").read_text())
publisher = BilibiliPublisher(config["accounts"]["anime_fr"]["bilibili"])
response = httpx.get(
    "https://member.bilibili.com/arcopen/fn/archive/type/list",
    headers=publisher._headers(), timeout=30)
response.raise_for_status()
payload = response.json()
if payload.get("code") != 0:
    raise SystemExit("Category request failed; check ARC_BASE approval and token validity")
print(json.dumps(payload.get("data"), ensure_ascii=False, indent=2))
'
```

At the scheduled time, the VPS uploads and submits the video. The adapter stores upload progress and a durable checkpoint before submission. If the final API reply is lost, it stops with an unknown-outcome error: inspect Studio before deciding to retry. A successful result means **Bilibili accepted the submission**; review and processing can delay or prevent public visibility. Keep the VPS online and the Drive file available until the submission finishes.

Rescheduling or canceling before dispatch changes the server job. Once Bilibili has accepted an archive, manage that archive in Studio; a scheduling action is not a promise to undo moderation or delete an already published video. Test one video first, confirm the target account and review outcome, then enable your regular slots.

## Resolve an interrupted submission

The usual retry keeps duplicate-prevention checkpoints. An unknown submission outcome therefore needs an explicit operator decision first. Check **all** relevant Studio entries, including processing, pending review, rejected, and private videos, before declaring a submission absent.

Recovery currently uses authenticated API routes, without a dedicated recovery UI. The VPS route is `POST /api/internal/jobs/{project_id}/platforms/bilibili/reconcile`, authenticated with its existing internal bearer token. Only failed API jobs can be reconciled; an upload in progress returns `409`.

If you find the submitted video, send:

```json
{"outcome": "published", "confirm": true, "resource_id": "BV_REPLACE_WITH_ACTUAL_VIDEO_ID"}
```

Use the actual `BV...` ID from Studio, not the example placeholder. This records your verified result and prevents another upload. If review is still pending, wait until you can verify the intended publication before confirming this outcome.

If you verify that no submitted video exists, send:

```json
{"outcome": "not_published", "confirm": true}
```

This clears the uncertain-submission marker and keeps reusable upload data. It **does not start another upload**. Then retry or cancel the failed job using the normal platform routes. Never use this confirmation just because a video is not visible publicly: it could still be processing or awaiting review. The same recovery API exists for Snapchat and Dailymotion; after a Dailymotion interrupted create with no known ID, remove any orphaned private video in Studio before confirming absence and retrying.

## Monetization for a creator in France

The official global [Bilibili Studio](https://member.bilibili.com/creator/) page promotes revenue opportunities and payment services for creators. We did **not** verify a current official eligibility table guaranteeing enrollment or payouts for every French resident, or a specific future international monetization launch date. Creating a creator account, obtaining upload API approval, and qualifying for a monetization program are separate processes.

Use your real French residence/account details. In Studio, inspect the monetization programs actually offered to your account and ask creator support to confirm French residency eligibility, accepted identity documents, payout destination and currency, required tax information, and rules for your content format. This integration anticipates publishing to that account; it does not enroll you in or activate monetization. Start with manual mode while access and monetization eligibility are being confirmed.

## Recover a failed scheduled job

The planning view and Discord entry show the provider failure. Fix credentials in the private account configuration and reload the backend. Retry through the local backend (replace `PROJECT_ID`, and use your backend address if different):

```sh
curl --fail-with-body -X POST \
  http://localhost:8000/api/project-manager/projects/PROJECT_ID/platforms/bilibili/retry
```

Retry preserves the original video, metadata, destination, and submission checkpoints. It supplies the current credentials without changing the creator identity. A due job is attempted on the next VPS scheduler tick. If the outcome of a prior submission is unknown, retry deliberately leaves that protection in place.

For an unknown outcome, inspect the platform's creator dashboard first. After you have confirmed the actual result, record that decision through the local backend:

```sh
# Only after confirming the video was never submitted/published (and removing
# any orphan private upload, where applicable):
curl --fail-with-body -X POST \
  http://localhost:8000/api/project-manager/projects/PROJECT_ID/platforms/bilibili/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"outcome":"not_published","confirm":true}'
```

Then call the retry route above, or cancel the reservation. If the video exists, use `{"outcome":"published","confirm":true,"resource_id":"REMOTE_VIDEO_ID"}` instead; that records the existing post and prevents another submission. For Bilibili use the `BV...` video ID; for Snapchat use the Spotlight ID; for Dailymotion use its video ID. These recovery actions are API routes, not additional planning buttons. Never confirm `not_published` merely because a network request timed out.
