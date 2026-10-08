# Snapchat Spotlight setup

Verified against Snap's official documentation on 2026-10-01. This integration publishes **Spotlight videos** to a Public Profile.

## Access and cost

The project talks directly to Snapchat; no paid publishing service or subscription is involved. However, **Public Profile API access requires Snap to allowlist your OAuth app**. An existing Snapchat account alone is insufficient. Ask your Snap contact for content publishing access, supplying your client ID and intended use; never send the client secret. Availability is subject to Snap's approval, so free automatic access cannot be guaranteed. [Official access requirements](https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted)

Until approved, use the manual configuration below. It keeps Snapchat in scheduling, rescheduling, cancellation, and Discord reminders while you upload the video yourself.

## Values you need

| Project field | Value |
| --- | --- |
| `mode` | `api` after approval, otherwise `manual` |
| `client_id` | Your Snap Business OAuth app's client ID |
| `client_secret` | That app's secret |
| `refresh_token` | Token obtained by authorizing the account that owns the destination profile |
| `profile_id` | Public Profile UUID returned by the profile lookup below |
| `redirect_uri` | The exact callback URL registered on the OAuth app |
| `locale` | Language and country, for example `fr_FR`, `en_US`, or `es_ES` |
| `slots` | Optional platform-specific Europe/Paris posting times; replaces the account's shared times |

