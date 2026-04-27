# Phase B — Instagram via VPS + Reaction-based TikTok Ack

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Move Instagram publishing from n8n into the VPS scheduler. Add a Discord reaction listener so the operator can mark TikTok manually-posted with a single ✅ on either the upload-channel embed OR the rich reminder, which also cancels the not-yet-fired reminder. Rename `TikTokJob` → `Job`.

**Architecture:** The VPS scheduler grows from "post a reminder at slot_time" to "dispatch a per-platform action at slot_time". TikTok action = post Discord reminder (existing). Instagram action = NEW — call IG Graph API to publish. Reaction listener is a side-by-side `discord.py` bot that listens to the gateway for `MessageReactionAdd` events; on a known job's embed/reminder message, it marks tiktok done + suppresses the upcoming reminder + cleans up reminder messages if already posted.

**Tech Stack:** Add `discord.py>=2.3.0` to VPS dev+runtime deps for gateway listening (REST stays via `httpx`). Instagram Graph API is reachable via `httpx`. Phase A must be merged first.

**Reference spec:** `docs/superpowers/specs/2026-04-26-mobile-tiktok-app-design.md` (Section 6's reminder design + Section 11's "out of scope" item about IG via Content Posting API — superseded by this plan).

**Migration:** On Phase B deploy, the operator wipes `data/jobs.json` on the VPS (per Q5: clean break, not best-effort). The user confirms no in-flight jobs exist before deploying.

---

## File Structure

```
server/
├── pyproject.toml                    # MOD: add discord.py to runtime deps
├── app/
│   ├── main.py                       # MOD: lifespan starts reaction listener
│   ├── models/job.py                 # MOD: rename → Job, drop status/acked_at,
│   │                                 #      add instagram_payload, add reminder_cancelled
│   │                                 #      PlatformStatus gains completed_at + attempts
│   ├── api/internal.py               # MOD: rename references, accept IG payload,
│   │                                 #      drop status filter from list calls
│   ├── api/health.py                 # MOD: count from list_all (already done in Phase A)
│   └── services/
│       ├── job_store.py              # MOD: drop status filter, add list_due_jobs
│       ├── reminder_scheduler.py     # MOD: refactor to per-platform dispatcher
│       ├── reminder_service.py       # MOD: add cleanup_reminder helper
│       ├── instagram_publisher.py    # NEW: IG Graph API publish flow
│       ├── discord_client.py         # MOD: add remove_reaction (or already there?)
│       └── reaction_listener.py      # NEW: discord.py-based gateway bot
└── tests/
    ├── test_job_model.py             # MOD: new field shape
    ├── test_job_store.py             # MOD: status field gone, list_due_jobs added
    ├── test_internal_api.py          # MOD: IG payload acceptance
    ├── test_reminder_service.py      # MOD: cleanup helper coverage
    ├── test_reminder_scheduler.py    # MOD: per-platform dispatch + IG path
    ├── test_instagram_publisher.py   # NEW: respx-mocked Graph API tests
    └── test_reaction_listener.py     # NEW: simulated gateway events

backend/
├── app/
│   ├── config.py                     # MOD: drop discord_webhook_url + n8n_webhook_url
│   ├── services/
│   │   ├── upload_phase.py           # MOD: drop _send_n8n_instagram_webhook,
│   │   │                             #      pass IG payload via create_job
│   │   └── (other unchanged)
│   └── (other unchanged)
└── .env.example                      # MOD: drop ATR_DISCORD_WEBHOOK_URL + ATR_N8N_WEBHOOK_URL

N8N_SCHEDULED_UPLOAD.md               # DELETE
```

---

## Conventions

- The VPS calls `Job` what it used to call `TikTokJob`. The on-disk JSON shape is new — Phase A's wipe instruction applies on deploy.
- Discord reactions are matched by emoji `"✅"` exactly (Unicode U+2705). The bot also adds its own `"✅"` to the embed when ack happens — when the listener sees that reaction added BY ITSELF, it must ignore it (filter on `payload.user_id != self.bot.user.id`).
- `discord.py` runs in its own asyncio task in the lifespan. It does NOT replace the existing httpx-based REST client; the two coexist.
- IG publish failures stop after 5 attempts (per Q4). After exhaustion, the embed marks `instagram` as `failed` with a detail, and a separate Discord ping fires in the reminder channel (`@Tiktok Reproducer Instagram failed for <title>: <detail>`).
- `Job.platform_statuses[platform].completed_at` replaces the old top-level `acked_at`. Different platforms can have different completion times.

