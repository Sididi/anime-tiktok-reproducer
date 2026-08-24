# Tiktok Reproducer - CEP Extension for Premiere Pro 2025

CEP panel for Premiere Pro 25.x with:
- classic `.trigger` / `Browse & Run` JSX execution,
- local HTTP trigger server (`localhost`),
- automatic project launch over the **Premiere Link** (WebSocket to the VPS),
- automated Google Drive download + `import_project.jsx` launch,
- `output.mp4` watch and automatic Drive upload (resumable),
- optional managed AME export from panel with encoder job tracking.

## One-time Installation

1. Close Premiere Pro and Adobe Media Encoder, then run `premiere-extension/install_extension.bat`
2. Start Premiere Pro 2025
3. Open `Window > Extensions > Tiktok Reproducer`

## One-time Setup (inside panel)

Fill and save in **Automation Settings**:
- `Drive Client ID`
- `Drive Client Secret`
- `Drive Refresh Token`
- `Drive Parent Folder ID`
- `Local Server Port` (default: `48653`)
- `Premiere Link URL` (default `wss://tiktok.sididi.tv/api/cep/ws`) — see **Premiere Link** below
- `Premiere Link token` — must match the VPS `ATR_CEP_LINK_TOKEN`
- `AME Preset (.epr)` path (required for **Export via CEP**)

Then click **Test Drive**, and **Test Link** once the Premiere Link fields are filled.

## Premiere Link (VPS -> CEP, automatic launch)

When the backend finishes a project's Drive export (first upload or re-upload
from `/processing`), it asks the VPS to launch that project here. The panel
keeps a WebSocket open to the VPS (`Premiere Link URL`, authenticated with
`Premiere Link token` = the VPS `ATR_CEP_LINK_TOKEN`); each `launch` frame runs
**exactly the same intake as opening `http://localhost:{PORT}/p/{project_id}`**:
same download/import queue, same sleeping queue during export/cleanup/final
acknowledgement, same per-session duplicate rule.

- **Replay**: launches requested while Premiere was closed are held by the VPS
  (7 days) and delivered as soon as the panel connects.
- **Outcome is never silent**: the panel logs `accepted` / `duplicate` /
  `error`, and the VPS appends a `Premiere: …` line to the project's Discord
  message (⏳ waiting for the panel · ✅ accepted HH:MM · ⚠️ duplicate (already
  run this session) · ❌ error · ⌛ expired).
- **Duplicate**: a project already handled in this Premiere session is ignored,
  exactly like clicking its Discord link twice.
- **Fallback**: the Discord message still carries the `localhost` link; opening
  it works unchanged. `Test Link` in the settings opens a throwaway socket and
  reports `Link OK (pending: N)`.
- **Reconnect**: jittered backoff 1 s → 60 s; a rejected token (4401) retries
  every 60 s until the settings are fixed. `GET /health` on the local server
  exposes `link_enabled` / `link_connected` / `link_last_error`.
- **Single device**: whoever holds the token *is* the panel — two panels with
  the same token both receive every launch.
- The link only starts after the host build check passes; with a mismatched
  `host.jsx` launches stay ⏳ on the VPS until the extension is reinstalled.

## Trigger Contract (Discord -> CEP)

Backend sends links in this format:

`http://localhost:{PORT}/p/{project_id}`

The panel runs a local server bound to `127.0.0.1` with endpoints:
- `GET /health`
- `GET /p/{project_id}`
- `GET /status/{project_id}`

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
- proxy generation/reconciliation is stopped and managed proxies are detached,
- every open Premiere project containing the downloaded source/proxy paths is purged,
- Premiere verifies that no source or proxy links to those paths remain,
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
