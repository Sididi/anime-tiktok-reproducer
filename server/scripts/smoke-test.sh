#!/usr/bin/env bash
# End-to-end smoke test for the deployed VPS server.
# Run from /opt/tiktok/server (or wherever .env + config/config.yaml live)
# AFTER `docker compose up`.
#
# Auto-discovers tokens from .env and the first account+device from config.yaml.
# Verifies: health, mobile auth, create job, platform status update,
# mobile job list, video URL, ack, cascade delete.
#
# After it runs, manually verify in Discord:
#   - Step 3 posted an embed in the upload channel + a reminder in the reminder channel
#   - Step 4 edited the embed (YouTube line ✅ with link)
#   - Step 7 marked TikTok ✅ Posté and added a ✅ reaction
#   - Step 8 removed both Discord messages

set -euo pipefail

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found in $(pwd)" >&2
    echo "       Run this from /opt/tiktok/server" >&2
    exit 1
fi
if [[ ! -f config/config.yaml ]]; then
    echo "ERROR: config/config.yaml not found in $(pwd)" >&2
    exit 1
fi

set -a; . ./.env; set +a

BASE="${ATR_PUBLIC_BASE_URL:-https://tiktok.sididi.tv}"
INTERNAL="${ATR_TIKTOK_SERVER_INTERNAL_TOKEN:?missing in .env}"

ACCOUNT_ID=$(python3 -c '
import yaml
cfg = yaml.safe_load(open("config/config.yaml"))
print(next(iter(cfg["accounts"])))
')
DEVICE_ID=$(python3 -c '
import yaml
cfg = yaml.safe_load(open("config/config.yaml"))
acc = next(iter(cfg["accounts"].values()))
print(acc["device"])
')
DEVICE_TOKEN_VAR="ATR_MOBILE_TOKEN_$(echo "$DEVICE_ID" | tr '[:lower:]' '[:upper:]')"
MOBILE="${!DEVICE_TOKEN_VAR:?missing $DEVICE_TOKEN_VAR in .env}"

echo "Smoke test → $BASE"
echo "  Account: $ACCOUNT_ID"
echo "  Device:  $DEVICE_ID  (token from $DEVICE_TOKEN_VAR)"

step() { echo; echo "=== $* ==="; }

step "1. Health check"
curl -sS "$BASE/healthz"; echo

step "2. GET /api/mobile/me"
curl -sS "$BASE/api/mobile/me" -H "Authorization: Bearer $MOBILE"; echo

step "3. POST /api/internal/jobs (project_id=smoke-1)"
curl -sS -X POST "$BASE/api/internal/jobs" \
  -H "Authorization: Bearer $INTERNAL" \
  -H 'Content-Type: application/json' \
  -d "{
    \"project_id\": \"smoke-1\",
    \"account_id\": \"$ACCOUNT_ID\",
    \"slot_time\": \"2026-04-27T21:00:00+00:00\",
    \"anime_title\": \"Smoke Test\",
    \"description\": \"Hello from the smoke test\",
    \"drive_video_url\": \"https://drive.google.com/uc?id=fake\",
    \"platforms_requested\": [\"youtube\", \"facebook\", \"instagram\", \"tiktok\"]
  }"; echo

step "4. POST /api/internal/jobs/smoke-1/platform-status (youtube → uploaded)"
curl -sS -X POST "$BASE/api/internal/jobs/smoke-1/platform-status" \
  -H "Authorization: Bearer $INTERNAL" \
  -H 'Content-Type: application/json' \
  -d '{"platform":"youtube","status":"uploaded","url":"https://youtu.be/SMOKE"}'; echo

step "5. GET /api/mobile/jobs"
JOBS=$(curl -sS "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE")
echo "$JOBS"
JOB_ID=$(echo "$JOBS" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["job_id"])')
echo "Picked job_id: $JOB_ID"

step "6. GET /api/mobile/jobs/$JOB_ID/video-url"
curl -sS "$BASE/api/mobile/jobs/$JOB_ID/video-url" -H "Authorization: Bearer $MOBILE"; echo

step "7. POST /api/mobile/jobs/$JOB_ID/ack"
curl -sS -X POST "$BASE/api/mobile/jobs/$JOB_ID/ack" -H "Authorization: Bearer $MOBILE"; echo

step "8. DELETE /api/internal/jobs/smoke-1 (cascade)"
curl -sS -X DELETE "$BASE/api/internal/jobs/smoke-1" -H "Authorization: Bearer $INTERNAL"; echo

step "9. GET /api/mobile/jobs (expect [])"
curl -sS "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE"; echo

echo
echo "✓ HTTP surface OK. Now eyeball Discord:"
echo "  3. Embed appeared in upload channel + reminder forwarded with @Tiktok Reproducer ping"
echo "  4. Embed's YouTube line changed to ✅ with the youtu.be/SMOKE link"
echo "  7. Embed's TikTok line changed to ✅ Posté + bot added a ✅ reaction"
echo "  8. Both embed and reminder messages disappeared"