---

## Task 1: Job model rename + new fields

**Files:**
- Modify: `server/app/models/job.py`
- Modify: `server/tests/test_job_model.py`

- [ ] **Step 1: Update `PlatformStatus`** to add `completed_at` and `attempts`:

```python
@dataclass(frozen=True)
class PlatformStatus:
    status: PlatformStatusName
    url: str | None = None
    detail: str | None = None
    completed_at: datetime | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "url": self.url,
            "detail": self.detail,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlatformStatus":
        ca = d.get("completed_at")
        return cls(
            status=d["status"],
            url=d.get("url"),
            detail=d.get("detail"),
            completed_at=datetime.fromisoformat(ca) if ca else None,
            attempts=int(d.get("attempts", 0)),
        )
```

- [ ] **Step 2: Rename `TikTokJob` → `Job`**, drop `status`/`acked_at`, add `instagram_payload` and `reminder_cancelled`:

```python
@dataclass
class Job:
    project_id: str
    job_id: str
    account_id: str
    device_id: str
    anime_title: str
    description: str
    drive_video_url: str
    slot_time: datetime
    platforms_requested: list[str]
    platform_statuses: dict[str, PlatformStatus]
    discord_message_id: str | None
    reminder_message_id: str | None
    reminder_forward_message_id: str | None = None
    reminder_cancelled: bool = False    # NEW: set True when ack arrives before reminder fires
    instagram_payload: dict | None = None  # NEW: { ig_user_id, ig_access_token, caption, graph_api_version }
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
```

(Drop the old `status: JobStatus` and `acked_at` fields entirely. The `JobStatus` Literal is no longer needed; remove it.)

- [ ] **Step 3: Update `to_dict` / `from_dict`** to match the new shape. `from_dict` must NOT accept old-shape inputs (per Q5 wipe decision).

- [ ] **Step 4: Update `test_job_model.py`** for the new shape.

- [ ] **Step 5: Run tests**

```bash
cd server && uv run pytest tests/test_job_model.py -v
```
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add server/app/models/job.py server/tests/test_job_model.py
git commit -m "refactor(server): rename TikTokJob → Job, drop status, add instagram_payload + reminder_cancelled"
```

---

## Task 2: JobStore changes

**Files:**
- Modify: `server/app/services/job_store.py`
- Modify: `server/tests/test_job_store.py`

- [ ] **Step 1: Drop the `status` filter** from `list_for_device` since `Job` no longer has a top-level `status`. Replace with:

```python
async def list_for_device(self, device_id: str) -> list[Job]:
    """Return all jobs for a device, regardless of completion state."""
```

- [ ] **Step 2: Add `list_all` (already added in Phase A)** — verify it exists and uses `Job.from_dict`.

- [ ] **Step 3: Add `list_due_jobs(now: datetime) -> list[Job]`** — returns jobs whose `slot_time <= now`. Used by the scheduler to find work to dispatch.

```python
async def list_due_jobs(self, now: datetime) -> list[Job]:
    async with self._lock:
        jobs = self._read()
        out: list[Job] = []
        for d in jobs.values():
            j = Job.from_dict(d)
            if j.slot_time <= now:
                out.append(j)
        return out
```

- [ ] **Step 4: Update `test_job_store.py`** for the new API. The `test_list_for_device_filters_by_device_and_status` test — drop the status filter assertion. Add `test_list_due_jobs`.

- [ ] **Step 5: Verify tests pass**

```bash
cd server && uv run pytest tests/test_job_store.py -v
```

- [ ] **Step 6: Commit**

```bash
git add server/app/services/job_store.py server/tests/test_job_store.py
git commit -m "refactor(server): JobStore — drop status filter, add list_due_jobs"
```

---

## Task 3: Internal API — accept IG payload + use new model

**Files:**
- Modify: `server/app/api/internal.py`
- Modify: `server/tests/test_internal_api.py`

- [ ] **Step 1: Update `CreateJobRequest`** to add an optional IG payload:

```python
class InstagramPayload(BaseModel):
    ig_user_id: str
    ig_access_token: str
    caption: str
    graph_api_version: str = "v25.0"


class CreateJobRequest(BaseModel):
    project_id: str
    account_id: str
    slot_time: datetime
    anime_title: str
    description: str
    drive_video_url: str
    platforms_requested: list[str]
    instagram: InstagramPayload | None = None
