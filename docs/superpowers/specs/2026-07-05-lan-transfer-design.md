# LAN Transfer for Premiere Pro Phase — Design

**Date:** 2026-07-05
**Status:** Approved (brainstorm with user)

## Problem

The Premiere Pro generation phase runs on a second computer (PC2, Windows 11, Premiere Pro + CEP extension) connected over WiFi through a PLC repeater. Today the CEP panel downloads the full project asset tree from Google Drive and uploads render outputs back to Drive. Both legs run at WiFi speed *plus* Drive API chunking/throttling overhead, making the phase slow despite a ~1Gbps fiber connection on the main machine (PC1, Arch Linux, ethernet).

Goal: a LAN transfer path between PC1 and PC2 that removes the cloud round trip for PC2, while keeping Google Drive fully functional as fallback and as the source for the distant VPS server (scheduled TikTok/Instagram publishing).

**Performance expectation (honest):** the PLC+WiFi hop remains the bandwidth ceiling in both directions. The gain comes from removing the double trip (PC1→cloud→PC2, PC2→cloud) and Drive API overhead, not from raw link speed. Milestone 0 measures the real ceiling with iperf3.

## Current flow (verified in code)

1. PC1 backend exports the project and uploads the `SPM_*` folder to Drive (`ExportService` + `google_drive_service.py`).
2. A Discord message carries a `localhost` URL clicked **on PC2**, hitting the CEP panel's own local HTTP trigger server (`main.js`).
3. The CEP panel talks directly to the Drive API (`drive_tasks.js`): downloads the project tree, builds the Premiere project, renders, uploads `output.mp4` + `output_no_music.wav` (+ `ATR_*.mp4` proxies) back to Drive.
4. PC1's Project Manager (`upload_phase.py`) polls Drive for readiness (green/orange/red), preview, copyright check, Instagram prep. Four `GoogleDriveService.download_file` call sites consume `output.mp4` / `output_no_music.wav` from Drive.
5. The VPS server consumes `output.mp4` and `output_instagram.mp4` (the latter produced **on PC1** during the upload phase) via Drive URLs.

The CEP panel never talks to PC1's backend today. The backend binds `127.0.0.1:8000`; ufw is active on PC1; PC1 runs ProtonVPN (`proton0`).

## Core invariant

**LAN mode replaces only PC2's cloud traffic. PC1 mirrors everything to Drive on its fast ethernet link, so Drive always converges to exactly the same end state as today.**

```
                    ┌────────── PC1 (Arch, ethernet ~1Gbps) ──────────┐
 Drive assets  ◄────┤ export: upload SPM assets (UNCHANGED, fallback) │
 Drive output.mp4 ◄─┤ relay on LAN receipt (NEW, automatic)          │
 Drive no_music  ◄──┤ relay on LAN receipt (NEW, automatic)          │
 Drive instagram ◄──┤ upload phase (UNCHANGED)                       │
                    └───────────────▲────────────────────────────────┘
                                    │ LAN HTTP (assets ↓, outputs ↑)
                    ┌───────────────▼────────────────────────────────┐
                    │ PC2 (Win11, WiFi/PLC) — CEP panel              │
                    │ probes PC1 → LAN mode, else Drive mode (as-is) │
                    └────────────────────────────────────────────────┘
```

Consequences:

- The VPS server is untouched — it keeps reading `output.mp4` / `output_instagram.mp4` from Drive.
- Drive fallback is always complete: if PC2 can't reach PC1, the CEP behaves exactly like today with zero manual steps (user decision: keep the export-time asset upload to Drive — PC1's upload is cheap on ethernet; the slow side was always PC2).
- Project Manager's LAN-first checks are a latency optimization, not a correctness requirement.

## Backend: LAN endpoints & security

New router `/api/lan/*` on the existing FastAPI app, guarded by a shared token (`X-ATR-LAN-Token` header; token in backend `.env`, mirrored in the CEP settings file):

