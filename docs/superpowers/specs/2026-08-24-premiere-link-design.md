# Premiere Link — VPS-brokered automatic project launch

**Date:** 2026-08-24
**Status:** Implemented (owner E2E with the Premiere PC pending)
**Replaces:** `2026-07-05-lan-transfer-design.md` (LAN transfer + LAN trigger, removed)

## Problem

Opening a project in the Premiere Pro CEP panel required a human on the
Premiere PC to click the `http://localhost:48653/p/{project_id}` link posted to
Discord after the `/processing` Drive export. A CEP-side receiver for automatic
launches existed (`lan_trigger.js`, 2026-08-14) but nothing ever called it, and
the LAN design needed inbound ports, a Windows firewall rule and hostname
resolution between the two machines, with no durability when the panel was
closed.

## Design

```
backend (PC1) ──HTTPS POST /api/internal/cep/launches (bearer internal token)──▶ VPS ──WSS /api/cep/ws──▶ CEP panel (PC2)
                                                                                 │◀── ack (accepted|duplicate|error) ──┘
                                                                                 └──▶ Discord: appends "Premiere: …" to the generation message
```

Both ends connect **outbound** to the VPS (`server/`, `https://tiktok.sididi.tv`).
No inbound port anywhere; the backend binds `127.0.0.1` again; the LAN token,
`/api/lan/*`, the `0.0.0.0` bind and the CEP LAN settings are gone.

### Backend (`backend/app/services/cep_link_service.py`)
- `_notify_drive_upload_complete` (`api/routes/processing.py`) posts the Discord
  message as before, then `CepLinkService.request_launch(project,
  discord_message_id, discord_content)`. Never raises, never blocks the route.
- Failure → `project.cep_launch_request` (payload + `retries`, `last_error`,
  `last_attempt_at`); `CepLinkService.run_loop` (lifespan task, first tick at
  startup, then every 60 s) retries with the reschedule backoff steps
  (1/2/5/15/60 min), alerts Discord after 5 failures, drops after 7 days.
- Managed project delete calls `DELETE /api/internal/cep/launches/{id}`.
- Kill switch `ATR_CEP_LINK_ENABLED=false`. VPS URL/token = `tiktok_server_*`.

### VPS (`server/app/services/cep_link.py`, `cep_launch_store.py`, `api/cep.py`, `api/internal.py`)
- Store `data/cep_launches.json` (JobStore pattern), one entry per project:
  `{project_id, launch_id "l_<hex8>", anime_title, requested_at, created_at,
  updated_at, expires_at=requested_at+7d, status pending|accepted|duplicate|
  error|expired, discord_message_id, discord_content, delivered_at,
  delivery_count, acked_at, ack_detail, panel_build_id}`.
- `POST /api/internal/cep/launches` upserts (always a new `launch_id`; a
  re-export supersedes a pending launch), writes `⏳ waiting for the panel` to
  Discord, pushes live if a panel is connected → `{launch_id, status,
  connected, delivered}` (202). `GET/DELETE /api/internal/cep/launches/{id}`,
  `GET /api/internal/cep/status`; `DELETE /api/internal/jobs/{id}` cascades.
- WebSocket `/api/cep/ws`: accept → first frame `auth` within 5 s
  (`ATR_CEP_LINK_TOKEN`, constant-time) else 4401/4400 → `auth_ok` → replay
  every pending unexpired launch (`replay: true`) → live pushes. App-level
  `ping`/`pong` every 25 s both ways; no pong for 2 intervals → 4408.
  `ack` → `record_ack` (stale `launch_id` → `error unknown_launch`) → Discord
  outcome line. Maintenance loop (10 min) expires stale launches (`⌛`).
  Shutdown closes sockets with 1012.
- nginx: dedicated `location = /api/cep/ws` (Upgrade headers, 3600 s timeouts,
  no buffering). Deployment: `server/DEPLOYMENT.md` §14.

### CEP (`premiere-extension/tiktok-reproducer/client/cep_link.js`)
- Browser-native `WebSocket` of the CEF page (no `ws` package). Settings
  `link_url` (default `wss://tiktok.sididi.tv/api/cep/ws`) + `link_token`;
  `Test Link` button. Reconnect with jittered backoff 1 s → 60 s (4401 pinned
  at 60 s). Client heartbeat 25 s, close 4000 after 50 s without a pong.
- `launch` → `handleLinkLaunch` → `queueDownloadImport(project_id, "link", …)`
  (identical to `/p/{id}`) → ack `accepted` / `duplicate` (per-session rule,
  logged as a warning) / `error` (e.g. host build mismatch). The link starts
  only after the host build check passes, so a mismatched `host.jsx` leaves
  launches waiting on the VPS.
- `/health` exposes `link_enabled`, `link_connected`, `link_last_error`,
  `link_reconnect_attempt`, `link_last_launch_at`; the status dot's tooltip
  shows the link state. Build ids bumped to `2026-08-24-premiere-link-v16`.

## Protocol (JSON text frames)

| Direction | Frame |
|---|---|
| panel → VPS | `{"type":"auth","token","panel_build_id","port"}` (first frame) |
| panel → VPS | `{"type":"pong","ts"}` · `{"type":"ping","ts"}` |
| panel → VPS | `{"type":"ack","launch_id","project_id","result":"accepted"\|"duplicate"\|"error","detail","status","queue_state","batch_phase"}` |
| VPS → panel | `{"type":"auth_ok","protocol_version","server_time","pending_count","heartbeat_interval_s"}` |
| VPS → panel | `{"type":"launch","launch_id","project_id","anime_title","requested_at","replay"}` |
| VPS → panel | `{"type":"ping","ts"}` · `{"type":"pong","ts"}` · `{"type":"error","code","detail"}` |

Close codes: 4401 auth failed/timeout/unconfigured · 4400 protocol · 4408 server
heartbeat timeout · 1012 server restart · 4000 client heartbeat/auth timeout ·
1000 client stop.

## Decisions (owner, 2026-08-24)
1. Remove LAN transfer **and** the LAN system; replace with the VPS-brokered socket.
2. Re-upload of a project already handled in the current Premiere session is
   dropped as `duplicate` (same as the URL) but surfaced in the panel log and
   Discord.
3. Discord `/p/` link stays as the manual fallback.
4. Single device in v1: the token identifies "the panel".

## Verification
- Backend: `cd backend && pixi run -e dev pytest` (new: `test_cep_link_service.py`,
  `test_processing_drive_notify.py`; extended: `test_managed_project_delete.py`).
- VPS: `cd server && uv run pytest` (new: `test_cep_launch_store.py`,
  `test_cep_link_ws.py`, `test_cep_internal_api.py`).
- CEP: `node --check client/cep_link.js client/main.js`.
- Manual round trip against the deployed VPS: `server/README.md` smoke step 9.
- Owner E2E with the Premiere PC: `DEPLOYMENT.md` §14, then export a project
  from `/processing` and watch the Discord line flip ⏳ → ✅.
