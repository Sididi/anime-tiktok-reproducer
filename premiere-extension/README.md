# Tiktok Reproducer - CEP Extension for Premiere Pro 2025

CEP panel for Premiere Pro 25.x with:
- classic `.trigger` / `Browse & Run` JSX execution,
- local HTTP trigger server (`localhost`),
- automated Google Drive download + `import_project.jsx` launch,
- `output.mp4` watch and automatic Drive upload (resumable),
- optional managed AME export from panel with encoder job tracking.

## One-time Installation

1. Run `premiere-extension/install_extension.bat`
2. Restart Premiere Pro 2025
3. Open `Window > Extensions > Tiktok Reproducer`

## One-time Setup (inside panel)

Fill and save in **Automation Settings**:
- `Drive Client ID`
- `Drive Client Secret`
- `Drive Refresh Token`
- `Drive Parent Folder ID`
- `Local Server Port` (default: `48653`)
- `AME Preset (.epr)` path (required for **Export via CEP**)
- `LAN base URL (empty = Drive only)` — optional; see **LAN Transfer** below
- `LAN token` — optional; must match the backend's `ATR_LAN_TRANSFER_TOKEN`

Then click **Test Drive**.

## LAN Transfer (optional, faster on a local network)

When the backend PC and this Premiere PC are on the same LAN, the panel can
download project assets from — and upload render outputs to — the backend
directly over HTTP, skipping the Google Drive round-trip. Drive stays the
automatic fallback and is still used by the distant VPS for scheduled
publishing, so end state on Drive is unchanged.

**How selection works:** before each download/upload job the panel probes
`GET {LAN base URL}/api/lan/ping`. On success it uses the LAN engine for that
job; on any failure (unreachable, wrong token, version mismatch) it silently
falls back to the Drive engine. A fresh probe runs per job, so a transient
network hiccup never poisons later jobs.

**Enable (on this PC):** in Automation Settings set
- `LAN base URL` = `http://<backend-host>:8000` (mDNS name recommended, e.g.
  `http://arch-sid.local:8000`, so it survives IP changes)
- `LAN token` = the same value as the backend's `ATR_LAN_TRANSFER_TOKEN`

With both fields configured, the CEP HTTP listener also binds to the LAN for
authenticated automatic project starts. The local Discord URL continues to
work unchanged.

**Disable:** clear `LAN base URL`. The panel then behaves exactly as before
(Drive only) with no probe.

**Backend side:** run the backend bound to `0.0.0.0` (the `backend` pixi task
already does this), set `ATR_LAN_TRANSFER_TOKEN` in the backend `.env`, and
firewall port `8000` to the local subnet.

**Backend kill switch:** set `ATR_LAN_TRANSFER_ENABLED=false` in the backend
`.env` to disable the feature entirely server-side — `/api/lan/*` returns 503
(so the panel's probe fails and it falls back to Drive automatically) and the
backend's local-first reads are bypassed, reverting behavior to pure Drive. Only `/api/lan/*` is token-guarded;
binding `0.0.0.0` exposes the rest of the API to the LAN, which is why the
firewall rule is required.

**Scope:** only `output.mp4` and `output_no_music.wav` are uploaded over the
LAN; `ATR_*` proxy files stay local to this PC. On receipt the backend relays
the outputs to Drive automatically.

## Trigger Contract (Discord -> CEP)

Backend sends links in this format:

`http://localhost:{PORT}/p/{project_id}`

The panel runs a local server bound to `127.0.0.1` with endpoints:
- `GET /health`
- `GET /p/{project_id}`
- `GET /status/{project_id}`

When LAN mode is configured, the same server listens on all network interfaces,
but these historical endpoints remain restricted to requests originating from
the Premiere computer. This preserves the existing Discord-link behavior.

## Automatic LAN Trigger (Backend -> CEP)

With a non-empty `LAN base URL` and `LAN token`, the backend can start a ready
project without anyone opening its Discord URL:

`POST http://<premiere-host>:48653/api/lan/projects/{project_id}/start`

Send the shared token in the `X-ATR-LAN-Token` header. A successful request
returns HTTP 202. `accepted: true` means the project was newly accepted;
`duplicate: true` means it was already seen in the current Premiere session.

The endpoint calls the same project intake used by `/p/{project_id}`. During
the intake phase it enters the normal download/import queue. During export,
cleanup, final acknowledgement, or a blocked batch it enters the sleeping
queue and is promoted after the current batch finishes.

The backend can check reachability and API compatibility first with:

`GET http://<premiere-host>:48653/api/lan/ping`

This request uses the same token header and returns `api_version: 1`. Use the
Premiere computer's mDNS hostname where available (for example,
`premiere-pc.local`) so DHCP address changes do not break the trigger. Allow
inbound TCP traffic to the configured CEP port from the backend computer in
the Premiere computer's firewall.

On `/p/{project_id}`:
1. resolve Drive folder `SPM_*_{project_id}` under configured parent,
2. download folder recursively into a fresh local folder suffixed with `_hhhh`,
3. extract `subtitles/atr_subtitles.zip` when present,
4. write `.atr_project_context.json`,
5. auto-run the downloaded `import_project.jsx` unchanged,
6. arm `output.mp4` monitor.

## Export and Upload Flow

### Manual export (recommended baseline)

Export path must be:

`<downloaded_project_folder>/output.mp4`

When file is stable for 10s, panel uploads it automatically to the same Drive folder root as `output.mp4` (overwrite/update behavior).

### Managed export via panel

Select tracked project and click **Export via CEP**.
The panel starts AME export with configured `.epr`, tracks encoder job events, then triggers upload when export completes.

## Cleanup option

Checkbox **Delete local folder after successful upload** is enabled by default.
Deletion happens only after:
- a confirmed successful Drive upload,
- Premiere project cleanup fully purges the active ATR project,
- the local downloaded folder is fully removed.

## Reliability / Recovery

State is persisted in:

`%APPDATA%\Adobe\TiktokReproducer\state\`

Legacy `%APPDATA%\Adobe\JSXRunner\...` state is migrated automatically on first run when possible.

Files:
- `settings.json`
- `projects/<project_id>.json`
- `upload_sessions/<project_id>.json` (resumable upload session)

After Premiere restart, tracked project state is restored for UI visibility and manual actions only.
Queued/in-progress jobs are **not** resumed automatically, export monitors are **not** re-armed automatically, and cleanup retries are **not** restarted automatically.
Transient states left by a crash/restart are normalized to manual-intervention states when the panel boots again.

## Legacy trigger still supported

The historical flow still works:
- `.bat` writes `.trigger` in `%APPDATA%\Adobe\TiktokReproducer\inbox`
- panel watches inbox and runs referenced `.jsx`

## Troubleshooting

- **Host build mismatch / automation disabled**: reinstall the extension and restart Premiere Pro. The panel intentionally refuses automatic work when its JavaScript and the loaded persistent `host.jsx` are from different builds.
- **Port already in use**: panel logs an explicit error and does not auto-switch port.
- **Drive ambiguous match**: if multiple `SPM_*_{project_id}` folders match, job is rejected.
- **No upload after export**: verify export path is exactly `output.mp4` in downloaded project root.
- **Managed export unavailable**: check `.epr` path exists and active Premiere sequence is open.