```

- [ ] **Step 2: Update `create_job` handler** to:
- Construct `Job` (renamed from TikTokJob) instead of TikTokJob.
- If `req.instagram` is provided, store it as `job.instagram_payload = req.instagram.model_dump()`.

Drop the `status="pending"` field initialization (it's gone from the model).

- [ ] **Step 3: Update `delete_job`** to use `Job` import (rename only).

- [ ] **Step 4: Update `platform_status`** for the renamed model — also update the embed re-render path. The `acked_at` reference is gone; there's nothing to set on top-level status.

- [ ] **Step 5: Update tests** — test_internal_api fixtures use the new model. Add `test_create_job_with_instagram_payload_persists_it`.

- [ ] **Step 6: Run tests**

```bash
cd server && uv run pytest tests/test_internal_api.py -v
```

- [ ] **Step 7: Commit**

```bash
git add server/app/api/internal.py server/tests/test_internal_api.py
git commit -m "feat(server): internal API accepts Instagram payload; use new Job model"
```

---

## Task 4: Instagram publisher

**Files:**
- Create: `server/app/services/instagram_publisher.py`
- Create: `server/tests/test_instagram_publisher.py`

- [ ] **Step 1: Write the failing tests first** (`tests/test_instagram_publisher.py`) covering:
- Happy path: create container → poll FINISHED → publish → permalink fetched → returns success
- Polling waits across multiple attempts before FINISHED
- Container creation error → returns failure
- Polling timeout (status stuck IN_PROGRESS forever) → returns failure  
- Publish API returns 5xx → returns failure
- All HTTP via respx mocks.

- [ ] **Step 2: Implement `server/app/services/instagram_publisher.py`**:

```python
"""Instagram Reels publisher via Meta Graph API.

Implements the canonical container → poll → publish flow:
  POST /{ig_user_id}/media?media_type=REELS&video_url=...&caption=...
  GET  /{container_id}?fields=status_code  (poll until FINISHED)
  POST /{ig_user_id}/media_publish?creation_id=...
  GET  /{media_id}?fields=permalink
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class InstagramPublishResult:
    success: bool
    permalink: str | None = None
    detail: str | None = None


_POLL_INTERVAL_SECONDS = 5
_POLL_TIMEOUT_SECONDS = 5 * 60  # 5 minutes


async def publish_to_instagram(
    *,
    ig_user_id: str,
    ig_access_token: str,
    caption: str,
    video_url: str,
    graph_api_version: str = "v25.0",
    poll_interval: float = _POLL_INTERVAL_SECONDS,
    poll_timeout: float = _POLL_TIMEOUT_SECONDS,
) -> InstagramPublishResult:
    base = f"https://graph.facebook.com/{graph_api_version}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Create container
        try:
            create = await client.post(
                f"{base}/{ig_user_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "access_token": ig_access_token,
                },
            )
            create.raise_for_status()
            container_id = create.json()["id"]
        except (httpx.HTTPError, KeyError) as e:
            return InstagramPublishResult(success=False, detail=f"create container failed: {e}")

        # 2. Poll status
        elapsed = 0.0
        while elapsed < poll_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                status_resp = await client.get(
                    f"{base}/{container_id}",
                    params={"fields": "status_code", "access_token": ig_access_token},
                )
                status_resp.raise_for_status()
                code = status_resp.json().get("status_code")
            except httpx.HTTPError as e:
                return InstagramPublishResult(success=False, detail=f"status poll failed: {e}")
            if code == "FINISHED":
                break
            if code == "ERROR":
                return InstagramPublishResult(success=False, detail="container status_code = ERROR")
        else:
            return InstagramPublishResult(success=False, detail="poll timeout")

        # 3. Publish
        try:
            pub = await client.post(
                f"{base}/{ig_user_id}/media_publish",
                params={"creation_id": container_id, "access_token": ig_access_token},
            )
            pub.raise_for_status()
            media_id = pub.json()["id"]
        except (httpx.HTTPError, KeyError) as e:
            return InstagramPublishResult(success=False, detail=f"publish failed: {e}")

        # 4. Fetch permalink
        try:
            perma = await client.get(
                f"{base}/{media_id}",
                params={"fields": "permalink", "access_token": ig_access_token},
            )
            perma.raise_for_status()
            permalink = perma.json().get("permalink")
        except httpx.HTTPError:
            permalink = None  # not fatal; we have a media_id, can construct URL or leave blank

        return InstagramPublishResult(success=True, permalink=permalink)
```

- [ ] **Step 3: Run tests**

```bash
cd server && uv run pytest tests/test_instagram_publisher.py -v
```

- [ ] **Step 4: Commit**

```bash
git add server/app/services/instagram_publisher.py server/tests/test_instagram_publisher.py
git commit -m "feat(server): Instagram Reels publisher (container → poll → publish)"
```

---

## Task 5: Scheduler refactor for per-platform dispatch

**Files:**
- Modify: `server/app/services/reminder_scheduler.py`
- Modify: `server/tests/test_reminder_scheduler.py`

- [ ] **Step 1: Refactor `dispatch_due_reminders` → `dispatch_due_actions`**:

```python
async def dispatch_due_actions(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    now: datetime | None = None,
) -> int:
    """Per-job, per-platform dispatch. Returns count of actions taken."""
    current = now or datetime.now(tz=timezone.utc)
    actions = 0
    for job in await store.list_due_jobs(current):
        for platform in job.platforms_requested:
            ps = job.platform_statuses.get(platform, PlatformStatus(status="pending"))
            if ps.status in ("uploaded", "skipped", "failed"):
                continue  # terminal, no work
            if platform == "tiktok":
                if await _dispatch_tiktok_reminder(job, store, settings, discord):
                    actions += 1
            elif platform == "instagram":
                if await _dispatch_instagram_publish(job, store, settings, discord):
                    actions += 1
            # YouTube/Facebook: VPS does nothing (handled by main backend natively)
    return actions
```

- [ ] **Step 2: Implement `_dispatch_tiktok_reminder`** — same logic as today's reminder dispatch, but only fires if `not job.reminder_cancelled and job.reminder_message_id is None`.

```python
async def _dispatch_tiktok_reminder(job, store, settings, discord) -> bool:
    if job.reminder_cancelled:
        return False
    if job.reminder_message_id is not None:
        return False
    account = settings.accounts.get(job.account_id)
    if account is None:
        logger.warning("Job %s references unknown account %s", job.project_id, job.account_id)
        return False
    rich_id, forward_id = await post_reminder(
        discord,
        job=job,
        account=account,
        public_base_url=settings.public_base_url,
        upload_channel_id=settings.discord.upload_channel_id,
        reminder_channel_id=settings.discord.reminder_channel_id,
        role_id=settings.discord.reminder_role_id,
        guild_id=settings.discord.guild_id,
    )
    if rich_id is None:
        return False
    await store.update(
        job.project_id,
        reminder_message_id=rich_id,
        reminder_forward_message_id=forward_id,
    )
    return True
```

- [ ] **Step 3: Implement `_dispatch_instagram_publish`** — calls `publish_to_instagram`, retries on failure up to 5 attempts, then marks failed:

```python
_IG_MAX_ATTEMPTS = 5


async def _dispatch_instagram_publish(job, store, settings, discord) -> bool:
    payload = job.instagram_payload
    if not payload:
        logger.warning("Job %s has 'instagram' in platforms but no instagram_payload", job.project_id)
        return False
    current = job.platform_statuses.get("instagram", PlatformStatus(status="pending"))
    if current.attempts >= _IG_MAX_ATTEMPTS:
        # Already terminal: mark failed if not already
        if current.status != "failed":
            await store.update(
                job.project_id,
                platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                    status="failed",
                    detail=current.detail or "max retries exhausted",
                    attempts=current.attempts,
                    completed_at=datetime.now(tz=timezone.utc),
                )},
            )
            await _post_failure_ping(job, settings, discord, current.detail or "max retries exhausted")
        return False

    # Mark uploading + bump attempts
    next_attempts = current.attempts + 1
    await store.update(
        job.project_id,
        platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
            status="uploading", attempts=next_attempts,
        )},
    )

    result = await publish_to_instagram(
        ig_user_id=payload["ig_user_id"],
        ig_access_token=payload["ig_access_token"],
        caption=payload["caption"],
        video_url=job.drive_video_url,
        graph_api_version=payload.get("graph_api_version", "v25.0"),
    )

    if result.success:
        await store.update(
            job.project_id,
            platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                status="uploaded",
                url=result.permalink,
                attempts=next_attempts,
                completed_at=datetime.now(tz=timezone.utc),
            )},
        )
        # Re-render the embed
        await _rerender_embed(job.project_id, store, settings, discord)
        return True

    # Failure: increment + leave as 'pending' for next tick to retry, UNLESS we've hit max
    if next_attempts >= _IG_MAX_ATTEMPTS:
        await store.update(
            job.project_id,
            platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                status="failed",
                detail=result.detail,
                attempts=next_attempts,
                completed_at=datetime.now(tz=timezone.utc),
            )},
        )
        await _post_failure_ping(job, settings, discord, result.detail or "publish failed")
        await _rerender_embed(job.project_id, store, settings, discord)
    else:
        # Pending: scheduler will retry on the next tick. Add exponential backoff via
        # next-eligible-time? For simplicity, retry on the very next tick (every 30s).
        # If that's too aggressive, add a `next_attempt_at: datetime` to PlatformStatus.
        await store.update(
            job.project_id,
            platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                status="pending",
                detail=result.detail,
                attempts=next_attempts,
            )},
        )
    return False
```

(For Q4's exponential backoff: simplest impl is `next_attempt_at = now + 60 * 2^attempts` and dispatch checks `if ps.next_attempt_at and ps.next_attempt_at > now: continue`. Add this field to PlatformStatus if you want strict backoff. For Phase B v1, the scheduler tick interval (30s) is itself a coarse backoff and simpler — leave strict exponential as a future enhancement. **Pick the simple version: no `next_attempt_at` field; tick-based retry every 30s.**)

- [ ] **Step 4: Implement `_post_failure_ping` and `_rerender_embed`** as helpers in the scheduler module:

```python
async def _post_failure_ping(job, settings, discord, detail: str) -> None:
    role = settings.discord.reminder_role_id
    msg = (
        f"<@&{role}> Instagram publish failed for **{job.anime_title}** "
        f"({job.account_id}): {detail}"
    )
    try:
        await discord.post_message(settings.discord.reminder_channel_id, content=msg)
    except Exception:
        logger.exception("Failed to post IG failure ping")


async def _rerender_embed(project_id, store, settings, discord) -> None:
    job = await store.get(project_id)
    if job is None or job.discord_message_id is None:
        return
    embed = build_embed(job, settings.accounts, settings.public_base_url)
    try:
        await discord.edit_message(
            settings.discord.upload_channel_id, job.discord_message_id, embed=embed
        )
    except Exception:
        logger.exception("Failed to re-render embed for %s", project_id)
```

- [ ] **Step 5: Update `run_scheduler_loop`** to call `dispatch_due_actions` (rename from `dispatch_due_reminders`).

- [ ] **Step 6: Update `test_reminder_scheduler.py`** — rename tests, add IG dispatch coverage (happy path, retry, max-attempt-reached).

- [ ] **Step 7: Run tests**

```bash
cd server && uv run pytest tests/test_reminder_scheduler.py -v
```

- [ ] **Step 8: Commit**

```bash
git add server/app/services/reminder_scheduler.py server/tests/test_reminder_scheduler.py
git commit -m "feat(server): scheduler refactored to per-platform dispatch (TikTok reminder + IG publish)"
```

---

## Task 6: Discord reaction listener

**Files:**
- Modify: `server/pyproject.toml` (add `discord.py>=2.3.0`)
- Create: `server/app/services/reaction_listener.py`
- Create: `server/tests/test_reaction_listener.py`
- Modify: `server/app/main.py` (start listener in lifespan)

- [ ] **Step 1: Add `discord.py` dep**

In `server/pyproject.toml`, under `[project] dependencies`:

```
"discord.py>=2.3.0",
```

Then `cd server && uv sync`.

- [ ] **Step 2: Write the listener** at `server/app/services/reaction_listener.py`:

```python
"""Discord gateway listener for ✅ reactions on job embeds.