The runtime obtains fresh access tokens from the refresh token. You do not need to copy a short-lived access token into the project. Snap's Marketing API documentation says access tokens last an hour and refresh tokens remain usable unless the associated user's access is removed. [Token lifetime](https://developers.snap.com/marketing-api/Ads-API/faq)

## 1. Create the correct OAuth app

1. Sign into [Snap Ads Manager](https://ads.snapchat.com/) using the existing Snapchat account and create/select its business organization.
2. As an organization administrator, open **Business Dashboard → Business Details → OAuth app**. Create an app and record its client ID and secret. Use the Business OAuth app, not a Snap Kit/Developer Portal app.
3. Register a callback URL under a domain you control, for example `https://YOUR_PROJECT_DOMAIN/oauth/snapchat/setup`. Keep its exact spelling for the next step. This project has no dedicated OAuth callback handler; a 404 at this path is acceptable for the one-time copy-and-paste procedure because the code remains in your browser's address bar.
4. Have Snap approve the app for the Public Profile API before continuing. [Official app setup](https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted)

## 2. Authorize once and retrieve the profile ID

Save the following as `/tmp/setup-snapchat-oauth.py`, then run `python /tmp/setup-snapchat-oauth.py` from a private terminal. It uses Python's standard library and does not upload any video. It generates a unique OAuth `state`, validates the returned value, exchanges the code, and stores credentials in a temporary file readable only by your operating-system user.

Log into the **destination profile owner's account** when the browser opens. After approving access, copy the complete redirected URL from the address bar and paste it into the script prompt. A callback 404 does not prevent this step. Do not use a callback hosted by a third party.

```python
import getpass
import json
import os
import secrets
import tempfile
import urllib.parse
import urllib.request
import webbrowser

client_id = input("Snap Business OAuth client ID: ").strip()
client_secret = getpass.getpass("Client secret: ").strip()
redirect_uri = input("Exact registered redirect URI: ").strip()
state = secrets.token_urlsafe(32)
authorization_url = (
    "https://accounts.snapchat.com/login/oauth2/authorize?"
    + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "snapchat-profile-api",
        "state": state,
    })
)
print("Open this URL if your browser does not launch:", authorization_url)
webbrowser.open(authorization_url)
returned = urllib.parse.urlsplit(getpass.getpass("Full redirected URL: ").strip())
registered = urllib.parse.urlsplit(redirect_uri)
if (returned.scheme, returned.netloc, returned.path) != (
    registered.scheme, registered.netloc, registered.path
):
    raise SystemExit("The redirect destination does not match the registered URL")
query = urllib.parse.parse_qs(returned.query)
if not secrets.compare_digest(query.get("state", [""])[0], state):
    raise SystemExit("OAuth state mismatch; start again")
if "error" in query or not query.get("code"):
    raise SystemExit("Authorization was denied or returned no code")
request = urllib.request.Request(
    "https://accounts.snapchat.com/login/oauth2/access_token",
    data=urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": query["code"][0],
    }).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    tokens = json.load(response)
if not tokens.get("access_token") or not tokens.get("refresh_token"):
    raise SystemExit("OAuth did not return access and refresh tokens")
profile_request = urllib.request.Request(
    "https://businessapi.snapchat.com/v1/public_profiles/my_profile",
    headers={"Authorization": "Bearer " + tokens["access_token"]},
)
with urllib.request.urlopen(profile_request, timeout=30) as response:
    profile_response = json.load(response)
profile = profile_response.get("public_profile", {})
if profile_response.get("request_status") != "SUCCESS" or not profile.get("id"):
    raise SystemExit("No authorized Public Profile found; check ownership and API approval")
fd, path = tempfile.mkstemp(prefix="snapchat-credentials-", suffix=".json")
with os.fdopen(fd, "w") as output:
    json.dump({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
        "profile_id": profile["id"],
        "redirect_uri": redirect_uri,
    }, output, indent=2)
print("Destination:", profile.get("display_name"), "profile_id:", profile["id"])
print("Private credentials saved to:", path)
```

The authorization and token exchange use the Marketing/Public Profile OAuth flow. The lookup reads the `public_profile.id` returned by `GET /v1/public_profiles/my_profile`; verify the displayed profile name before configuring uploads. For an organization-managed profile that differs from the authorizing user's own profile, list `GET /v1/organizations/{organization_id}/public_profiles` using the same bearer token and choose the intended authorized profile's UUID. [OAuth reference](https://developers.snap.com/marketing-api/Ads-API/authentication), [profile lookup reference](https://developers.snap.com/marketing-api/Public-Profile-API/Profiles)

## 3. Configure this project

Add this block **inside an existing account** in `config/accounts/config.yaml` (or the path configured by `ATR_ACCOUNTS_CONFIG_PATH`). Copy the values from the private JSON generated above. Do not replace your other platform settings.

```yaml
accounts:
  anime_fr:  # Your existing account key
    snapchat:
      mode: api
      profile_id: "YOUR_PUBLIC_PROFILE_UUID"
      client_id: "YOUR_BUSINESS_OAUTH_CLIENT_ID"
      client_secret: "YOUR_BUSINESS_OAUTH_CLIENT_SECRET"
      refresh_token: "YOUR_REFRESH_TOKEN"
      redirect_uri: "https://YOUR_PROJECT_DOMAIN/oauth/snapchat/setup"
      locale: "fr_FR"
      slots: ["14:00", "18:00"]  # Europe/Paris wall-clock times, including DST
```

This file is already Git-ignored. Restrict access to it and delete the temporary credential JSON after transferring its contents. Do not put these secrets in frontend environment variables. The backend sends credentials to the authenticated VPS as part of the queued job; renewed credentials are stored privately under the VPS data directory. Keep that directory persistent across deployments.

Ensure the same account key exists in `server/config/config.yaml` for the Discord display. The credentials belong in the backend account configuration above. Rebuild/restart the server after installing this change so its `cryptography` dependency is available; the Docker image already includes `ffprobe`. Restart/reload the backend after changing account configuration.

To use the free manual workflow while waiting for approval, configure:

```yaml
accounts:
  anime_fr:
    snapchat:
      mode: manual
      identity: "your-snapchat-username"  # Shares the scheduling pool if reused
      slots: ["14:00", "18:00"]
```

Manual mode requires no Snapchat API credentials. At the reminder, open the supplied video, upload it to Spotlight through Snapchat, and react ✅ **on that platform's reminder** to mark it complete.

## 4. Verify scheduling and publication

1. Prepare a small compliant test video and schedule it to Snapchat using the normal project upload workflow.
2. Move the scheduled time and verify the queued time changes. Cancel the queued Snapchat destination and verify it stops waiting for dispatch. Repeat with a new schedule and let it publish.
3. Keep the VPS running and the exported video/Drive link accessible until dispatch. The server downloads and uploads at the due time; Snapchat is not holding a native scheduled post in advance.
4. Check the resulting Spotlight in Snapchat. An API success means Snapchat accepted the submission; moderation can still delay or reject public availability.

The API accepts MP4 videos of 6–60 seconds and at least 540×960 resolution. API mode checks duration as soon as the backend stages your video: an invalid or unreadable duration marks Snapchat failed immediately while other platforms continue. The VPS rechecks duration, format, file size, and resolution before publishing. Neither check cuts or speeds up the video. Manual mode is exempt from the API duration preflight. The adapter truncates the description to 160 characters. Uploaded media is encrypted and split into chunks, with temporary encrypted files removed afterward. [Spotlight and media requirements](https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement)

## Cancellation, retries, and troubleshooting

- **Before dispatch:** use the project's existing platform cancellation/rescheduling controls. They modify the durable VPS queue.
- **Once submission begins:** cancellation/rescheduling cannot retract that API call. The documented Public Profile API offers no Spotlight deletion or native scheduling operation. Remove an already submitted/published Spotlight in Snapchat if needed. This is a limitation of the documented API, not a claim about every private Snap partner API. [Published API operations](https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement)
- **Unknown submission outcome:** a timeout can happen after Snap accepted the video. The worker preserves a submission checkpoint and will not blindly repost. Inspect Snapchat before choosing a fresh upload; do not erase the checkpoint to force an unattended retry.
- **403/permission error:** confirm your app is allowlisted for content management and the authorizing account can manage the selected Public Profile. Public metadata access alone does not verify upload permission.
- **OAuth error:** verify the Business OAuth app, exact redirect URL, and `snapchat-profile-api` scope. Reauthorize if the user revoked access or the refresh token stopped working.
- **Duration/resolution error:** prepare a compliant export and rerun the project upload workflow for Snapchat. The recovery route below reuses the original video; it cannot replace an invalid export. An early duration failure creates no Snapchat publisher job on the VPS. Other platforms' longer duration allowances do not apply to this API.
- **Credentials changed after scheduling:** an already queued job contains the credentials captured at upload time. Cancel and queue again with the updated configuration before it dispatches.

The integration's automated tests mock Snap responses and verify encryption, multipart fields, OAuth refresh, error handling, and duplicate prevention. A live upload still requires your approved app and profile; no live publish was performed during implementation.

## Recover a failed scheduled job

The planning view and Discord entry show the provider failure. Fix credentials in the private account configuration and reload the backend. Retry through the local backend (replace `PROJECT_ID`, and use your backend address if different):

```sh
curl --fail-with-body -X POST \
  http://localhost:8000/api/project-manager/projects/PROJECT_ID/platforms/snapchat/retry
```

Retry preserves the original video, metadata, destination, and submission checkpoints. It supplies the current credentials without changing the creator identity. A due job is attempted on the next VPS scheduler tick. If the outcome of a prior submission is unknown, retry deliberately leaves that protection in place.

For an unknown outcome, inspect the platform's creator dashboard first. After you have confirmed the actual result, record that decision through the local backend:

```sh
# Only after confirming the video was never submitted/published (and removing
# any orphan private upload, where applicable):
curl --fail-with-body -X POST \
  http://localhost:8000/api/project-manager/projects/PROJECT_ID/platforms/snapchat/reconcile \
  -H 'Content-Type: application/json' \
  -d '{"outcome":"not_published","confirm":true}'
```

Then call the retry route above, or cancel the reservation. If the video exists, use `{"outcome":"published","confirm":true,"resource_id":"REMOTE_VIDEO_ID"}` instead; that records the existing post and prevents another submission. For Bilibili use the `BV...` video ID; for Snapchat use the Spotlight ID; for Dailymotion use its video ID. These recovery actions are API routes, not additional planning buttons. Never confirm `not_published` merely because a network request timed out.
