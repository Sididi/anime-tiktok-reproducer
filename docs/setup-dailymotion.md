# Dailymotion setup

Checked against Dailymotion's official documentation on **1 October 2026**.

This project uses Dailymotion's current **API v2** directly. There is no paid publishing intermediary. The VPS runs scheduled jobs, uploads the file privately, records its video ID, and makes that same video public. Dailymotion processes the upload asynchronously, so the scheduled time is when publishing begins; playback can become available later.

## 1. Confirm that your account can create a private API key

An existing viewing account is not sufficient. Open [Dailymotion Studio](https://www.dailymotion.com/partner), sign in, and use **Upgrade to Partner** if offered. Complete the profile information and accept the Partner terms. Dailymotion documents this account upgrade as free. The Creator Support Program and revenue eligibility are separate matters. [Official Creator upgrade instructions](https://faq.dailymotion.com/hc/en-us/articles/11526400930194-Become-a-Creator).

Check **Organization → API keys** while signed in as an Owner or Admin. Private keys are available to Creator Standard/Premium accounts and eligible Pro accounts. The availability of the menu and key type is the practical access check; a free upgrade does not guarantee that every existing account immediately receives every capability. If private keys are unavailable, ask Dailymotion Support whether your Creator account can receive access without a subscription. You do not need to buy a Pro plan for this project's implementation. [Official API key eligibility](https://faq.dailymotion.com/hc/en-us/articles/5483274630930-Create-and-manage-your-API-keys).

## 2. Create credentials

In **Organization → API keys → Create API key**:

1. Select **Private API key**.
2. Give it a recognizable name, such as `Anime uploader`.
3. Limit its channel access to the profile you intend to publish to.
4. Enable the permissions to read and manage videos. The publisher requests `video.read video.manage`.
5. For the one-time profile discovery below, also allow the account read permission (`account.read`).
6. Create the key and securely save its **API key** and **API secret**. The secret is displayed only once.

The API key becomes `client_id`; the API secret becomes `client_secret`. API v2 uses the OAuth **client credentials** grant. There is no browser redirect, authorization code, callback URL, account password, or refresh token to supply. The publisher requests new access tokens when needed and renews after an expired-token response. [Official authentication workflow](https://developers.dailymotion.com/docs/authenticate).

## 3. Find the destination profile ID

Use the `profile_id` belonging to the channel, not your user ID or channel name. Run this from the repository root; it prompts for the two credentials without displaying them and prints only profile IDs and names:

```sh
server/.venv/bin/python - <<'PY'
from getpass import getpass
import httpx

client_id = getpass("Dailymotion API key: ")
client_secret = getpass("Dailymotion API secret: ")
with httpx.Client(timeout=30) as client:
    token_response = client.post(
        "https://oauth2.dailymotion.com/v2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "account.read",
        },
    )
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]
    profiles_response = client.get(
        "https://api.dailymotion.com/v2/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    profiles_response.raise_for_status()
    for profile in profiles_response.json().get("profiles", []):
        print(profile["profile_id"], profile.get("name", ""))
PY
```

If you use a different Python environment, run its Python executable with `httpx` installed. Select the profile matching your intended channel. A missing profile usually means the API key was granted access to a different channel. [Official profile discovery](https://developers.dailymotion.com/docs/collect-profile-ids).

## 4. Add the project configuration

Add the following `dailymotion` block to the existing account in your private `config/accounts/config.yaml`. Preserve the other fields and platforms already configured there:

```yaml
accounts:
  anime_fr:                       # your existing account ID
    # ...existing account settings...
    dailymotion:
      mode: api
      client_id: "YOUR_PRIVATE_API_KEY"
      client_secret: "YOUR_PRIVATE_API_SECRET"
      profile_id: "YOUR_PROFILE_ID"
      category: shortfilms
      is_for_kids: false
      slots: ["14:00", "18:00"]   # optional; Europe/Paris, inherits account slots if omitted
```

Values to supply:

| Field | Where it comes from |
| --- | --- |
| `client_id` | The private API key from Studio |
| `client_secret` | Its private API secret |
| `profile_id` | The profile selected in step 3 |
| `category` | A Dailymotion content category; `shortfilms` is the default |
| `is_for_kids` | Your audience classification, as a YAML boolean |
| `slots` | Optional publication times in Europe/Paris local time |

Choose `is_for_kids` according to the actual audience; animation alone is not the setting. Title and description come from the upload's metadata. The adapter limits the title to 255 characters and the description to 3,000. API uploads require explicit audience/category/visibility values; Studio content defaults are not a substitute. [Official video creation fields](https://developers.dailymotion.com/reference/create-profile-video).

Keep real secrets in the private account configuration, never in `config.example.yaml` or a committed guide. Restart/reload the backend after editing account configuration. The backend must be connected to the existing VPS and Drive upload pipeline; keep the VPS worker running at the scheduled time.

If API access has not been granted, use `mode: manual` while completing onboarding. This keeps the project's planning/reminder workflow available, with publication performed by you in Dailymotion Studio.

## 5. Scheduling, rescheduling, cancellation, and recovery

The project owns the schedule. An API-mode job reaches the VPS with its destination credentials and metadata. At the due time the worker downloads the source from Drive, obtains an upload session, transfers the video, creates a **private** video, and changes its visibility to **public**. Dailymotion receives the binary file directly; the existing Drive source must remain downloadable by the VPS. [Official upload workflow](https://developers.dailymotion.com/docs/upload-videos).

Reschedule and cancel queued uploads through the same planning controls as the other platforms. Rescheduling changes the pending job time. Cancellation before publishing starts prevents the worker from submitting it. A job that has started may require reconciliation before a safe retry or deletion; inspect the returned status instead of starting a second upload.

The publisher persists its progress before video creation and before publication. If the final publish response is lost, retrying reads the existing video's visibility and reuses its ID. If private creation itself had an unknown result, automatic re-creation stops: inspect **Studio → Media → Videos** for the private upload and resolve/remove that orphan before attempting another job. The adapter supports API deletion of a known video ID, but planning cancellation after a publish attempt may require manual reconciliation in Studio. [Official update endpoint](https://developers.dailymotion.com/reference/update-video), [official deletion endpoint](https://developers.dailymotion.com/reference/delete-video).

The current API v2 reference does not document a field for a future publication deadline. Its `published_at` field is not used as a scheduler. The older API's `publish_date` capability is restricted to verified partners, so this implementation does not depend on it. [Legacy availability restrictions](https://developers.dailymotion.com/v0/reference/api-edit-a-video).

## 6. First verification

1. Ensure the API key can read the intended profile and has video management permission.
2. Pick a short video you are ready to publish and schedule it a few minutes ahead.
3. Verify that the pending Dailymotion entry appears in the project's planning view.
4. Move its time before it starts and verify that only the new time remains.
5. Cancel a separate pending test job and verify that no video is created for it.
6. Let one job run, then check its video link and your Studio video list. Allow time for encoding.

HTTP 401 generally indicates invalid or expired credentials; the publisher already refreshes a rejected access token once. HTTP 403 indicates missing scope, profile permission, or account capability. HTTP 422 indicates invalid metadata. Upload and rate limits still apply to the account; free integration does not mean unlimited uploads. [Official upload policies and technical specifications](https://faq.dailymotion.com/hc/en-us/articles/115009030568-Upload-policies-technical-specifications).

## Recover a failed scheduled job

The planning view and Discord entry show the provider failure. Fix credentials in the private account configuration and reload the backend. Retry through the local backend (replace `PROJECT_ID`, and use your backend address if different):

```sh
curl --fail-with-body -X POST \
  http://localhost:8000/api/project-manager/projects/PROJECT_ID/platforms/dailymotion/retry
```

Retry preserves the original video, metadata, destination, and submission checkpoints. It supplies the current credentials without changing the creator identity. A due job is attempted on the next VPS scheduler tick. If the outcome of a prior submission is unknown, retry deliberately leaves that protection in place.

For an unknown outcome, inspect the platform's creator dashboard first. After you have confirmed the actual result, record that decision through the local backend:

```sh
# Only after confirming the video was never submitted/published (and removing
# any orphan private upload, where applicable):
curl --fail-with-body -X POST \
  http://localhost:8000/api/project-manager/projects/PROJECT_ID/platforms/dailymotion/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"outcome":"not_published","confirm":true}'
```

Then call the retry route above, or cancel the reservation. If the video exists, use `{"outcome":"published","confirm":true,"resource_id":"REMOTE_VIDEO_ID"}` instead; that records the existing post and prevents another submission. For Bilibili use the `BV...` video ID; for Snapchat use the Spotlight ID; for Dailymotion use its video ID. These recovery actions are API routes, not additional planning buttons. Never confirm `not_published` merely because a network request timed out.