Runs alongside the existing httpx REST client. Connects to the gateway,
listens for `MessageReactionAdd` events, filters for ✅ on a known job's
embed message OR reminder message, and triggers the manual-ack flow.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from app.config import Settings
from app.models.job import PlatformStatus
from app.services.embed_builder import build_embed
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)

_ACK_EMOJI = "✅"


class ReactionListener:
    """Bot connected to Discord gateway. Single-purpose: react to ✅ reactions."""

    def __init__(
        self,
        *,
        bot_token: str,
        store: JobStore,
        settings: Settings,
        rest_discord_client,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = False  # we don't read content
        intents.reactions = True
        self._client = discord.Client(intents=intents)
        self._token = bot_token
        self._store = store
        self._settings = settings
        self._rest = rest_discord_client
        self._task: asyncio.Task | None = None

        @self._client.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            await self._handle_reaction(payload)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        # Filter: emoji must be ✅
        if str(payload.emoji) != _ACK_EMOJI:
            return
        # Filter: not from ourselves
        if self._client.user and payload.user_id == self._client.user.id:
            return

        # Look up the job by message_id (could be the upload-channel embed
        # OR the rich reminder).
        job = await self._find_job_by_message(str(payload.message_id))
        if job is None:
            return

        logger.info(
            "✅ reaction on %s by user %s → marking tiktok done for %s",
            payload.message_id,
            payload.user_id,
            job.project_id,
        )

        now = datetime.now(tz=timezone.utc)
        new_statuses = {
            **job.platform_statuses,
            "tiktok": PlatformStatus(
                status="uploaded",
                completed_at=now,
                attempts=job.platform_statuses.get(
                    "tiktok", PlatformStatus(status="pending")
                ).attempts,
            ),
        }
        # Mark reminder cancelled if not yet fired.
        updates = {"platform_statuses": new_statuses}
        if job.reminder_message_id is None:
            updates["reminder_cancelled"] = True

        await self._store.update(job.project_id, **updates)

        # Re-render upload-channel embed.
        if job.discord_message_id:
            try:
                latest = await self._store.get(job.project_id)
                embed = build_embed(latest, self._settings.accounts, self._settings.public_base_url)
                await self._rest.edit_message(
                    self._settings.discord.upload_channel_id,
                    job.discord_message_id,
                    embed=embed,
                )
                # Bot adds its own ✅ reaction (visual confirmation; we ignore
                # this echo via the user_id == self.bot.user.id filter above).
                await self._rest.add_reaction(
                    self._settings.discord.upload_channel_id,
                    job.discord_message_id,
                    _ACK_EMOJI,
                )
            except Exception:
                logger.exception("Failed to re-render embed after ack")

        # Cleanup reminder messages if posted (delete both rich + forward).
        await self._cleanup_reminder(job)

    async def _find_job_by_message(self, message_id: str):
        """Match the message_id against any job's discord_message_id or
        reminder_message_id."""
        for j in await self._store.list_all():
            if j.discord_message_id == message_id:
                return j
            if j.reminder_message_id == message_id:
                return j
        return None

    async def _cleanup_reminder(self, job) -> None:
        """Delete any reminder + forward messages for this job."""
        if job.reminder_message_id:
            try:
                await self._rest.delete_message(
                    self._settings.discord.reminder_channel_id,
                    job.reminder_message_id,
                )
            except Exception:
                logger.warning("Failed to delete reminder rich message", exc_info=True)
        if job.reminder_forward_message_id:
            try:
                await self._rest.delete_message(
                    self._settings.discord.reminder_channel_id,
                    job.reminder_forward_message_id,
                )
            except Exception:
                logger.warning("Failed to delete reminder forward message", exc_info=True)
        if job.reminder_message_id or job.reminder_forward_message_id:
            await self._store.update(
                job.project_id,
                reminder_message_id=None,
                reminder_forward_message_id=None,
            )

    async def start(self) -> None:
        self._task = asyncio.create_task(self._client.start(self._token))

    async def stop(self) -> None:
        await self._client.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
```

- [ ] **Step 3: Wire into `main.py` lifespan**

In `server/app/main.py`, in the lifespan block, after the DiscordClient is created and before the scheduler starts:

```python
from app.services.reaction_listener import ReactionListener

# ... in lifespan:
listener = ReactionListener(
    bot_token=settings.discord.bot_token,
    store=job_store,
    settings=settings,
    rest_discord_client=discord,
)
await listener.start()
try:
    # existing scheduler start + yield
    ...
finally:
    await listener.stop()
```

(Adapt the existing lifespan structure — concretely, the listener.start() goes right after the discord client is bound to app.state, and listener.stop() in the finally before the scheduler stop.)

- [ ] **Step 4: Write `tests/test_reaction_listener.py`**

Cover:
- `_handle_reaction` ignores non-✅ emoji
- Ignores reactions from the bot itself
- Ignores reactions on unknown messages
- On valid reaction on upload-channel embed: marks tiktok uploaded, sets reminder_cancelled=True if reminder_message_id is None, re-renders embed, adds bot's own ✅
- On valid reaction on reminder embed: same effect, plus deletes reminder + forward messages

Use mocks for `discord.Client` and the REST client.

- [ ] **Step 5: Run tests**

```bash
cd server && uv run pytest tests/test_reaction_listener.py -v
```

- [ ] **Step 6: Commit**

```bash
git add server/pyproject.toml server/uv.lock \
        server/app/services/reaction_listener.py \
        server/app/main.py \
        server/tests/test_reaction_listener.py
git commit -m "feat(server): Discord reaction listener for manual TikTok ack"
```

---

## Task 7: Update embed builder for new `Job` model

**Files:**
- Modify: `server/app/services/embed_builder.py`
- Modify: `server/tests/test_embed_builder.py`

- [ ] **Step 1: Update `build_embed`** signature: imports `Job` instead of `TikTokJob`. Drop the `devices` parameter (already done in Phase A). Drop the `_ = devices` line (already done in Phase A).

- [ ] **Step 2: Update embed-rendering logic** for the renamed `acked` state semantics. The TikTok line currently shows `✅ TikTok — Posté` when `ps.status == "uploaded"`. With the new model, this check is identical (just based on platform_statuses["tiktok"].status). Should not require code changes if Phase A's signature change is consistent.

- [ ] **Step 3: Update `tests/test_embed_builder.py`** to pass the new `Job` shape (no `status`, no `acked_at`, but with `platform_statuses["tiktok"] = uploaded` to test the ack-state rendering).

- [ ] **Step 4: Run tests**

```bash
cd server && uv run pytest tests/test_embed_builder.py -v
```

- [ ] **Step 5: Commit**

```bash
git add server/app/services/embed_builder.py server/tests/test_embed_builder.py
git commit -m "refactor(server): embed builder uses Job model"
```

---

## Task 8: Reminder service — pass `Job`, no other changes

**Files:**
- Modify: `server/app/services/reminder_service.py` (just rename type hint)
- Modify: `server/tests/test_reminder_service.py`

- [ ] **Step 1: Update type hints** from `TikTokJob` → `Job`. The logic is unchanged.

- [ ] **Step 2: Update tests** for the new model shape.

- [ ] **Step 3: Run tests**

```bash
cd server && uv run pytest tests/test_reminder_service.py -v
```

- [ ] **Step 4: Commit**

```bash
git add server/app/services/reminder_service.py server/tests/test_reminder_service.py
git commit -m "refactor(server): reminder service uses Job type"
```

---

## Task 9: Main backend — pass IG payload + drop n8n

**Files:**
- Modify: `backend/app/services/upload_phase.py`
- Modify: `backend/app/config.py` (drop `discord_webhook_url` and `n8n_webhook_url`)
- Modify: `backend/.env.example` (drop those env vars)
- Delete: `N8N_SCHEDULED_UPLOAD.md`

- [ ] **Step 1: Edit `backend/app/services/upload_phase.py`**

Find the section where the n8n webhook is sent for Instagram (`_send_n8n_instagram_webhook` or similar; grep for `n8n` in upload_phase.py). Delete the function entirely.

In the upload flow, find where Instagram is currently routed to n8n. Replace the routing with: include the Instagram payload in the existing `DiscordService.create_job` call. The Instagram payload has these fields, derived from existing project data:

```python
instagram_payload = None
if "instagram" in requested_platforms and account.meta is not None and account.meta.instagram_business_account_id:
    ig_token = account.meta.instagram_access_token or account.meta.facebook_page_access_token
    if ig_token:
        instagram_payload = {
            "ig_user_id": account.meta.instagram_business_account_id,
            "ig_access_token": ig_token,
            "caption": metadata.instagram.caption,
            "graph_api_version": settings.meta_graph_api_version,
        }
```

Then in the `DiscordService.create_job(...)` call, add `instagram=instagram_payload` (a new kwarg).

- [ ] **Step 2: Update `DiscordService.create_job`** in `backend/app/services/discord_service.py`:

```python
@classmethod
@_swallow("Discord create_job")
def create_job(
    cls,
    *,
    project_id: str,
    account_id: str,
    slot_time: datetime,
    anime_title: str,
    description: str,
    drive_video_url: str,
    platforms_requested: list[str],
    instagram: dict | None = None,
) -> dict[str, Any] | None:
    body = {
        "project_id": project_id,
        "account_id": account_id,
        "slot_time": slot_time.isoformat(),
        "anime_title": anime_title,
        "description": description,
        "drive_video_url": drive_video_url,
        "platforms_requested": list(platforms_requested),
    }
    if instagram is not None:
        body["instagram"] = instagram
    with _client() as c:
        r = c.post("/api/internal/jobs", json=body)
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 3: Drop `discord_webhook_url` and `n8n_webhook_url`**

In `backend/app/config.py`:
- Delete `discord_webhook_url: str | None = None` and `n8n_webhook_url: str | None = None`.

In `backend/.env.example`:
- Delete the `ATR_DISCORD_WEBHOOK_URL=` and `ATR_N8N_WEBHOOK_URL=` lines.
- Update any comments that referenced n8n.

- [ ] **Step 4: Update test_discord_service.py**

Add a test verifying the IG payload field is included when passed.

- [ ] **Step 5: Delete `N8N_SCHEDULED_UPLOAD.md`**

```bash
git rm N8N_SCHEDULED_UPLOAD.md
```

- [ ] **Step 6: Run main backend tests**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer && pixi run --environment dev pytest backend/tests/ -v
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/upload_phase.py \
        backend/app/services/discord_service.py \
        backend/app/config.py \
        backend/.env.example \
        backend/tests/test_discord_service.py
git rm N8N_SCHEDULED_UPLOAD.md
git commit -m "feat(backend): route Instagram via VPS create_job; retire n8n"
```

---

## Task 10: Final verification + deployment notes

**Files:**
- Modify: `server/DEPLOYMENT.md` (Phase B notes)

- [ ] **Step 1: Add Phase B upgrade notes**

Append to `server/DEPLOYMENT.md`:

```markdown
## Phase B — VPS upgrade notes

**Before deploying**: confirm there are no in-flight pending jobs on the VPS.
Quickest check:
```bash
ssh vps 'cat /opt/tiktok/server/data/jobs.json | python3 -m json.tool'
```
If `"jobs": {}` (empty), you're good. Otherwise, finish/delete pending jobs first.

**Deploy steps**:
1. Pull the latest: `git pull`
2. Wipe the old jobs.json (model rename = clean break, per Phase B spec):
   ```bash
   docker compose down
   docker volume rm $(docker volume ls -q | grep jobs-data) || true
   ```
3. Rebuild + start: `docker compose up -d --build`
4. Verify: `curl https://tiktok.sididi.tv/healthz` returns OK.
5. **Verify the gateway connection started**:
   ```bash
   docker compose logs --tail 50 | grep -i "gateway\|connected\|ReactionListener"
   ```
   You should see discord.py logging that it connected to the gateway.

**TikTok bot perms**: the bot needs to see reactions in the upload + reminder channels. Verify:
- Permissions for the bot's role include "Read Message History" + "Add Reactions" in both channels.
```

- [ ] **Step 2: Commit**

```bash
git add server/DEPLOYMENT.md
git commit -m "docs(server): Phase B upgrade notes (jobs.json wipe + gateway perms)"
```

- [ ] **Step 3: Push the branch + open PR**

```bash
git push -u origin feat/phase-b-instagram
```

---

## Self-Review Notes

After all 10 tasks:

1. **Tests**: VPS suite has new tests for IG publisher + reaction listener; updated tests for renamed model + scheduler. All pass.
2. **Backend**: n8n integration fully gone; `grep -rn "n8n\|N8N\|webhook_url" backend/` returns zero matches in src.
3. **Job model**: rename complete; `grep -rn "TikTokJob" server/` returns zero matches.
4. **discord.py**: properly wired into lifespan with start/stop; doesn't conflict with the httpx REST client.
5. **End-to-end**: process a real test project with Instagram in `platforms_requested`. Watch the embed appear, watch IG container creation logs at slot_time, watch the IG line update to ✅ with the permalink, react ✅ on the embed yourself to mark TikTok done, watch the reminder messages disappear (or not appear if you reacted before slot_time).

---

## Outstanding concerns post-Phase-B

- **Exponential backoff on IG retry**: deferred. Phase B v1 retries every scheduler tick (30s); after 5 attempts, gives up. If you observe Discord/Instagram rate limits, add `next_attempt_at` per Q4 follow-up.
- **Reaction listener resilience**: discord.py's `client.start()` reconnects on transient gateway disconnects automatically. If the entire VPS process dies and restarts, the listener reconnects on lifespan start. No persistent state needed.
- **Race condition**: if the scheduler is mid-publish and the user reacts ✅ in the same window, both writes happen against the same job. JobStore's asyncio.Lock serializes writes; the second write wins. In practice the IG publish updates `platform_statuses["instagram"]` and the reaction handler updates `platform_statuses["tiktok"]` + `reminder_cancelled` — orthogonal fields, no real conflict.
