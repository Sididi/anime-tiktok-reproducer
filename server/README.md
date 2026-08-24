# TikTok Server

VPS-deployed FastAPI service for the anime-tiktok-reproducer mobile flow.

## Quickstart (local dev)

```bash
cd server
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env                    # fill in real values
cp config/config.example.yaml config/config.yaml   # fill in real values
uv run uvicorn app.main:app --reload
```

Tests:
```bash
uv run pytest
```

## Deployment

Deployed at `tiktok.sididi.tv` behind nginx + Let's Encrypt. Step-by-step VPS setup (Ubuntu 24.04) is in [DEPLOYMENT.md](DEPLOYMENT.md). The nginx site config lives at [deploy/nginx-tiktok-server.conf](deploy/nginx-tiktok-server.conf).

## Update flow

```bash
cd /opt/tiktok/server
git pull
docker compose up -d --build
```

## Smoke test (post-deploy)

Replace `INTERNAL` and channel/role IDs with real values. TikTok publishing goes through
Post for Me — see [docs/POST_FOR_ME_SETUP.md](../docs/POST_FOR_ME_SETUP.md) for account
setup and `ATR_PFM_API_KEY`.

```bash
INTERNAL="<ATR_TIKTOK_SERVER_INTERNAL_TOKEN>"
BASE="https://tiktok.sididi.tv"
```

### 1. Health check

```bash
curl -s "$BASE/healthz" | jq
# Expected: {"status":"ok","jobs_pending":0}
```

### 2. Avatar serves

```bash
curl -s "$BASE/api/avatars/anime_fr.jpg" -o /tmp/avatar.jpg -w "%{http_code} %{content_type}\n"
# Expected: 200 image/jpeg
```

(`HEAD` is not supported on this route — use `GET` as above.)

### 3. Auth gate rejects unauthenticated

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/api/internal/jobs" \
  -H "Content-Type: application/json" -d '{}'
# Expected: 401
```

### 4. Create a fake job → verify Discord embed

```bash
SLOT=$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%S+00:00)
curl -s -X POST "$BASE/api/internal/jobs" \
  -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"smoke-1\",
    \"account_id\": \"anime_fr\",
    \"slot_time\": \"$SLOT\",
    \"anime_title\": \"Smoke Test\",
    \"description\": \"Hello from the smoke test\",
    \"drive_video_url\": \"https://drive.google.com/uc?id=fake\",
    \"platforms_requested\": [\"youtube\", \"facebook\", \"instagram\", \"tiktok\"]
  }" | jq
```

In Discord:
- The upload channel should show a rich embed with avatar + device + project + platforms grid + description + drive URL.
- At `slot_time` (~2 minutes later) the scheduler publishes TikTok via Post for Me — see step 6.

### 5. Update a platform status → verify embed edits

```bash
curl -s -X POST "$BASE/api/internal/jobs/smoke-1/platform-status" \
  -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d '{"platform":"youtube","status":"uploaded","url":"https://youtu.be/SMOKE"}'
```

In Discord: the embed's YouTube line should change to `✅ YouTube — https://youtu.be/SMOKE`.

### 6. Wait for slot_time → verify TikTok publish

After ~2 minutes (the SLOT you set), check the upload-channel embed. The TikTok line
should flip to `✅ TikTok — <published URL>` (requires `ATR_PFM_API_KEY` to be set and a
valid `tiktok.post_for_me_account_id` on the account). Without a working Post for Me
config, the line instead shows the retry detail (`⏳ TikTok — Uploading (...)`), and a
`@Tiktok Reproducer` role ping is sent to the alerts channel after 5 failed attempts.

### 7. Cascade delete

```bash
curl -s -X DELETE "$BASE/api/internal/jobs/smoke-1" -H "Authorization: Bearer $INTERNAL" | jq
# Expected: {"ok":true, "deleted":true}
```

In Discord: the upload-channel embed disappears, along with the manual-TikTok reminder
message if one was posted (manual mode only, see below).

### 8. Manual TikTok mode (accounts without a Post for Me id)

Create the job with `"tiktok_manual": true` and a `pending` TikTok status (this is what the
backend sends for accounts that have TikTok slots but no `post_for_me_account_id`):

```bash
curl -s -X POST "$BASE/api/internal/jobs" -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d "{\"project_id\":\"smoke-2\",\"account_id\":\"anime_fr\",\"slot_time\":\"$SLOT\",
       \"anime_title\":\"Smoke\",\"description\":\"d\",\"drive_video_url\":\"https://x\",
       \"platforms_requested\":[\"tiktok\"],\"tiktok_manual\":true,
       \"platform_statuses\":{\"tiktok\":{\"status\":\"pending\",\"detail\":\"Post manuel\"}}}"
```

The embed shows `🎯 TikTok — Post manuel (réagis ✅ une fois posté)`. Five minutes before
`slot_time` the scheduler posts ONE reminder in the reminder channel (`@role` ping, embed,
jump link to the original post). React ✅ on either message: the embed flips to
`✅ TikTok — Posté manuellement`, the bot mirrors ✅ on the original post and deletes the
reminder. Reacting before the reminder fires cancels it.

### 9. Premiere Link round trip (CEP auto-launch)

Requires `ATR_CEP_LINK_TOKEN` in the VPS `.env` and the nginx `/api/cep/ws`
block (DEPLOYMENT.md §14). The panel is a browser WebSocket; from a shell use
the Python client that ships with the server env:

```bash
# Terminal A — pretend to be the panel
cd server && uv run python -m websockets wss://tiktok.sididi.tv/api/cep/ws
> {"type":"auth","token":"<ATR_CEP_LINK_TOKEN>","panel_build_id":"smoke","port":0}
< {"type":"auth_ok", ... "pending_count":0}
# answer every {"type":"ping","ts":...} with {"type":"pong","ts":"<same ts>"} or expect close 4408 after ~50 s

# Terminal B — pretend to be the backend
curl -s -X POST "$BASE/api/internal/cep/launches" \
  -H "Authorization: Bearer $INTERNAL" -H "Content-Type: application/json" \
  -d '{"project_id":"smoke-1","requested_at":"2026-08-24T12:00:00+00:00","anime_title":"Smoke","discord_message_id":null,"discord_content":null}' | jq
# Expected: {"launch_id":"l_...","status":"pending","connected":true,"delivered":true}
# Terminal A prints {"type":"launch","project_id":"smoke-1",...}; reply:
> {"type":"ack","launch_id":"l_...","project_id":"smoke-1","result":"accepted","detail":null}

curl -s "$BASE/api/internal/cep/launches/smoke-1" -H "Authorization: Bearer $INTERNAL" | jq .status
# Expected: "accepted"
curl -s "$BASE/api/internal/cep/status" -H "Authorization: Bearer $INTERNAL" | jq
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "$BASE/api/internal/cep/launches/smoke-1" -H "Authorization: Bearer $INTERNAL"
# Expected: 204
```
