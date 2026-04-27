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

Replace `INTERNAL`, `MOBILE`, `BASE_URL`, and channel/role IDs with real values.

```bash
INTERNAL="<ATR_TIKTOK_SERVER_INTERNAL_TOKEN>"
MOBILE="<ATR_MOBILE_TOKEN_IPHONE_13_PRO>"
BASE="https://tiktok.sididi.tv"
```

### 1. Health check

```bash
curl -s "$BASE/healthz" | jq
# Expected: {"status":"ok","jobs_pending":0}
```

### 2. Avatar serves

```bash
curl -sI "$BASE/api/avatars/anime_fr.jpg"
# Expected: HTTP/2 200, content-type: image/jpeg
```

### 3. Mobile auth gate

```bash
curl -s "$BASE/api/mobile/me"
# Expected: 401

curl -s "$BASE/api/mobile/me" -H "Authorization: Bearer $MOBILE" | jq
# Expected: {"device_id":"iphone_13_pro", "accounts":[...]}
```

### 4. Create a fake job → verify Discord embed + reminder

```bash
curl -s -X POST "$BASE/api/internal/jobs" \
  -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "smoke-1",
    "account_id": "anime_fr",
    "slot_time": "2026-04-26T21:00:00+00:00",
    "anime_title": "Smoke Test",
    "description": "Hello from the smoke test",
    "drive_video_url": "https://drive.google.com/uc?id=fake",
    "platforms_requested": ["youtube", "facebook", "instagram", "tiktok"]
  }' | jq
```

In Discord:
- The upload channel should show a rich embed with avatar + device + project + platforms grid + description + drive URL.
- The reminder channel should show a forwarded copy with the role ping.

### 5. Update a platform status → verify embed edits

```bash
curl -s -X POST "$BASE/api/internal/jobs/smoke-1/platform-status" \
  -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d '{"platform":"youtube","status":"uploaded","url":"https://youtu.be/SMOKE"}'
```

In Discord: the embed's YouTube line should change to `✅ YouTube — https://youtu.be/SMOKE`.

### 6. Mobile job list → ack flow

```bash
curl -s "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE" | jq
# Expected: array containing the smoke-1 job

JOB_ID=$(curl -s "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE" | jq -r '.[0].job_id')

curl -s "$BASE/api/mobile/jobs/$JOB_ID/video-url" -H "Authorization: Bearer $MOBILE" | jq
# Expected: {"video_url":"https://drive.google.com/uc?id=fake"}

curl -s -X POST "$BASE/api/mobile/jobs/$JOB_ID/ack" -H "Authorization: Bearer $MOBILE" | jq
# Expected: {"ok":true, "status":"acked"}
```

In Discord: the embed's TikTok line should change to `✅ TikTok — Posté`. The bot should add a `✅` reaction below the embed.

### 7. Cascade delete

```bash
curl -s -X DELETE "$BASE/api/internal/jobs/smoke-1" -H "Authorization: Bearer $INTERNAL" | jq
# Expected: {"ok":true, "deleted":true}
```

In Discord: the embed message and the reminder message both disappear.

### 8. Final state

```bash
curl -s "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE" | jq
# Expected: []
```