| Endpoint | Purpose |
|---|---|
| `GET /api/lan/ping` | `{ok, api_version}` — CEP LAN detection probe (2.5s client timeout) |
| `GET /api/lan/projects/{id}/manifest` | File list of the local export folder (relative path, size, mtime) — same tree uploaded to Drive |
| `GET /api/lan/projects/{id}/files/{relpath}` | Streamed download; `relpath` resolved and confined to the project export dir (traversal rejected) |
| `POST /api/lan/projects/{id}/outputs/{filename}` | Streamed upload into the project output dir; temp file + atomic rename; filename whitelist: `output.mp4`, `output_no_music.wav` only (ATR proxies stay on PC2 — user decision) |

On receipt of an output file, a background task **relays it to the project's Drive folder** (reuses `GoogleDriveService.upsert_local_file`) and invalidates the readiness cache. Relay failures retry with backoff; `execute_upload` performs a final ensure-upsert regardless.

Deployment on PC1:

- uvicorn binds `0.0.0.0` instead of `127.0.0.1`.
- `ufw allow from 192.168.1.0/24 to any port 8000 proto tcp` (tighten to PC2's static IP if a static lease is set).
- ProtonVPN must have "allow LAN connections" enabled, or its kill-switch drops PC2's packets.
- Accepted trade-off: the whole backend API becomes reachable on the private home LAN; only `/api/lan/*` requires the token; CORS still limits browser access.

## CEP extension changes

A new `lan_tasks.js` implements the **same task interface** as `drive_tasks.js` (`downloadImport`, `uploadOutput`, same progress callbacks); `main.js`'s job runner just picks an engine. `drive_tasks.js` is not modified — it remains the fallback engine.

1. **Settings**: `lan_base_url` (e.g. `http://192.168.1.76:8000`), `lan_token`, `lan_probe_timeout_ms` (default 2500). Empty `lan_base_url` = feature fully off, no probe, behavior byte-identical to today.
2. **Per-job detection**: at the start of each download/upload job, probe `GET /api/lan/ping`. Success → LAN engine for that job; failure/timeout → Drive engine. Fresh probe every job; a log line records the chosen mode and why.
3. **Download job (LAN)**: manifest fetch, sequential per-file download with byte-size verification and 3 retries with backoff; same subtitle-archive extraction and progress reporting as today. If LAN dies mid-job after retries, the job fails cleanly; a re-run re-probes (and may choose Drive — assets are always there).
4. **Upload job (LAN)**: POST `output.mp4` + `output_no_music.wav` to PC1. If LAN is unreachable at upload time, fall back to the existing direct Drive upload — the pipeline converges either way.

## Behavior matrix (exhaustive)

| Behavior | Today | With LAN mode active |
|---|---|---|
| Export: SPM assets → Drive | Backend uploads | **Unchanged** (fallback guarantee) |
| Drive folder creation | At export | **Unchanged** |
| Discord notification + localhost URL | Sent | **Unchanged** |
| CEP asset download | Drive tree walk | LAN manifest + files; fallback Drive |
| CEP output upload | Drive upload | LAN POST → PC1 relays to Drive automatically; fallback direct Drive |
| Upload readiness (green/orange/red) | Polls Drive | **Local-first**: local `output/output.mp4` + metadata → green with no Drive call; else Drive check as today |
| Upload button activation | Via readiness | Inherits local-first automatically |
| Video preview button | Downloads Drive video, caches | **Local-first**: serve local file directly; else Drive download |
| Copyright check video | Drive download | Local-first, else Drive |
| Copyright audio build (`output_no_music.wav`) | Drive download | Local-first, else Drive |
| `execute_upload` source video | Drive download | Local-first, else Drive |
| Instagram prep + `output_instagram.mp4` → Drive | On PC1 | **Unchanged** (VPS needs it); source video now usually already local |
| `drive_video_url` payload for VPS | From Drive file id | Guaranteed by relay; upload phase does a final ensure-upsert in case a relay failed silently |
| Managed delete (local + Drive + webhook) | Works | **Unchanged** — LAN files *are* the local files it already deletes |
| VPS scheduled publish / reschedule | Reads Drive | **Unchanged** |
| `ATR_*.mp4` proxies | Watched as export candidates on PC2 | **Stay local to PC2** (never sent over LAN; user decision) |

Unifying pattern on PC1: one helper — *"resolve source video: local path if present, else Drive download"* — replacing the four `GoogleDriveService.download_file` call sites in `upload_phase.py` / `project_manager.py`.

## Error handling & edge cases

- **Mid-transfer WiFi drop**: per-file 3× retries with backoff, then clean job failure with a log line. Re-run re-probes and may choose Drive. No partial state: PC2 verifies byte sizes against the manifest; PC1 writes uploads to a temp file with atomic rename; stale temp files swept at backend startup.
- **Relay-to-Drive failure on PC1** (quota, network blip): background retry with backoff; readiness stays green (local file exists); `execute_upload`'s ensure-upsert is the last line of defense — a permanently failed relay only surfaces when publishing while PC1's internet is down, with an explicit error.
- **Probe false positive** (ping OK, transfer stalls): covered by per-file retries + clean failure; nothing corrupts.
- **Version skew** (CEP vs backend): `ping` returns `api_version`; on mismatch the CEP logs a warning and falls back to Drive.
- **Path traversal / rogue LAN client**: `relpath` confined to the project export dir; upload filenames whitelisted; token on every `/api/lan/*` call.
- **uvicorn `--reload`**: watches `.py` files only, so `.mp4` uploads landing in `backend/data` do not restart the server (assumption re-verified during testing).
- **Concurrent projects**: endpoints stateless and per-project; jobs on different projects cannot collide.
- **ProtonVPN toggled without warning on PC1** (observed in practice): if the kill-switch filters LAN while connected, the per-job probe fails and that job transparently uses Drive; a toggle mid-transfer exhausts retries, fails the job cleanly, and the re-run re-probes. VPN state affects speed, never correctness. Milestone 0 Stage C determines whether kill-switch settings (Standard vs Advanced, LAN-allow) can make LAN work with VPN on.
- **PC1 has two LAN interfaces** (ethernet `192.168.1.76` + WiFi `192.168.1.57` on the devolo repeater): the CEP `lan_base_url` and ufw rule target the ethernet IP only; a static DHCP lease for it should be set in the box admin.

## Testing & rollout

- **Milestone 0 (gate, before feature code)**: reachability from PC2 (`ping 192.168.1.76`, then a throwaway HTTP fetch once ufw is opened) — validates the PLC repeater bridges without AP isolation/NAT; `iperf3` run to measure the real PLC+WiFi ceiling; enable ProtonVPN "allow LAN connections" on PC1.
- **Backend (pytest)**: token auth, manifest correctness, path-traversal rejection, filename whitelist, atomic upload, relay trigger (Drive mocked), local-first resolution helper (local present / absent / Drive fallback).
- **CEP (manual E2E)**: one small real project run three ways — LAN mode; `lan_base_url` emptied (must match today's behavior exactly); ufw closed mid-run to watch the fallback engage.
- **Rollout**: ships dormant. Backend endpoints harmless if unused; CEP only probes when `lan_base_url` is set. Enable = fill two settings fields on PC2; disable = empty one.

## Decisions log

1. **Keep export-time asset upload to Drive** (fallback stays free; PC1 upload is cheap on ethernet).
2. **Transport = HTTP via the existing FastAPI backend** (over Samba/SMB and Syncthing): no new system services, CEP reuses its HTTP machinery, backend knows instantly when outputs arrive, pytest-able.
3. **ATR proxies stay local to PC2** — only `output.mp4` + `output_no_music.wav` cross the LAN.
4. **PC1 relays received outputs to Drive immediately** — Drive converges to today's state, so all existing Drive-based consumers keep working unmodified.
