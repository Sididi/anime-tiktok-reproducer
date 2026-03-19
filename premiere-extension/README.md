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

Then click **Test Drive**.

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
5. auto-run `import_project.jsx`,
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

- **Port already in use**: panel logs an explicit error and does not auto-switch port.
- **Drive ambiguous match**: if multiple `SPM_*_{project_id}` folders match, job is rejected.
- **No upload after export**: verify export path is exactly `output.mp4` in downloaded project root.
- **Managed export unavailable**: check `.epr` path exists and active Premiere sequence is open.
