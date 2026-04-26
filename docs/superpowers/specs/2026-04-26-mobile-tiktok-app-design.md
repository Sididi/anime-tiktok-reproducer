# Mobile TikTok Posting Flow — Design

**Date:** 2026-04-26
**Status:** Approved (brainstorming complete, awaiting writing-plans)
**Scope:** Replace the manual "download from Discord, post to TikTok by hand" flow with a custom mobile app (iOS + Android) backed by a VPS-deployed server. Includes a Discord bot upgrade, a single rich-embed model, removal of the existing strikethrough-on-overdue system, and a new reminder channel.

---

## 1. Goals & Non-Goals

### Goals

- Eliminate the manual phone workflow (download from Drive, switch to TikTok, paste description) by giving the operator a one-tap "Open TikTok" action on their phone.
- Support multiple phones (iOS + Android) and multiple TikTok accounts per phone.
- Replace the lazy "open Project Manager to cross out overdue uploads" model with an event-driven Discord reaction added when the operator actually shares.
- Upgrade Discord output from plain text + per-channel webhook to a single bot-managed rich embed per upload that updates in place as platform statuses change.
- Add a reminder channel that pings `@Tiktok Reproducer` at slot time with a native forward of the embed.

### Non-Goals

- Posting to TikTok directly via the Content Posting API. The flow uses TikTok's mobile OpenSDK Share Kit, which opens TikTok's editor with the video pre-loaded for the currently active account. Server-side INBOX uploads were considered and rejected (Section 11).
- Multi-user / multi-operator support. The system is single-operator with N phones.
- Automatic background posting. The operator always taps to share; the app is a preparation/handoff tool, not an autoposter.
- Analytics on posted TikTok videos.
- Migration of pre-existing in-flight projects. The new flow applies from deployment forward.

---

## 2. Architecture Overview

Three runtime components:

1. **Existing main backend** (`backend/`, runs on the operator's dev machine). Continues to handle video processing, transcription, scene detection, YT/FB/IG uploads. Stops talking to Discord directly; instead talks to the VPS server for all Discord-related operations.
2. **New VPS server** (`server/`, deployed at `https://tiktok.sididi.tv`). Owns the TikTok job lifecycle: persistence, mobile API surface, all Discord bot interactions, avatar serving, reminder channel.
3. **New mobile app** (`mobile/`, React Native bare workflow). Installed on each operator phone. Talks only to the VPS server.

### End-to-end flow for one upload

1. Main backend's existing upload phase fires for project P at slot time.
2. Main backend calls `POST tiktok.sididi.tv/api/internal/jobs` with `{project_id, account_id, slot_time, anime_title, description, drive_video_url, platforms_requested}`.
3. VPS creates a `TikTokJob` row, posts a rich embed in the upload channel, posts a forward+ping in the reminder channel, returns `{job_id, discord_message_id}` to the main backend.
4. As main backend's YT/FB/IG uploads complete, it calls `POST tiktok.sididi.tv/api/internal/jobs/{project_id}/platform-status` with `{platform, status, url, detail}`. VPS edits the embed in place.
5. Operator sees Discord ping (reminder channel), opens the mobile app on the relevant phone.
6. App calls `GET /api/mobile/jobs` (filtered server-side by device token), renders pending jobs.
7. Operator taps "Open TikTok" on a card. App calls `GET /api/mobile/jobs/{id}/video-url`, downloads to `RNFS.CachesDirectoryPath/{job_id}.mp4`.
8. On download complete: app copies description to clipboard, calls `POST /api/mobile/jobs/{id}/ack`, then invokes the native `TikTokShareBridge.share(...)` which opens TikTok's editor with the video.
9. VPS's ack handler edits the embed (TikTok line → uploaded with timestamp), adds ✅ reaction.
10. Operator pastes description in TikTok if not pre-filled, taps Post.
11. If the operator deletes project P via Project Manager, main backend cascades: calls `DELETE tiktok.sididi.tv/api/internal/jobs/{project_id}`, VPS removes embed message + reminder message + job row.

### Service boundaries

```
┌─────────────────────────────┐         ┌──────────────────────────┐
│  Main backend (FastAPI)     │         │  Discord (bot)           │
│  - video pipeline (unchanged│         │  - upload channel embed  │
│  - DiscordService = thin    │         │  - reminder channel ping │
│    HTTP client to VPS       │         │  - ✅ reaction on ack    │
└──────────┬──────────────────┘         └──────────────────────────┘
           │ HTTPS Bearer (internal)             ▲
           │                                     │ bot REST
           ▼                                     │
┌─────────────────────────────┐                  │
│  VPS server (FastAPI)       │ ─────────────────┘
│  - TikTokJob persistence    │
│  - /api/internal/*          │
│  - /api/mobile/*            │
│  - /api/avatars/*           │
└──────────┬──────────────────┘
           │ HTTPS Bearer (per-device)
           ▼
┌─────────────────────────────┐
│  Mobile app (React Native)  │ ───── TikTok OpenSDK Share Kit
│  - Settings + Jobs screens  │       (opens TikTok editor)
│  - Native bridge to OpenSDK │
└─────────────────────────────┘
```

---

## 3. TikTok Integration Mechanism

**TikTok OpenSDK Share Kit (mobile-side share intent).** The operator's mobile app invokes TikTok's OpenSDK from the phone, passing a local video file path and (best-effort) caption hints. TikTok opens its editor with the video pre-loaded for whichever TikTok account is currently active on the device.

Account targeting is **implicit**: whichever account the user has logged into in the TikTok app is the target. If multiple accounts share a device, the operator must switch accounts in TikTok before sharing. This is acceptable per the operator's workflow and matches the single-screen one-tap UX choice (Section 8).

**Why not server-side Content Posting API:** The INBOX upload mode requires per-account OAuth maintenance for N accounts, the user would still need to manually tap Post in TikTok, and Direct Post requires app review. Share Kit is operationally simpler at the cost of "currently logged in account" being implicit.

---

## 4. Repository & Deployment Topology

### Monorepo structure

```
anime-tiktok-reproducer/                # this repo (private GitHub, single remote)
├── backend/                            # existing, unchanged in scope (only edits, no new structure)
├── frontend/                           # existing
├── server/                             # NEW: VPS-deployed FastAPI service
│   ├── app/
│   ├── config/
│   ├── avatars/
│   ├── data/
│   ├── pyproject.toml
│   └── Dockerfile
├── mobile/                             # NEW: React Native bare-workflow app
│   ├── ios/
│   ├── android/
│   └── src/
└── scripts/
    └── check-vps-config-sync.py        # NEW: validates main + VPS configs match
```

Single GitHub repository — no submodule. VPS deployment uses `Dockerfile` in `server/` with build context `server/`, so the resulting image contains only server code. Sparse-checkout or rsync of `server/` are equally valid alternatives. Submodules were considered and rejected for single-developer friction (atomic API contract changes across two repos, detached-HEAD risk).

### VPS stack

- **Caddy** as reverse proxy, auto-provisions Let's Encrypt cert for `tiktok.sididi.tv`.
- **Docker Compose** running the FastAPI service with a named volume for `data/`.
- **Systemd unit** to launch docker-compose at boot.
- No SQLite, no PostgreSQL, no Redis. JSON file persistence with an `asyncio.Lock` is sufficient for the workload.

---

## 5. Configuration

### Main backend (`config/accounts/config.yaml`)

Adds a single field per account:

```yaml
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    supported_types: ["anime"]
    avatar: "anime_fr.jpg"
    device: "iphone_13_pro"     # NEW, required
    slots: ["14:00", "18:00", "21:00"]
    youtube: {...}
    meta: {...}
```

`AccountConfig.device: str` is **required** at parse time. Missing field raises a clear `ValueError` naming the account. The full device record (platform, mobile token) lives only on VPS — main backend just needs the label so it can include it in the job payload.

### VPS slim config (`server/config/config.yaml`)

```yaml
devices:
  iphone_13_pro:
    platform: "ios"
  pixel_8:
    platform: "android"

accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_13_pro"
    avatar: "anime_fr.jpg"
  anime_en:
    name: "Anime EN"
    language: "en"
    device: "iphone_13_pro"
    avatar: "anime_en.png"
```

Deliberately omits YT/Meta tokens, slot times, and supported_types — none are needed for mobile API serving or embed rendering. Validated at startup: every account's device must reference a key in `devices`, every avatar filename must exist in `server/avatars/`. Mismatches crash startup with a clear message.

### VPS environment variables (`server/.env`)

```bash
# Authn
ATR_TIKTOK_SERVER_INTERNAL_TOKEN=...
ATR_MOBILE_TOKEN_IPHONE_13_PRO=...
ATR_MOBILE_TOKEN_PIXEL_8=...

# Discord
ATR_DISCORD_BOT_TOKEN=...
ATR_DISCORD_UPLOAD_CHANNEL_ID=...
ATR_DISCORD_REMINDER_CHANNEL_ID=...
ATR_DISCORD_REMINDER_ROLE_ID=...

# Server
ATR_SERVER_HOST=0.0.0.0
ATR_SERVER_PORT=8000
ATR_PUBLIC_BASE_URL=https://tiktok.sididi.tv
```

Per-device tokens follow the convention `ATR_MOBILE_TOKEN_<UPPER(device_id)>`. VPS resolves them at startup; missing token = startup error.

### Main backend environment additions (`backend/.env`)

```bash
ATR_TIKTOK_SERVER_BASE_URL=https://tiktok.sididi.tv
ATR_TIKTOK_SERVER_INTERNAL_TOKEN=<same value as on VPS>
```

`ATR_DISCORD_WEBHOOK_URL` is **removed** (not deprecated, deleted).

### Avatar files

Avatars **move** from `config/accounts/avatars/` to `server/avatars/` (single `git mv`). Main backend's `_avatars_dir` ([account_service.py:133](../../../backend/app/services/account_service.py#L133)) appears unused — verified during implementation, then removed.

### Config sync helper

`scripts/check-vps-config-sync.py` reads both YAMLs and asserts:
- Account ids match across both configs.
- `name`, `language`, `device`, `avatar` fields equal per account.
- Every avatar filename referenced exists in `server/avatars/`.
- Every device id referenced in main backend's accounts exists in VPS's `devices` block.

Exit 0 = OK, exit 1 = inconsistent with diff. Run manually or pre-commit. Not a runtime check.

---

## 6. VPS Server

### Module layout

```
server/
├── pyproject.toml
├── Dockerfile
├── .env.example
├── config/
│   └── config.yaml
├── avatars/
├── data/
│   └── jobs.json
├── app/
│   ├── main.py                 # FastAPI app + lifespan
│   ├── config.py               # YAML + env loading + validation
│   ├── api/
│   │   ├── internal.py
│   │   ├── mobile.py
│   │   └── public.py
│   ├── services/
│   │   ├── job_store.py
│   │   ├── discord_client.py
│   │   ├── embed_builder.py
│   │   └── reminder_service.py
│   └── models/
│       └── job.py
└── scripts/
    └── check-config.py
```

Tech: FastAPI + httpx. **No `discord.py`** — REST-only via httpx with `Authorization: Bot <token>`. Sufficient since we never listen to Discord events. Persistence: a single JSON file at `data/jobs.json` with an `asyncio.Lock`, matching existing `tiktok_url_db_service.py` conventions.

### API surface

#### Internal API — `/api/internal/*`
Auth: `Authorization: Bearer <ATR_TIKTOK_SERVER_INTERNAL_TOKEN>`. Used by main backend.

| Method + Path | Purpose |
|---|---|
| `POST /api/internal/jobs` | Create job + post embed + reminder. Body: `{project_id, account_id, slot_time, anime_title, description, drive_video_url, platforms_requested}`. Returns `{job_id, discord_message_id}`. Idempotent — same `project_id` returns existing job without re-posting. |
| `POST /api/internal/jobs/{project_id}/platform-status` | Body: `{platform, status, url?, detail?}`. Merges status into job, rebuilds embed, edits Discord message. No-op if state unchanged. |
| `DELETE /api/internal/jobs/{project_id}` | Cascade-delete: removes embed message, reminder message, job row. Returns 200 even if job doesn't exist. |
| `POST /api/internal/discord/messages` | Generic message post. Body: `{channel_id?, content, embed?}`. Returns `{message_id}`. |
| `PATCH /api/internal/discord/messages/{id}` | Generic edit. Body: `{content?, embed?}`. |
| `DELETE /api/internal/discord/messages/{id}` | Generic delete. |

Jobs are addressed by `project_id` (not a server-side job id) so the main backend doesn't need to remember a separate identifier — it already has `project.id`.

#### Mobile API — `/api/mobile/*`
Auth: `Authorization: Bearer <per-device-token>`. The token resolves to a `device_id` on every request via FastAPI dependency.

| Method + Path | Purpose |
|---|---|
| `GET /api/mobile/jobs` | List pending jobs for this device. Returns `[{job_id, project_id, account_id, account_name, account_avatar_url, anime_title, description, slot_time, status}, ...]`. Filtered to `status=pending` and `account.device == this_device`. |
| `GET /api/mobile/jobs/{id}/video-url` | Returns `{video_url}`. The stored Drive URL passed in by main backend at job creation. |
| `POST /api/mobile/jobs/{id}/ack` | Marks job acked, sets TikTok platform status to uploaded with timestamp, rebuilds embed, adds ✅ reaction. Idempotent. |
| `GET /api/mobile/me` | Returns `{device_id, accounts: [{id, name, avatar_url}]}`. Used by Settings screen to verify token and populate account list. |

#### Public API — `/api/avatars/*`
No auth.

| Method + Path | Purpose |
|---|---|
| `GET /api/avatars/{filename}` | Serves files from `server/avatars/` with `Content-Type: image/*` and aggressive cache headers. 404 if missing. |

### Auth middleware

Two FastAPI dependencies, declared once and reused:

```python
async def require_internal_token(authorization: str = Header(...)) -> None:
    expected = settings.internal_api_token
    if not authorization.startswith("Bearer ") or authorization[7:] != expected:
        raise HTTPException(401)

async def require_device_token(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401)
    token = authorization[7:]
    device_id = settings.resolve_device_for_token(token)
    if device_id is None:
        raise HTTPException(401)
    return device_id
```

### Discord bot client (`discord_client.py`)

Pure REST client (~150 lines):

```python
class DiscordClient:
    BASE = "https://discord.com/api/v10"

    async def post_message(self, channel_id, *, content=None, embed=None,
                           message_reference=None) -> str: ...
    async def edit_message(self, channel_id, message_id, *,
                           content=None, embed=None) -> None: ...
    async def delete_message(self, channel_id, message_id) -> None: ...
    async def add_reaction(self, channel_id, message_id, emoji) -> None: ...
```

Auth header `Authorization: Bot {ATR_DISCORD_BOT_TOKEN}`. Single shared `httpx.AsyncClient`. Retries with exponential backoff. Respects `X-RateLimit-*` headers.

### Embed builder (`embed_builder.py`)

Pure function `build_embed(job, accounts, devices) -> dict`. No I/O, no side effects, easy to unit test independently. Called any time embed state changes (job creation, platform update, mobile ack). Each consumer rebuilds and calls `discord_client.edit_message(...)`.

Embed structure:

```
┌─────────────────────────────────────────────────────────┐
│ [avatar] Anime FR                                       │   author block
│ ─────────────────────────────────────────────────────── │
│ One Piece Episode 1063 — TikTok 2x3                     │   title
│                                                         │
│ Programmé le **dimanche 26 avril 2026 à 21:00 UTC**     │   description
│                                                         │
│ ┌────────────────┬────────────────┐                     │   inline fields
│ │ 📱 Device       │ 🆔 Project      │                     │
│ │ iphone_13_pro   │ 2ee46c92a4ce    │                     │
│ └────────────────┴────────────────┘                     │
│                                                         │
│ **Plateformes**                                         │
│ ✅ YouTube     — https://youtu.be/abc                    │
│ ⚠️ Facebook   — Skipped (config)                        │
│ ⏳ Instagram  — Uploading…                              │
│ 🎯 TikTok     — Pending handoff                         │
│                                                         │
│ **Description TikTok**                                  │
│ ```                                                     │
│ description text…                                       │
│ ```                                                     │
│                                                         │
│ **Lien vidéo**                                          │
│ https://drive.google.com/...                            │
│                                                         │
│ ─── Anime FR · iphone_13_pro · 21:00 UTC                │   footer
└─────────────────────────────────────────────────────────┘
                        ✅  (added on ack)
```

### Reminder service (`reminder_service.py`)

Single function `post_reminder(job, embed_message_id) -> str`:

1. Tries Discord native FORWARD: `message_reference: {type: 1, channel_id, message_id}` + role-ping content (`<@&{ROLE_ID}> Time to post **{anime_title}** on **{account_name}** ({device_name})`).
2. On any error, falls back to pasting the message URL (`https://discord.com/channels/{guild_id}/{channel_id}/{message_id}`) in plain content.

Returns the reminder message ID, stored on the job for cascade cleanup.

### Job store (`job_store.py`)

JSON file at `data/jobs.json`, keyed by `project_id`:

```json
{
  "jobs": {
    "2ee46c92a4ce": { ...job fields... }
  }
}
```

Async methods guarded by `asyncio.Lock`:

```python
class JobStore:
    async def create(self, job: TikTokJob) -> None: ...
    async def get(self, project_id: str) -> TikTokJob | None: ...
    async def list_for_device(self, device_id: str, *, status: str | None = None) -> list[TikTokJob]: ...
    async def update(self, project_id: str, **fields) -> TikTokJob: ...
    async def delete(self, project_id: str) -> None: ...
```

Atomic writes via temp-file + rename.

### Lifespan / startup validation

1. Load YAML config + env vars.
2. Validate: every account's device exists in `devices`, every account's avatar file exists, every device has its `ATR_MOBILE_TOKEN_<...>` env var set.
3. Initialize `JobStore` (creates `data/jobs.json` if missing).
4. Initialize `DiscordClient` (single shared `httpx.AsyncClient`).
5. Bind dependencies into FastAPI app state.

Any validation failure crashes startup with a clear error.

---

## 7. Job Data Model & Lifecycle

### `TikTokJob` dataclass

```python
@dataclass
class TikTokJob:
    project_id: str                  # primary key (mirrors main backend's project.id)
    job_id: str                      # short uuid, used in mobile API paths

    # Snapshot at creation
    account_id: str
    device_id: str                   # resolved from account.device at creation
    anime_title: str
    description: str                 # full TikTok caption
    drive_video_url: str             # full URL provided by main backend
    slot_time: datetime
    platforms_requested: list[str]

    # Mutable state
    status: Literal["pending", "acked"]
    platform_statuses: dict[str, PlatformStatus]
    discord_message_id: str | None   # None if Discord post failed; ack/edit ops skip silently
    reminder_message_id: str | None
    acked_at: datetime | None

    created_at: datetime
    updated_at: datetime
```

`PlatformStatus`:
```python
@dataclass
class PlatformStatus:
    status: Literal["pending", "uploading", "uploaded", "skipped", "failed"]
    url: str | None = None
    detail: str | None = None
```

The job carries a **snapshot** of metadata. Once a job exists on VPS, the main backend can be offline forever and the mobile flow still works.

### States & transitions

Two states: `pending` and `acked`. No `expired`, no `skipped` (per operator's preference).

```
            POST /api/internal/jobs
   (none) ──────────────────────────▶ pending
                                         │
                                         │ POST /api/mobile/jobs/{id}/ack
                                         ▼
                                       acked
                                         │
   DELETE /api/internal/jobs/{pid}       │
   (any state) ────────────────────────▶ (deleted)
```

Side effects:

| Trigger | State change | Side effects |
|---|---|---|
| `POST /api/internal/jobs` | `(none) → pending` | Build embed, post to upload channel, post reminder forward, save job |
| `POST /api/internal/jobs/{pid}/platform-status` | (none) | Merge status, rebuild embed, edit Discord message |
| `POST /api/mobile/jobs/{id}/ack` | `pending → acked` | Set `acked_at`, mark TikTok status uploaded with timestamp, rebuild embed, add ✅ reaction |
| `DELETE /api/internal/jobs/{pid}` | `* → (deleted)` | Delete embed message, delete reminder message (if present), remove from job store |

### Idempotency

All write endpoints are idempotent so retries (network glitches, app re-taps) are safe:

- Same `project_id` on `POST /api/internal/jobs` → returns existing job, no re-post.
- `platform-status` with no actual change → no-op.
- `ack` on already-acked job → no-op (Discord's `add_reaction` is itself idempotent for the same bot user).
- `DELETE` on missing job → returns 200.

### Manual "Mark as posted" safety valve

If the operator posts outside the app (phone died, manual test, etc.), a long-press on a card in the mobile app triggers a confirmation dialog → `POST /api/mobile/jobs/{id}/ack` → same ack behavior on backend. Same final state as a normal share, no third state.

### Cascade-delete flow

1. Frontend → main backend `DELETE /api/managed-projects/{project_id}` ([project_manager.py:101](../../../backend/app/api/routes/project_manager.py#L101)).
2. `UploadPhaseService.managed_delete(project_id)` ([upload_phase.py:1355](../../../backend/app/services/upload_phase.py#L1355)) does its existing work, then calls `DiscordService.delete_job(project_id)` which forwards to VPS.
3. VPS deletes embed + reminder + job row.

VPS unreachable → cascade fails silently (logged), main backend's project deletion still succeeds. Orphaned VPS row acceptable (manual cleanup possible).

### Retention

Acked jobs stay in `data/jobs.json` indefinitely. No auto-expire. At ~5 jobs/day this is tens-of-records/month — nothing. Future pruning is additive if ever needed.

---

## 8. Mobile App

### Tech stack

- **React Native bare workflow** (not Expo — the TikTok OpenSDK requires custom native modules).
- **TypeScript** throughout.
- **`@react-navigation/native` + `native-stack`** for navigation.
- **`react-native-keychain`** for secure storage of backend URL + token.
- **`react-native-fs`** for the video download.
- **`@react-native-clipboard/clipboard`** for description fallback paste.
- **TikTok OpenSDK** via native iOS (CocoaPods) + Android (Gradle) dependencies.
- No state management library — `useReducer` + `AuthContext` is enough for two screens.

### File layout (`mobile/`)

```
mobile/
├── package.json
├── tsconfig.json
├── app.json
├── ios/
├── android/
└── src/
    ├── App.tsx
    ├── api/
    │   ├── client.ts
    │   └── types.ts
    ├── auth/
    │   ├── secureStore.ts
    │   └── AuthContext.tsx
    ├── screens/
    │   ├── SettingsScreen.tsx
    │   └── JobsScreen.tsx
    ├── components/
    │   ├── JobCard.tsx
    │   └── AccountAvatar.tsx
    ├── services/
    │   ├── videoDownloader.ts
    │   ├── tiktokShare.ts
    │   └── clipboard.ts
    └── native/
        └── TikTokShareBridge.ts
```

### Auth & first-launch flow

```
App launches
   │
   ├─ SecureStore has {backend_url, token}?
   │      │
   │      ├─ no  → SettingsScreen (mandatory)
   │      │           ├─ user enters URL + token
   │      │           ├─ app calls GET /api/mobile/me to validate
   │      │           ├─ on 200: persist + populate AuthContext + navigate to Jobs
   │      │           └─ on 401: show error, stay on Settings
   │      │
   │      └─ yes → call GET /api/mobile/me on launch
   │                  ├─ on 200: populate AuthContext → JobsScreen
   │                  └─ on 401: clear store, back to Settings
```

### Jobs screen (single-screen, one-tap)

Card-per-job layout:

```
┌──────────────────────────────────┐
│ [avatar] Anime FR · 21:00 UTC    │
│ One Piece Episode 1063 — TikTok  │
│ "description preview…"           │
│ ┌────────────────────────────┐   │
│ │  Open TikTok               │   │
│ └────────────────────────────┘   │
│  long-press: Mark as posted      │
└──────────────────────────────────┘
```

Refresh triggers: mount, pull-to-refresh, AppState `active`. **No polling** or background refresh — honest about lazy-fetch.

Tap "Open TikTok" state machine:

```
idle ─tap─▶ fetching-url ─ok─▶ downloading ─ok─▶ ack-then-share ─ok─▶ shared
              │                   │                  │
              ╰── error ◀─────────┴──────────────────┘
```

1. **fetching-url**: spinner. Calls `GET /api/mobile/jobs/{id}/video-url`.
2. **downloading**: progress bar 0–100%. Saves to `RNFS.CachesDirectoryPath/{job_id}.mp4`.
3. **ack-then-share** (atomic from user's POV):
   - Copy `description` to clipboard.
   - Call `POST /api/mobile/jobs/{id}/ack`.
   - Call native `TikTokShareBridge.share({videoPath, hashtags})`.
   - Show toast: "Description copiée. Colle-la dans TikTok si nécessaire."
4. **shared**: card removed (next refresh excludes acked jobs; local optimistic removal).

**Ack-before-share rationale:** no reliable callback for "share completed", network during share UX adds lag, and the manual "Mark as posted" action (Section 7) lets the operator re-resolve any rare wrong-state cases.

Long-press → confirmation dialog → `POST /api/mobile/jobs/{id}/ack` directly without launching Share Kit.

### Settings screen

Form: Backend URL, API token, "Save & verify" button. After save, displays device id + accounts assigned to this device. Sign-out clears credentials.

### Video downloader

Wraps `RNFS.downloadFile`:

```typescript
async function downloadVideo(
  url: string,
  destPath: string,
  onProgress: (bytes: number, total: number) => void
): Promise<string>
```

Saves to `RNFS.CachesDirectoryPath/{jobId}.mp4`. Best-effort cleanup of files older than 24h on app launch.

### TikTok share JS interface

```typescript
export interface ShareParams {
  videoPath: string;
  hashtags?: string[];
}

export async function shareToTikTok(params: ShareParams): Promise<void> {
  return NativeModules.TikTokShareBridge.share(params);
}
```

Resolves when share intent has been dispatched (not when the user has actually posted — outside our reach).

### Out of scope

- Login / multi-user.
- History view of acked jobs.
- Editing description before sharing.
- Background notifications (Discord remains the alert).
- Analytics, crash reporter (deferrable).
- French only, hardcoded strings. Dark mode follows system. No accessibility polish. Phone-only.

---

## 9. TikTok OpenSDK Integration

### TikTok Developer Portal (prerequisite)

1. Create developer account at `developers.tiktok.com`.
2. Create one app with iOS + Android platform configs.
3. Note client key.
4. Whitelist bundle/package IDs:
   - iOS: `tv.sididi.tiktokreproducer`
   - Android: `tv.sididi.tiktokreproducer` + SHA-1/SHA-256 of signing keystore.
5. Enable Share Kit capability.
6. **Verify whether App Review is required** for Share Kit in production. Allocate 1–2 weeks lead time if so.

### iOS bridge (`mobile/ios/`)

Files added: `TikTokShareBridge.swift`, `TikTokShareBridge.m`. Updates to `Info.plist`, `Podfile`, `AppDelegate.swift`.

Podfile:
```ruby
pod 'TikTokOpenSDKShare'
pod 'TikTokOpenSDKCore'
```

Native module sketch (verify against current SDK):

```swift
import TikTokOpenSDKShare

@objc(TikTokShareBridge)
class TikTokShareBridge: NSObject {
  @objc static func requiresMainQueueSetup() -> Bool { return true }

  @objc func share(_ params: NSDictionary,
                   resolver resolve: @escaping RCTPromiseResolveBlock,
                   rejecter reject: @escaping RCTPromiseRejectBlock) {
    guard let videoPath = params["videoPath"] as? String else {
      reject("bad_params", "videoPath required", nil); return
    }
    let hashtags = params["hashtags"] as? [String] ?? []

    let req = TikTokShareRequest()
    req.mediaType = .video
    req.localIdentifiers = [videoPath]
    req.hashtagNames = hashtags

    req.send { response in
      if let error = response.error {
        reject("share_failed", error.localizedDescription, error)
      } else {
        resolve(nil)
      }
    }
  }
}
```

`Info.plist` adds `CFBundleURLTypes` with the `tiktok{CLIENT_KEY}` scheme + `LSApplicationQueriesSchemes` with TikTok app schemes.

`AppDelegate` forwards `application(_:open:options:)` to `TikTokOpenAPIApplicationDelegate.handleOpenURL(_:)`.

### Android bridge (`mobile/android/`)

Files added: `TikTokShareModule.kt`, `TikTokSharePackage.kt`. Updates to `MainApplication.kt`, `app/build.gradle`, `AndroidManifest.xml`.

Gradle:
```gradle
implementation 'com.tiktok.open.sdk:tiktok-open-sdk-share:latest.release'
implementation 'com.tiktok.open.sdk:tiktok-open-sdk-core:latest.release'
```

Native module sketch (verify against current SDK):

```kotlin
class TikTokShareModule(reactContext: ReactApplicationContext) :
    ReactContextBaseJavaModule(reactContext) {

  override fun getName() = "TikTokShareBridge"

  @ReactMethod
  fun share(params: ReadableMap, promise: Promise) {
    val videoPath = params.getString("videoPath")
      ?: return promise.reject("bad_params", "videoPath required")
    val hashtags = params.getArray("hashtags")
      ?.toArrayList()?.map { it.toString() } ?: emptyList()
    val activity = currentActivity
      ?: return promise.reject("no_activity", "No activity")

    val shareApi = ShareApi(activity)
    val mediaContent = MediaContent(
      mediaType = MediaType.VIDEO,
      mediaPaths = arrayListOf(videoPath)
    )
    val request = Share.Request(
      mediaContent = mediaContent,
      packageName = activity.packageName,
      resultActivityFullPath = "${activity.packageName}.TikTokEntryActivity",
      hashtagList = ArrayList(hashtags)
    )
    shareApi.share(request)
    promise.resolve(null)
  }
}
```

Manifest adds `TikTokEntryActivity` with intent filter for the `tiktok{CLIENT_KEY}` scheme. On Android 11+, file paths require `FileProvider` URIs — verify SDK version's expectations.

### Caption / description handling — known unknown

Hashtag fields are stable. Full caption injection is **inconsistent** across SDK versions and TikTok app versions. Strategy:

1. Pass full description as caption via SDK if a field exists in our chosen SDK version.
2. Always extract hashtags and pass via `hashtagNames` / `hashtagList`.
3. **Always** copy full description to clipboard before firing share.
4. Show toast unconditionally so the operator knows the clipboard fallback is available.

If SDK caption injection works → operator does nothing extra. If not → operator pastes once. Either way, share proceeds.

### Verification checklist (during implementation)

- Confirm SDK versions for iOS/Android (latest stable at impl time).
- Confirm Share Kit caption-injection behavior on real devices.
- Confirm whether App Review is required for Share Kit production use.
- Confirm Android FileProvider URI requirement on Android 13+.
- Confirm TikTok URL schemes (`LSApplicationQueriesSchemes`) match current TikTok app.

These are not design blockers — clipboard fallback ensures the app works regardless.

### Failure modes

| Scenario | Handling |
|---|---|
| TikTok app not installed | Native bridge rejects with code `tiktok_not_installed`. JS shows: "TikTok app not found." |
| User cancels share inside TikTok | Backend already received ack (we ack before firing share). Operator re-shares manually via the "Mark as posted" action if needed. |
| Bundle ID mismatch / SDK init error | Native bridge throws on init; configuration error surfaced. Pre-prod issue. |
| Video file too large for Share Kit | Verify TikTok's current limit. Backend-produced TikTok encodes should fit, but worth a sanity check. |

---

## 10. Main Backend Changes

### `DiscordService` rewrite ([backend/app/services/discord_service.py](../../../backend/app/services/discord_service.py))

Becomes a thin HTTP client to VPS. Drop all webhook URL handling, message formatting, Discord API logic. New surface:

```python
class DiscordService:
    # Generic
    @classmethod
    def is_configured(cls) -> bool: ...
    @classmethod
    def post_message(cls, content, *, embed=None) -> str | None: ...
    @classmethod
    def edit_message(cls, message_id, *, content=None, embed=None) -> None: ...
    @classmethod
    def delete_message(cls, message_id) -> None: ...

    # Job-oriented
    @classmethod
    def create_job(cls, *, project_id, account_id, slot_time, anime_title,
                   description, drive_video_url, platforms_requested) -> dict: ...
    @classmethod
    def update_job_platform(cls, project_id, platform, status, *,
                            url=None, detail=None) -> None: ...
    @classmethod
    def delete_job(cls, project_id) -> None: ...
```

All methods are httpx calls to `tiktok.sididi.tv` with `Authorization: Bearer <ATR_TIKTOK_SERVER_INTERNAL_TOKEN>`. Network errors logged + swallowed (return `None` / no-op).

### `AccountConfig` ([backend/app/services/account_service.py](../../../backend/app/services/account_service.py))

Add `device: str` field (required at parse time). Update `_parse_account` and `config.example.yaml`.

### `upload_phase.py` major changes

**Delete:**
- `_format_upload_discord_message` ([upload_phase.py:444-518](../../../backend/app/services/upload_phase.py#L444-L518))
- `_cross_out_discord_message` ([upload_phase.py:378-397](../../../backend/app/services/upload_phase.py#L378-L397))
- `_cross_overdue_upload_messages` ([upload_phase.py:400-431](../../../backend/app/services/upload_phase.py#L400-L431))
- The call site at [upload_phase.py:257](../../../backend/app/services/upload_phase.py#L257)

**Replace:**
- Initial Discord message ([upload_phase.py:738](../../../backend/app/services/upload_phase.py#L738)) → `DiscordService.create_job(...)`. Stores returned `discord_message_id` on `project.final_upload_discord_message_id`.
- Edit calls ([upload_phase.py:691,980](../../../backend/app/services/upload_phase.py#L980)) → `DiscordService.update_job_platform(project_id, platform, status, url=..., detail=...)`.
- Cleanup deletes for the upload-phase message ([upload_phase.py:728,1363](../../../backend/app/services/upload_phase.py#L1363)) → `DiscordService.delete_job(project_id)`. The generation-phase message ([upload_phase.py:718,1365](../../../backend/app/services/upload_phase.py#L1365)) keeps `delete_message` (generic).

**`managed_delete`** ([upload_phase.py:1355](../../../backend/app/services/upload_phase.py#L1355)): inside existing cleanup, add `DiscordService.delete_job(project_id)` (no-op if VPS unreachable).

### `Project` model ([backend/app/models/project.py](../../../backend/app/models/project.py))

Remove `discord_upload_message_crossed: bool` field and references. Keep `final_upload_discord_message_id` and `generation_discord_message_id`.

### `processing.py` route

No semantic change. Still uses `DiscordService.post_message(...)` and `DiscordService.delete_message(...)` (now hitting VPS internally).

### `integration_health_service.py`

`is_configured()` semantics unchanged. Optionally extend health check to ping `GET tiktok.sididi.tv/healthz`.

### Frontend (Project Manager UI)

Probably zero changes — verify during implementation that no UI affordance reads `discord_upload_message_crossed`.

---

## 11. Error Handling, Edge Cases, and Constraints

### Network failure handling

| Boundary | Failure | Behavior |
|---|---|---|
| main backend → VPS | unreachable | Log + swallow. Video pipeline unaffected. Discord trail simply skipped. Project deletion succeeds. |
| mobile app → VPS | 5xx | Inline error banner with "Retry". 401 → boot to Settings. |
| mobile app → Drive | network drop | `RNFS.downloadFile` reports error → JobCard shows error + "Retry". Partial file deleted. |
| VPS → Discord | 5xx, rate-limit | DiscordClient retries with exponential backoff, respects `X-RateLimit-*`. After all retries fail, returns error to caller, which logs + swallows. Job's `discord_message_id` may be `None`; subsequent edit/delete operations skip silently. Job remains functional for mobile. |

**Principle:** Discord is best-effort. None of the platforms (main backend processing, mobile app posting) blocks on Discord working.

### Concurrency

Single `asyncio.Lock` around `JobStore` write operations. With ~5 writes/day this is comically over-provisioned. Discord rate limits: respected; volume is far below limits.

### Observability

VPS:
- `GET /healthz` returns `{status, jobs_pending, version}` (no auth).
- Structured JSON logging to stdout (captured by Docker).
- Optional: VPS errors posted to a dedicated Discord channel via the bot itself.

Main backend: existing logging — no change.

Mobile: console-only initially. Add Sentry later if needed.

### Decisions explicitly out of scope

- Server-side INBOX upload via Content Posting API (rejected: requires per-account OAuth, still requires manual tap, Direct Post needs review).
- Submodule for `server/` (rejected: friction outweighs benefit for single-developer monorepo).
- iOS public App Store release (TestFlight internal is the target).
- Web fallback / PWA equivalent of the mobile app.
- Multi-user authentication.
- Migration of in-flight projects.
- Posting destination URL capture (would require operator to paste back, friction not justified for current goals).
- iOS background download (lazy, foreground-only).
- Auto-expire of pending jobs.
- "Skip" action for jobs.

---

## 12. Rollout Plan

Four independently shippable phases:

**Phase 1 — VPS server up.**
- Build `server/`, deploy, validate `tiktok.sididi.tv` TLS.
- Manually test endpoints with `curl`: create fake job, verify embed posts, ack, reaction, reminder.
- Main backend untouched. Safe to roll back by stopping VPS.

**Phase 2 — Main backend swap to VPS Discord pipeline.**
- Refactor `DiscordService` to call VPS.
- Update `upload_phase.py` (delete cross system, swap to job-oriented calls).
- Add `device:` field to account config + populate.
- Process one project end-to-end. Verify embed, reminder, deletion cascade.
- Remove `ATR_DISCORD_WEBHOOK_URL`.

**Phase 3 — Mobile app build & distribute.**
- (gating prerequisite) TikTok Developer Portal registration: app + bundle/package IDs whitelisted, client key obtained, Share Kit enabled. No coding starts until this lands.
- iOS + Android bare RN setup with the registered bundle/package IDs.
- TikTok OpenSDK integration verified on a real test device (caption injection, FileProvider, URL schemes).
- TestFlight + Play closed test rollout.
- First end-to-end: backend creates job → app pulls → Share Kit fires → ack → reaction.

**Phase 4 — Final verification sweep.**

A residual-check pass after Phases 1-3 land. The actual deletions all happen in Phase 2; this phase just verifies nothing was missed:
- Confirm no remaining references to `_cross_overdue_upload_messages` or `_cross_out_discord_message`.
- Confirm `discord_upload_message_crossed` is gone from the `Project` model and any serializers / loaders.
- Confirm `ATR_DISCORD_WEBHOOK_URL` is gone from `.env`, `.env.example`, and code.
- Confirm `_avatars_dir` ([account_service.py:133](../../../backend/app/services/account_service.py#L133)) is removed if unused.

Each phase = one coherent commit/PR. Phase 2 depends on Phase 1; Phase 3 depends on Phase 2; Phase 4 is a small follow-up after Phase 3.

---

## 13. Open Items (resolved at implementation time, not now)

- TikTok OpenSDK current versions for iOS + Android.
- Whether Share Kit caption-injection works for full descriptions (pre-merge gate).
- Whether App Review is required for Share Kit production use.
- Android FileProvider URI requirement.
- Current TikTok URL schemes for `LSApplicationQueriesSchemes`.
- Whether main backend's `_avatars_dir` ([account_service.py:133](../../../backend/app/services/account_service.py#L133)) has any callers (likely none, verify before removal).
- Whether the frontend Project Manager UI reads `discord_upload_message_crossed` (likely not, verify before model field removal).
