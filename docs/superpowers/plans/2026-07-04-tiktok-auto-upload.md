# TikTok Auto-Upload via Post for Me — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual TikTok flow (Discord reminder + ✅ reaction ack) with automated publishing through Post for Me, triggered by the VPS scheduler at slot time — exactly like Instagram.

**Architecture:** The backend builds a `tiktok_payload` (caption + Post for Me social-account id + post options) and sends it with the job to the VPS server. At `platform_scheduled_at["tiktok"]` the server scheduler downloads the original video from Drive, uploads it to Post for Me (signed-URL flow), creates a post (immediate publish, no `scheduled_at`), polls the post results, and updates the Discord embed. Reminder posting is removed; the reaction listener is commented out but kept.

**Tech Stack:** Python 3.12, FastAPI, httpx, dataclasses, pytest (+pytest-asyncio `asyncio_mode=auto` on the server), Post for Me REST API (`https://api.postforme.dev/v1`, Bearer API key).

**Spec:** `docs/superpowers/specs/2026-07-02-tiktok-auto-upload-design.md`

## Global Constraints

- Post for Me API base URL default: `https://api.postforme.dev/v1`; auth header `Authorization: Bearer <ATR_PFM_API_KEY>`.
- The PFM API key lives **only** in the server `.env` (`ATR_PFM_API_KEY`) — never in `jobs.json`, never in `config/accounts/config.yaml`, never in job payloads.
- TikTok publish retry policy mirrors Instagram: max **5** attempts, transient failure → back to `pending` (retry next tick), terminal failure → `failed` + Discord role ping.
- Double-post guard: once a PFM post id exists in `tiktok_publish_state`, retries must poll that post's results — a new PFM post may only be created when no post id exists or the previous post has a definitive failed result.
- `reaction_listener.py` is **commented out entirely, not deleted** (including its `main.py` wiring and its tests). `reminder_service.py` and its tests **are deleted**.
- Reminder fields on `Job` (`reminder_message_id`, `reminder_forward_message_id`, `reminder_cancelled`) stay readable (old `jobs.json` entries) but are no longer written by new code paths.
- Server tests: `cd server && uv run pytest` (async tests need no marker: `asyncio_mode=auto`). Backend tests: `pixi run test` from repo root (runs pytest with cwd=backend).
- Payload key naming (deliberate simplification vs the spec's example JSON): the TikTok payload field is `social_account_id` (not `pfm_social_account_id`) — it lives inside the `tiktok` payload so the prefix is redundant.
- Commit after every task with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Server job model — `TikTokPublishState` + new `Job` fields + store setter

**Files:**
- Modify: `server/app/models/job.py`
- Modify: `server/app/services/job_store.py`
- Test: `server/tests/test_job_model.py`, `server/tests/test_job_store.py`

**Interfaces:**
- Consumes: existing `Job`, `InstagramPublishState` patterns in `server/app/models/job.py`.
- Produces:
  - `TikTokPublishState` frozen dataclass with fields `post_id: str | None`, `media_url: str | None`, `stage: str | None` (values used later: `"media_uploaded" | "post_created" | "published" | "failed"`), `created_at: datetime | None`, `last_polled_at: datetime | None`, `last_error: str | None`, `url: str | None`; methods `to_dict() -> dict` and `classmethod from_dict(d) -> TikTokPublishState | None`.
  - `Job.tiktok_payload: dict | None = None` and `Job.tiktok_publish_state: TikTokPublishState | None = None`, both round-tripped by `Job.to_dict()`/`from_dict()`.
  - `JobStore.set_tiktok_publish_state(project_id: str, state: TikTokPublishState | None) -> Job`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_job_model.py`:

```python
from app.models.job import TikTokPublishState


def _job_dict_minimal() -> dict:
    return {
        "project_id": "p1",
        "job_id": "j_1",
        "account_id": "anime_fr",
        "device_id": "iphone_16",
        "anime_title": "Title",
        "description": "desc",
        "drive_video_url": "https://drive/x",
        "slot_time": "2026-07-04T12:00:00+00:00",
        "platforms_requested": ["tiktok"],
        "platform_statuses": {},
        "discord_message_id": None,
        "reminder_message_id": None,
        "created_at": "2026-07-04T10:00:00+00:00",
        "updated_at": "2026-07-04T10:00:00+00:00",
    }


def test_tiktok_publish_state_round_trip():
    state = TikTokPublishState(
        post_id="post_123",
        media_url="https://media.postforme.dev/abc.mp4",
        stage="post_created",
        created_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        last_polled_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
        last_error=None,
        url=None,
    )
    restored = TikTokPublishState.from_dict(state.to_dict())
    assert restored == state


def test_tiktok_publish_state_from_dict_none():
    assert TikTokPublishState.from_dict(None) is None


def test_job_round_trips_tiktok_fields():
    d = _job_dict_minimal()
    d["tiktok_payload"] = {"social_account_id": "spc_1", "caption": "hi"}
    d["tiktok_publish_state"] = {"post_id": "post_1", "stage": "published"}
    job = Job.from_dict(d)
    assert job.tiktok_payload == {"social_account_id": "spc_1", "caption": "hi"}
    assert job.tiktok_publish_state.post_id == "post_1"
    out = job.to_dict()
    assert out["tiktok_payload"] == d["tiktok_payload"]
    assert out["tiktok_publish_state"]["post_id"] == "post_1"


def test_job_defaults_tiktok_fields_absent():
    job = Job.from_dict(_job_dict_minimal())
    assert job.tiktok_payload is None
    assert job.tiktok_publish_state is None
```

(Reuse the module's existing imports; add `from datetime import UTC, datetime` and `Job` import only if not already present. If `test_job_model.py` already has a minimal-job-dict helper, reuse it instead of adding `_job_dict_minimal`.)

Append to `server/tests/test_job_store.py`:

```python
from app.models.job import TikTokPublishState


async def test_set_tiktok_publish_state(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job("p1")  # reuse this file's existing job factory helper (adapt name)
    await store.create(job)
    state = TikTokPublishState(post_id="post_9", stage="post_created")
    updated = await store.set_tiktok_publish_state("p1", state)
    assert updated.tiktok_publish_state == state
    reloaded = await store.get("p1")
    assert reloaded.tiktok_publish_state.post_id == "post_9"
```

(Adapt the job factory call to whatever helper `test_job_store.py` already uses to build a `Job`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_job_model.py tests/test_job_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'TikTokPublishState'`.

- [ ] **Step 3: Implement**

In `server/app/models/job.py`, after the `InstagramPublishState` class, add:

```python
@dataclass(frozen=True)
class TikTokPublishState:
    """Resumable Post for Me publish state (no secrets: the API key stays in env).

    Once `post_id` is set, retries poll that post's results instead of creating
    a new post — this is the double-post guard.
    """

    post_id: str | None = None
    media_url: str | None = None
    stage: str | None = None  # media_uploaded | post_created | published | failed
    created_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_error: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "media_url": self.media_url,
            "stage": self.stage,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_polled_at": (
                self.last_polled_at.isoformat() if self.last_polled_at else None
            ),
            "last_error": self.last_error,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TikTokPublishState | None:
        if not isinstance(d, dict):
            return None

        def _dt(key: str) -> datetime | None:
            value = d.get(key)
            if not value:
                return None
            return datetime.fromisoformat(str(value))

        return cls(
            post_id=d.get("post_id"),
            media_url=d.get("media_url"),
            stage=d.get("stage"),
            created_at=_dt("created_at"),
            last_polled_at=_dt("last_polled_at"),
            last_error=d.get("last_error"),
            url=d.get("url"),
        )
```

In the `Job` dataclass, after `instagram_publish_state: InstagramPublishState | None = None` add:

```python
    tiktok_payload: dict | None = None
    tiktok_publish_state: TikTokPublishState | None = None
```

In `Job.to_dict()` add alongside the instagram entries:

```python
            "tiktok_payload": self.tiktok_payload,
            "tiktok_publish_state": (
                self.tiktok_publish_state.to_dict()
                if self.tiktok_publish_state
                else None
            ),
```

In `Job.from_dict()` add:

```python
            tiktok_payload=d.get("tiktok_payload"),
            tiktok_publish_state=TikTokPublishState.from_dict(
                d.get("tiktok_publish_state")
            ),
```

In `server/app/services/job_store.py`: import `TikTokPublishState` and add (mirroring `set_instagram_publish_state`):

```python
    async def set_tiktok_publish_state(
        self, project_id: str, state: TikTokPublishState | None
    ) -> Job:
        """Atomically replace the resumable TikTok publish state."""
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            job.tiktok_publish_state = state
            job.updated_at = datetime.now(tz=UTC)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_job_model.py tests/test_job_store.py -v`
Expected: PASS (all, including pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add server/app/models/job.py server/app/services/job_store.py server/tests/test_job_model.py server/tests/test_job_store.py
git commit -m "feat(server): TikTokPublishState + tiktok job fields + store setter"
```

---

### Task 2: Server settings — PFM key/base URL + optional `device`

**Files:**
- Modify: `server/app/config.py`
- Modify: `server/.env.example`
- Test: `server/tests/test_config.py`

**Interfaces:**
- Consumes: `Settings.load()` in `server/app/config.py`.
- Produces: `Settings.pfm_api_key: str | None` (env `ATR_PFM_API_KEY`, optional), `Settings.pfm_base_url: str` (env `ATR_PFM_BASE_URL`, default `"https://api.postforme.dev/v1"`); `AccountConfig.device` may be `""` when the YAML omits `device`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_config.py` (reuse the file's existing helpers for writing a config YAML + required env vars — every existing test there sets the `ATR_*` env vars via `monkeypatch`; follow the same pattern):

```python
def test_pfm_settings_default_and_env(tmp_server_dir, example_avatar, monkeypatch):
    _set_required_env(monkeypatch)  # reuse/extract this file's env-setup pattern
    config = tmp_server_dir / "config.yaml"
    config.write_text(
        "accounts:\n"
        "  anime_fr:\n"
        "    name: Anime FR\n"
        "    language: fr\n"
        "    device: iphone_16\n"
        "    avatar: anime_fr.jpg\n"
    )
    settings = Settings.load(
        config_path=config, avatars_dir=tmp_server_dir / "avatars"
    )
    assert settings.pfm_api_key is None
    assert settings.pfm_base_url == "https://api.postforme.dev/v1"

    monkeypatch.setenv("ATR_PFM_API_KEY", "pfm_test_key")
    monkeypatch.setenv("ATR_PFM_BASE_URL", "http://localhost:9999/v1")
    settings = Settings.load(
        config_path=config, avatars_dir=tmp_server_dir / "avatars"
    )
    assert settings.pfm_api_key == "pfm_test_key"
    assert settings.pfm_base_url == "http://localhost:9999/v1"


def test_account_device_is_optional(tmp_server_dir, example_avatar, monkeypatch):
    _set_required_env(monkeypatch)
    config = tmp_server_dir / "config.yaml"
    config.write_text(
        "accounts:\n"
        "  anime_fr:\n"
        "    name: Anime FR\n"
        "    language: fr\n"
        "    avatar: anime_fr.jpg\n"
    )
    settings = Settings.load(
        config_path=config, avatars_dir=tmp_server_dir / "avatars"
    )
    assert settings.accounts["anime_fr"].device == ""
```

If `test_config.py` has no shared `_set_required_env` helper, add one setting: `ATR_TIKTOK_SERVER_INTERNAL_TOKEN`, `ATR_PUBLIC_BASE_URL`, `ATR_DISCORD_BOT_TOKEN`, `ATR_DISCORD_GUILD_ID`, `ATR_DISCORD_UPLOAD_CHANNEL_ID`, `ATR_DISCORD_REMINDER_CHANNEL_ID`, `ATR_DISCORD_REMINDER_ROLE_ID` to dummy values, and delete `ATR_PFM_API_KEY`/`ATR_PFM_BASE_URL` (`monkeypatch.delenv(..., raising=False)`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_config.py -v`
Expected: the two new tests FAIL (`AttributeError: pfm_api_key` / `KeyError: 'device'`).

- [ ] **Step 3: Implement**

In `server/app/config.py`:

1. Add fields to `Settings` (after `data_dir: Path`):

```python
    pfm_api_key: str | None = None
    pfm_base_url: str = "https://api.postforme.dev/v1"
```

2. In `Settings.load()`, account parsing: replace `device=str(a["device"]),` with `device=str(a.get("device") or ""),`.

3. In the `return cls(...)` call add:

```python
            pfm_api_key=os.environ.get("ATR_PFM_API_KEY") or None,
            pfm_base_url=os.environ.get(
                "ATR_PFM_BASE_URL", "https://api.postforme.dev/v1"
            ),
```

In `server/.env.example`, after the `ATR_TIKTOK_SERVER_INTERNAL_TOKEN` line add:

```bash
# Post for Me (postforme.dev) — TikTok auto-publish. See docs/POST_FOR_ME_SETUP.md
ATR_PFM_API_KEY=replace_me
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app/config.py server/.env.example server/tests/test_config.py
git commit -m "feat(server): Post for Me settings + optional account device"
```

---

### Task 3: Post for Me publisher service

**Files:**
- Create: `server/app/services/post_for_me_publisher.py`
- Test: `server/tests/test_post_for_me_publisher.py`

**Interfaces:**
- Consumes: `TikTokPublishState` from Task 1.
- Produces:

```python
@dataclass
class TikTokPublishResult:
    success: bool
    url: str | None = None
    detail: str | None = None
    publish_state: TikTokPublishState | None = None

async def publish_to_tiktok(
    *,
    api_key: str,
    social_account_id: str,
    caption: str,
    download_url: str,
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    base_url: str = "https://api.postforme.dev/v1",
    poll_interval: float = 15.0,
    poll_timeout: float = 1800.0,
    publish_state: TikTokPublishState | dict | None = None,
    progress_callback: TikTokProgressCallback | None = None,
    temp_dir: Path | None = None,
) -> TikTokPublishResult
```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_post_for_me_publisher.py`. The tests fake the whole HTTP surface with `respx`-free plain monkeypatching of `httpx.AsyncClient` methods via a lightweight transport: use `httpx.MockTransport` (bundled with httpx, no new dependency).

```python
"""Tests for the Post for Me TikTok publisher (httpx.MockTransport-based)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.models.job import TikTokPublishState
from app.services import post_for_me_publisher as pfm
from app.services.post_for_me_publisher import publish_to_tiktok

BASE = "https://api.postforme.dev/v1"


class FakePfm:
    """Programmable fake of the PFM API + the video download host."""

    def __init__(self) -> None:
        self.video_bytes = b"\x00" * 1024
        self.upload_puts: list[bytes] = []
        self.created_posts: list[dict] = []
        # list of result-payloads returned per successive results poll
        self.results_sequence: list[list[dict]] = []
        self._results_calls = 0
        self.fail_create_post = False
        self.fail_upload = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://drive.example/video.mp4":
            return httpx.Response(200, content=self.video_bytes)
        if url == f"{BASE}/media/create-upload-url":
            return httpx.Response(200, json={
                "upload_url": "https://storage.example/signed-put",
                "media_url": "https://media.example/abc.mp4",
            })
        if url == "https://storage.example/signed-put":
            if self.fail_upload:
                return httpx.Response(500, text="storage error")
            self.upload_puts.append(request.read())
            return httpx.Response(200)
        if url == f"{BASE}/social-posts" and request.method == "POST":
            if self.fail_create_post:
                return httpx.Response(400, json={"error": "bad payload"})
            body = json.loads(request.read())
            self.created_posts.append(body)
            return httpx.Response(200, json={"id": "post_1", "status": "processing"})
        if url.startswith(f"{BASE}/social-post-results"):
            idx = min(self._results_calls, len(self.results_sequence) - 1)
            data = self.results_sequence[idx] if self.results_sequence else []
            self._results_calls += 1
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404, text=f"unexpected: {request.method} {url}")


@pytest.fixture
def fake(monkeypatch) -> FakePfm:
    fake = FakePfm()
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(pfm.httpx, "AsyncClient", client_factory)
    # the binary PUT goes through a sync httpx call in a thread; patch it too
    def fake_put_sync(*, upload_url, video_path, timeout_seconds):
        with httpx.Client(transport=transport) as c:
            with open(video_path, "rb") as f:
                r = c.put(upload_url, content=f.read())
            return r.status_code, r.text

    monkeypatch.setattr(pfm, "_put_file_sync", fake_put_sync)
    return fake


async def _publish(fake, tmp_path, **overrides):
    kwargs = dict(
        api_key="key",
        social_account_id="spc_1",
        caption="my caption",
        download_url="https://drive.example/video.mp4",
        poll_interval=0.0,
        poll_timeout=1.0,
        temp_dir=tmp_path,
    )
    kwargs.update(overrides)
    return await publish_to_tiktok(**kwargs)


async def test_happy_path(fake, tmp_path):
    fake.results_sequence = [
        [],
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"id": "tt1", "url": "https://tiktok.com/@a/video/1"},
          "error": None}],
    ]
    result = await _publish(fake, tmp_path)
    assert result.success is True
    assert result.url == "https://tiktok.com/@a/video/1"
    assert result.publish_state.stage == "published"
    # exactly one post created, with the tiktok configuration
    assert len(fake.created_posts) == 1
    body = fake.created_posts[0]
    assert body["social_accounts"] == ["spc_1"]
    assert body["caption"] == "my caption"
    assert body["media"] == [{"url": "https://media.example/abc.mp4"}]
    assert body["platform_configurations"]["tiktok"]["privacy_status"] == "public"
    # binary was uploaded once
    assert fake.upload_puts == [fake.video_bytes]


async def test_platform_options_forwarded(fake, tmp_path):
    fake.results_sequence = [[{"social_account_id": "spc_1", "success": True,
                               "platform_data": {"url": "u"}, "error": None}]]
    await _publish(
        fake, tmp_path,
        privacy_status="private", allow_comment=False,
        allow_duet=False, allow_stitch=False,
    )
    tiktok = fake.created_posts[0]["platform_configurations"]["tiktok"]
    assert tiktok == {
        "privacy_status": "private",
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
    }


async def test_failed_result_reports_error(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": False,
          "platform_data": {}, "error": {"message": "tiktok rejected"}}],
    ]
    result = await _publish(fake, tmp_path)
    assert result.success is False
    assert "tiktok rejected" in result.detail
    assert result.publish_state.stage == "failed"


async def test_poll_timeout_keeps_resumable_state(fake, tmp_path):
    fake.results_sequence = [[]]  # never a result
    result = await _publish(fake, tmp_path, poll_timeout=0.0)
    assert result.success is False
    assert "timeout" in result.detail
    assert result.publish_state.post_id == "post_1"
    assert result.publish_state.stage == "post_created"


async def test_resume_polls_existing_post_without_new_post(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "https://tiktok.com/v/9"}, "error": None}],
    ]
    state = TikTokPublishState(
        post_id="post_1", media_url="https://media.example/abc.mp4",
        stage="post_created", created_at=datetime.now(tz=UTC),
    )
    result = await _publish(fake, tmp_path, publish_state=state)
    assert result.success is True
    assert fake.created_posts == []      # double-post guard
    assert fake.upload_puts == []        # no re-download / re-upload


async def test_already_published_short_circuits(fake, tmp_path):
    state = TikTokPublishState(post_id="post_1", stage="published", url="https://t/v")
    result = await _publish(fake, tmp_path, publish_state=state)
    assert result.success is True
    assert result.url == "https://t/v"
    assert fake.created_posts == []


async def test_retry_after_failed_reuses_media_and_creates_new_post(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "https://t/v2"}, "error": None}],
    ]
    state = TikTokPublishState(
        post_id="post_old", media_url="https://media.example/abc.mp4",
        stage="failed", last_error="tiktok rejected",
    )
    result = await _publish(fake, tmp_path, publish_state=state)
    assert result.success is True
    assert len(fake.created_posts) == 1   # new post created
    assert fake.upload_puts == []         # media reused, no re-upload
    assert fake.created_posts[0]["media"] == [{"url": "https://media.example/abc.mp4"}]


async def test_create_post_http_error_is_failure(fake, tmp_path):
    fake.fail_create_post = True
    result = await _publish(fake, tmp_path)
    assert result.success is False
    assert "create_post" in result.detail


async def test_upload_failure_is_failure(fake, tmp_path):
    fake.fail_upload = True
    result = await _publish(fake, tmp_path)
    assert result.success is False
    assert "upload" in result.detail


async def test_progress_callback_receives_states(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "u"}, "error": None}],
    ]
    seen: list[str] = []

    async def cb(state):
        seen.append(state.stage)

    await _publish(fake, tmp_path, progress_callback=cb)
    assert "media_uploaded" in seen
    assert "post_created" in seen
    assert seen[-1] == "published"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_post_for_me_publisher.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.post_for_me_publisher`.

- [ ] **Step 3: Implement**

Create `server/app/services/post_for_me_publisher.py`:

```python
"""TikTok publisher via Post for Me (postforme.dev).

Flow:
  GET  download_url                      -> save the MP4 locally
  POST {base}/media/create-upload-url    -> {upload_url, media_url}
  PUT  upload_url                        -> binary upload (signed, no auth)
  POST {base}/social-posts               -> immediate publish (no scheduled_at)
  GET  {base}/social-post-results?post_id=... (poll until a result exists)

Managed credentials: Post for Me's audited TikTok app publishes on our behalf.
The only secret is the project API key (server .env, never persisted in jobs).

Double-post guard: TikTokPublishState persists the PFM post id the moment the
post is created. Retries with a live post id poll its results instead of
creating a new post; a new post is only created when the previous one has a
definitive failed result (stage == "failed"), reusing the uploaded media_url.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.models.job import TikTokPublishState

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.postforme.dev/v1"
_DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 30 * 60.0
_UPLOAD_TIMEOUT_SECONDS = 900.0

TikTokProgressCallback = Callable[[TikTokPublishState], Awaitable[None] | None]


@dataclass
class TikTokPublishResult:
    success: bool
    url: str | None = None
    detail: str | None = None
    publish_state: TikTokPublishState | None = None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _stage_detail(stage: str, detail: str) -> str:
    return f"{stage}: {detail}"


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body[:500] if body else f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}: {str(payload)[:500]}"


def _unwrap(payload: Any) -> dict[str, Any]:
    """PFM object endpoints return either the object or {'data': object}."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _coerce_state(
    state: TikTokPublishState | dict[str, Any] | None,
) -> TikTokPublishState | None:
    if isinstance(state, TikTokPublishState):
        return state
    return TikTokPublishState.from_dict(state)


async def _emit_progress(
    callback: TikTokProgressCallback | None, state: TikTokPublishState
) -> None:
    if callback is None:
        return
    result = callback(state)
    if result is not None:
        await result


async def _download_video(
    client: httpx.AsyncClient, url: str, temp_dir: Path | None
) -> Path:
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="tt-pfm-", suffix=".mp4",
        dir=str(temp_dir) if temp_dir is not None else None,
    )
    os.close(fd)
    path = Path(tmp)
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                await response.aread()
            response.raise_for_status()
            with path.open("wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
        if path.stat().st_size <= 0:
            raise RuntimeError("downloaded video is empty")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _put_file_sync(
    *, upload_url: str, video_path: Path, timeout_seconds: float
) -> tuple[int, str]:
    """Blocking binary PUT to the signed URL. Returns (status_code, body)."""
    with video_path.open("rb") as f:
        response = httpx.put(
            upload_url,
            content=f,
            headers={"Content-Type": "video/mp4"},
            timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds),
            follow_redirects=True,
        )
    return response.status_code, response.text


async def _upload_media(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    download_url: str,
    temp_dir: Path | None,
) -> str:
    """Download the video and push it to PFM storage. Returns media_url."""
    video_path = await _download_video(client, download_url, temp_dir)
    try:
        create = await client.post(
            f"{base_url}/media/create-upload-url",
            headers=_headers(api_key),
            json={},
        )
        create.raise_for_status()
        payload = _unwrap(create.json())
        upload_url = str(payload["upload_url"])
        media_url = str(payload["media_url"])
        status_code, body = await asyncio.to_thread(
            _put_file_sync,
            upload_url=upload_url,
            video_path=video_path,
            timeout_seconds=_UPLOAD_TIMEOUT_SECONDS,
        )
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}: {body[:300]}")
        return media_url
    finally:
        video_path.unlink(missing_ok=True)


def _result_error_detail(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail")
        if message:
            return str(message)[:500]
    if error:
        return str(error)[:500]
    return "post failed without error detail"


async def publish_to_tiktok(  # noqa: PLR0912, PLR0915
    *,
    api_key: str,
    social_account_id: str,
    caption: str,
    download_url: str,
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    base_url: str = DEFAULT_BASE_URL,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
    temp_dir: Path | None = None,
) -> TikTokPublishResult:
    base = base_url.rstrip("/")
    state = _coerce_state(publish_state)
    started = time.monotonic()

    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None), follow_redirects=True
    ) as client:
        # ---- Ensure media is uploaded (reuse persisted media_url on retry) ----
        media_url = state.media_url if state else None
        post_id = state.post_id if state and state.stage != "failed" else None

        if post_id is None and media_url is None:
            try:
                media_url = await _upload_media(
                    client,
                    base_url=base,
                    api_key=api_key,
                    download_url=download_url,
                    temp_dir=temp_dir,
                )
            except httpx.HTTPStatusError as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("upload", _response_detail(e.response)),
                    publish_state=state,
                )
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("upload", f"{type(e).__name__}: {e}"),
                    publish_state=state,
                )
            state = TikTokPublishState(
                media_url=media_url,
                stage="media_uploaded",
                created_at=_utc_now(),
            )
            await _emit_progress(progress_callback, state)

        # ---- Ensure the post exists ----
        if post_id is None:
            body = {
                "caption": caption,
                "social_accounts": [social_account_id],
                "media": [{"url": media_url}],
                "platform_configurations": {
                    "tiktok": {
                        "privacy_status": privacy_status,
                        "allow_comment": allow_comment,
                        "allow_duet": allow_duet,
                        "allow_stitch": allow_stitch,
                    }
                },
            }
            try:
                create = await client.post(
                    f"{base}/social-posts", headers=_headers(api_key), json=body
                )
                create.raise_for_status()
                post_id = str(_unwrap(create.json())["id"])
            except httpx.HTTPStatusError as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("create_post", _response_detail(e.response)),
                    publish_state=state,
                )
            except (httpx.HTTPError, KeyError, ValueError) as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("create_post", f"{type(e).__name__}: {e}"),
                    publish_state=state,
                )
            state = replace(
                state or TikTokPublishState(),
                post_id=post_id,
                stage="post_created",
                last_error=None,
            )
            await _emit_progress(progress_callback, state)
            logger.info(
                "PFM post created social_account_id=%s post_id=%s", social_account_id, post_id
            )

        # ---- Poll results ----
        elapsed = 0.0
        while True:
            try:
                results_resp = await client.get(
                    f"{base}/social-post-results",
                    headers=_headers(api_key),
                    params={"post_id": post_id},
                )
                results_resp.raise_for_status()
                payload = results_resp.json()
            except httpx.HTTPError as e:
                detail = (
                    _response_detail(e.response)
                    if isinstance(e, httpx.HTTPStatusError)
                    else f"{type(e).__name__}: {e}"
                )
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("poll_results", detail),
                    publish_state=state,
                )
            results = payload.get("data") if isinstance(payload, dict) else None
            state = replace(state, last_polled_at=_utc_now())
            if isinstance(results, list) and results:
                result = next(
                    (
                        r for r in results
                        if isinstance(r, dict)
                        and r.get("social_account_id") == social_account_id
                    ),
                    results[0],
                )
                if result.get("success"):
                    platform_data = result.get("platform_data") or {}
                    url = platform_data.get("url")
                    state = replace(state, stage="published", url=url)
                    await _emit_progress(progress_callback, state)
                    logger.info(
                        "PFM TikTok publish succeeded post_id=%s url=%s elapsed=%.1fs",
                        post_id, url, time.monotonic() - started,
                    )
                    return TikTokPublishResult(
                        success=True, url=url, publish_state=state
                    )
                detail = _result_error_detail(result)
                state = replace(state, stage="failed", last_error=detail)
                await _emit_progress(progress_callback, state)
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("result", detail),
                    publish_state=state,
                )
            await _emit_progress(progress_callback, state)
            if elapsed >= poll_timeout:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail(
                        "poll_results",
                        f"timeout after {int(poll_timeout)}s; "
                        f"post_id={post_id}; resumable=true",
                    ),
                    publish_state=state,
                )
            await asyncio.sleep(poll_interval)
            elapsed += max(poll_interval, 0.001)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_post_for_me_publisher.py -v`
Expected: PASS (all 10 tests).

- [ ] **Step 5: Commit**

```bash
git add server/app/services/post_for_me_publisher.py server/tests/test_post_for_me_publisher.py
git commit -m "feat(server): Post for Me TikTok publisher with resumable double-post guard"
```

---

### Task 4: Scheduler — replace TikTok reminder dispatch with publish dispatch

**Files:**
- Modify: `server/app/services/reminder_scheduler.py`
- Test: `server/tests/test_reminder_scheduler.py`

**Interfaces:**
- Consumes: `publish_to_tiktok` + `TikTokPublishResult` (Task 3), `JobStore.set_tiktok_publish_state` (Task 1), `Settings.pfm_api_key`/`pfm_base_url` (Task 2).
- Produces: `_dispatch_tiktok_publish(job, store, settings, discord) -> bool`, wired into `dispatch_due_actions` for `platform == "tiktok"`. `_post_failure_ping` gains a `platform_label: str` parameter (call sites: `"Instagram"`, `"TikTok"`).

- [ ] **Step 1: Rewrite the TikTok tests in `server/tests/test_reminder_scheduler.py`**

Delete the reminder-oriented TikTok tests (`test_dispatch_fires_due_jobs_and_marks_them`, `test_dispatch_skips_already_reminded_jobs`, `test_dispatch_retries_on_next_tick_when_post_fails`, and any other test asserting `post_reminder`/`reminder_message_id` behavior — keep `test_dispatch_skips_jobs_not_yet_due` and `test_run_scheduler_loop_stops_on_event`, adapting their job fixtures if they relied on reminders). Add, following the file's existing fixture style for store/settings/discord (mirror how the `test_dispatch_instagram_*` tests build jobs and monkeypatch `publish_to_instagram`):

```python
from app.models.job import TikTokPublishState
from app.services.post_for_me_publisher import TikTokPublishResult


def _tiktok_job(project_id="p1", *, slot_offset_minutes=-1, payload=True, **overrides):
    """Build a due-by-default TikTok job. Adapt to this file's existing job factory."""
    job = _make_job(  # reuse the file's existing helper; align argument names
        project_id=project_id,
        platforms_requested=["tiktok"],
        slot_time=datetime.now(tz=UTC) + timedelta(minutes=slot_offset_minutes),
    )
    if payload:
        job.tiktok_payload = {
            "social_account_id": "spc_1",
            "caption": "cap",
            "privacy_status": "public",
            "allow_comment": True,
            "allow_duet": True,
            "allow_stitch": True,
        }
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


async def test_dispatch_tiktok_happy_path(store, settings, discord, monkeypatch):
    calls = {}

    async def fake_publish(**kwargs):
        calls.update(kwargs)
        return TikTokPublishResult(
            success=True,
            url="https://tiktok.com/@a/video/1",
            publish_state=TikTokPublishState(post_id="post_1", stage="published"),
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    settings = replace(settings, pfm_api_key="key")  # or set on the fixture
    await store.create(_tiktok_job())
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 1
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "uploaded"
    assert job.platform_statuses["tiktok"].url == "https://tiktok.com/@a/video/1"
    assert job.tiktok_publish_state.stage == "published"
    assert calls["social_account_id"] == "spc_1"
    assert calls["caption"] == "cap"
    assert calls["download_url"] == job.drive_video_url


async def test_dispatch_tiktok_missing_payload_skips(store, settings, discord):
    await store.create(_tiktok_job(payload=False))
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 0
    job = await store.get("p1")
    assert job.platform_statuses.get("tiktok", PlatformStatus(status="pending")).status == "pending"


async def test_dispatch_tiktok_missing_api_key_counts_attempt(
    store, settings, discord
):
    settings = replace(settings, pfm_api_key=None)
    await store.create(_tiktok_job())
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    job = await store.get("p1")
    tt = job.platform_statuses["tiktok"]
    assert tt.status == "pending"          # retried next tick
    assert tt.attempts == 1
    assert "ATR_PFM_API_KEY" in tt.detail


async def test_dispatch_tiktok_fails_after_max_attempts_and_pings(
    store, settings, discord, monkeypatch
):
    async def fake_publish(**kwargs):
        return TikTokPublishResult(success=False, detail="result: tiktok rejected")

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    settings = replace(settings, pfm_api_key="key")
    job = _tiktok_job()
    await store.create(job)
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="pending", attempts=4)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "failed"
    assert updated.platform_statuses["tiktok"].attempts == 5
    # a failure ping mentioning TikTok was posted to the alerts channel
    contents = [
        str(kwargs.get("content") or (args[1] if len(args) > 1 else ""))
        for args, kwargs in discord.post_message.call_args_list
    ]
    assert any("TikTok" in c for c in contents)


async def test_dispatch_tiktok_terminal_statuses_are_not_retried(
    store, settings, discord, monkeypatch
):
    called = False

    async def fake_publish(**kwargs):
        nonlocal called
        called = True
        return TikTokPublishResult(success=True)

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    settings = replace(settings, pfm_api_key="key")
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="uploaded")
    )
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 0
    assert called is False


async def test_dispatch_tiktok_passes_publish_state_for_resume(
    store, settings, discord, monkeypatch
):
    seen = {}

    async def fake_publish(**kwargs):
        seen.update(kwargs)
        return TikTokPublishResult(success=True)

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    settings = replace(settings, pfm_api_key="key")
    job = _tiktok_job()
    job.tiktok_publish_state = TikTokPublishState(post_id="post_7", stage="post_created")
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    assert seen["publish_state"].post_id == "post_7"
```

Adapt fixture/helper names (`store`, `settings`, `discord`, `_make_job`, `replace`) to what `test_reminder_scheduler.py` actually uses — the Instagram tests in the same file show the exact pattern (e.g. if settings is a plain dataclass, use `dataclasses.replace`; if the fixture builds it from env, set `pfm_api_key` the same way the instagram tests inject payload config).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_reminder_scheduler.py -v`
Expected: new TikTok tests FAIL (`ImportError`/`AttributeError: publish_to_tiktok`); Instagram tests still PASS.

- [ ] **Step 3: Implement in `server/app/services/reminder_scheduler.py`**

1. Update the module docstring: tiktok line becomes `- tiktok → publish the video via Post for Me (managed TikTok API). Retries like Instagram; after 5 attempts give up + ping.` Remove the reminder sentences.
2. Replace imports: drop `from app.services.reminder_service import post_reminder`; add:

```python
from app.models.job import InstagramPublishState, Job, PlatformStatus, TikTokPublishState
from app.services.post_for_me_publisher import TikTokPublishResult, publish_to_tiktok
```

3. Add constant next to `_IG_MAX_ATTEMPTS`:

```python
_TT_MAX_ATTEMPTS = 5
```

4. In `dispatch_due_actions`, replace the tiktok branch:

```python
            if platform == "tiktok":
                if await _dispatch_tiktok_publish(job, store, settings, discord):
                    actions += 1
```

5. Delete `_dispatch_tiktok_reminder` entirely and add:

```python
async def _dispatch_tiktok_publish(
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    payload = job.tiktok_payload
    if not payload:
        logger.warning(
            "Job %s has 'tiktok' in platforms_requested but no tiktok_payload",
            job.project_id,
        )
        return False
    current = job.platform_statuses.get("tiktok", PlatformStatus(status="pending"))
    if current.status in ("uploaded", "failed", "skipped", "uploading"):
        return False

    next_attempts = current.attempts + 1
    # merge_platform_status is atomic under the store lock (see the Instagram
    # dispatcher for the rationale).
    await store.merge_platform_status(
        job.project_id, "tiktok",
        PlatformStatus(status="uploading", attempts=next_attempts),
    )

    async def persist_tiktok_state(state: TikTokPublishState) -> None:
        await store.set_tiktok_publish_state(job.project_id, state)

    if not settings.pfm_api_key:
        result = TikTokPublishResult(
            success=False, detail="ATR_PFM_API_KEY is not configured"
        )
    else:
        result = await publish_to_tiktok(
            api_key=settings.pfm_api_key,
            base_url=settings.pfm_base_url,
            social_account_id=payload["social_account_id"],
            caption=payload["caption"],
            download_url=job.drive_video_url,
            privacy_status=payload.get("privacy_status", "public"),
            allow_comment=bool(payload.get("allow_comment", True)),
            allow_duet=bool(payload.get("allow_duet", True)),
            allow_stitch=bool(payload.get("allow_stitch", True)),
            publish_state=job.tiktok_publish_state,
            progress_callback=persist_tiktok_state,
            temp_dir=settings.data_dir / "tmp" / "tiktok",
        )
    if result.publish_state is not None:
        await store.set_tiktok_publish_state(job.project_id, result.publish_state)

    now = datetime.now(tz=UTC)
    if result.success:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="uploaded",
                url=result.url,
                attempts=next_attempts,
                completed_at=now,
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        logger.info(
            "TikTok publish succeeded for %s (url=%s)", job.project_id, result.url
        )
        return True

    if next_attempts >= _TT_MAX_ATTEMPTS:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="failed",
                detail=result.detail,
                attempts=next_attempts,
                completed_at=now,
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        await _post_failure_ping(
            job, settings, discord, result.detail or "publish failed",
            platform_label="TikTok",
        )
        logger.warning(
            "TikTok publish failed for %s after %d attempts: %s",
            job.project_id, next_attempts, result.detail,
        )
    else:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="pending",
                detail=result.detail,
                attempts=next_attempts,
            ),
        )
        logger.info(
            "TikTok publish attempt %d/%d failed for %s: %s — will retry next tick",
            next_attempts, _TT_MAX_ATTEMPTS, job.project_id, result.detail,
        )
    return False
```

6. Generalize `_post_failure_ping` (and update the Instagram call site to pass `platform_label="Instagram"`):

```python
async def _post_failure_ping(
    job: Job, settings: Settings, discord, detail: str, *, platform_label: str
) -> None:
    role = settings.discord.reminder_role_id
    msg = (
        f"<@&{role}> {platform_label} publish failed for **{job.anime_title}** "
        f"({job.account_id}): {detail}"
    )
    try:
        await discord.post_message(settings.discord.reminder_channel_id, content=msg)
    except Exception:
        logger.exception("Failed to post %s failure ping", platform_label)
```

Note: the `"uploading"` state is skipped in `_dispatch_tiktok_publish` (unlike Instagram, whose long poll happens inline in the same tick). If the process crashes mid-publish, the status stays `uploading` — the resume path is the persisted `tiktok_publish_state`, so also treat `uploading` **older than one hour** as retryable if you find dispatch never resumes in the crash test; otherwise keep the simple guard. The tests as written cover the simple guard only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_reminder_scheduler.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app/services/reminder_scheduler.py server/tests/test_reminder_scheduler.py
git commit -m "feat(server): scheduler publishes TikTok via Post for Me instead of posting reminders"
```

---

### Task 5: Internal API — accept the `tiktok` payload

**Files:**
- Modify: `server/app/api/internal.py`
- Test: `server/tests/test_internal_api.py`

**Interfaces:**
- Consumes: `Job.tiktok_payload` / `tiktok_publish_state` (Task 1).
- Produces: `CreateJobRequest.tiktok: TikTokPayload | None` where

```python
class TikTokPayload(BaseModel):
    social_account_id: str
    caption: str
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
```

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_internal_api.py`, following the file's existing client/auth fixture pattern (same body shape as the smoke test in `server/README.md`):

```python
def _job_body(project_id="tt-1", **extra):
    body = {
        "project_id": project_id,
        "account_id": "anime_fr",
        "slot_time": "2026-08-01T18:00:00+00:00",
        "anime_title": "Test",
        "description": "desc",
        "drive_video_url": "https://drive/x",
        "platforms_requested": ["tiktok"],
    }
    body.update(extra)
    return body


def test_create_job_stores_tiktok_payload(client, auth_headers, app):
    tiktok = {
        "social_account_id": "spc_1",
        "caption": "cap",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": True,
        "allow_stitch": False,
    }
    r = client.post(
        "/api/internal/jobs", json=_job_body(tiktok=tiktok), headers=auth_headers
    )
    assert r.status_code == 200
    job = _get_job(app, "tt-1")  # reuse the file's helper for reading the store
    assert job.tiktok_payload == tiktok


def test_update_job_replaces_tiktok_payload_and_resets_state(
    client, auth_headers, app
):
    r = client.post(
        "/api/internal/jobs",
        json=_job_body(tiktok={"social_account_id": "spc_1", "caption": "a"}),
        headers=auth_headers,
    )
    assert r.status_code == 200
    # same job, changed caption → payload updated, publish state reset
    r = client.post(
        "/api/internal/jobs",
        json=_job_body(tiktok={"social_account_id": "spc_1", "caption": "b"}),
        headers=auth_headers,
    )
    assert r.status_code == 200
    job = _get_job(app, "tt-1")
    assert job.tiktok_payload["caption"] == "b"
    assert job.tiktok_publish_state is None


def test_create_job_without_tiktok_payload_is_allowed(client, auth_headers, app):
    r = client.post("/api/internal/jobs", json=_job_body(), headers=auth_headers)
    assert r.status_code == 200
    job = _get_job(app, "tt-1")
    assert job.tiktok_payload is None
```

(Adapt `client`, `auth_headers`, `app`, and the store-reading helper to the file's actual fixtures. If the store is read via `asyncio.run(app.state.job_store.get(...))` in existing tests, do the same.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_internal_api.py -v`
Expected: new tests FAIL (`tiktok_payload` is None / unexpected field ignored).

- [ ] **Step 3: Implement in `server/app/api/internal.py`**

1. Add after `InstagramPayload`:

```python
class TikTokPayload(BaseModel):
    social_account_id: str
    caption: str
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
```

2. Add to `CreateJobRequest`:

```python
    tiktok: TikTokPayload | None = None
```

3. Add next to `_instagram_payload`:

```python
def _tiktok_payload(req: CreateJobRequest) -> dict | None:
    return req.tiktok.model_dump() if req.tiktok else None
```

4. In `_job_payload_changed`, add parameter `tiktok_payload: dict | None` and the clause `or job.tiktok_payload != tiktok_payload`.

5. In `create_job`:
   - compute `tiktok_payload = _tiktok_payload(req)` next to `instagram_payload`;
   - pass it to `_job_payload_changed(existing, req, instagram_payload, tiktok_payload)`;
   - in the `store.update(...)` call add `tiktok_payload=tiktok_payload, tiktok_publish_state=None,`;
   - in the new `Job(...)` constructor add `tiktok_payload=tiktok_payload,`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_internal_api.py tests/test_internal_jobs_slot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app/api/internal.py server/tests/test_internal_api.py
git commit -m "feat(server): internal jobs API accepts tiktok payload"
```

---

### Task 6: Remove reminders, comment out reaction listener, generic TikTok embed line

**Files:**
- Delete: `server/app/services/reminder_service.py`, `server/tests/test_reminder_service.py`
- Modify: `server/app/services/reaction_listener.py` (comment out), `server/tests/test_reaction_listener.py` (comment out), `server/app/main.py`, `server/app/services/embed_builder.py`
- Test: `server/tests/test_embed_builder.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_format_platform_line` treats tiktok like every platform (⏳/✅ + URL); embed omits the Device field and footer segment when `job.device_id` is empty.

- [ ] **Step 1: Write the failing embed tests**

In `server/tests/test_embed_builder.py`, update any test asserting the old TikTok special lines (`🎯 … Pending handoff`, `✅ TikTok — Posté`) and add:

```python
def test_tiktok_line_is_generic_with_url():
    ps = PlatformStatus(status="uploaded", url="https://tiktok.com/@a/video/1")
    line = _format_platform_line("tiktok", ps)
    assert line == "✅ TikTok — https://tiktok.com/@a/video/1"


def test_tiktok_line_pending_is_generic():
    line = _format_platform_line("tiktok", PlatformStatus(status="pending"))
    assert line == "⏳ TikTok — Pending"


def test_embed_omits_device_when_empty(example_account_and_job):
    account, job = example_account_and_job  # adapt to the file's fixtures
    job.device_id = ""
    embed = build_embed(job, {job.account_id: account}, "https://base")
    names = [f["name"] for f in embed["fields"]]
    assert "📱 Device" not in names
    assert " ·  · " not in embed["footer"]["text"]
```

(Adapt the fixture to how this file already builds accounts/jobs.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_embed_builder.py -v`
Expected: new tests FAIL on the old special-cased strings.

- [ ] **Step 3: Implement**

1. `server/app/services/embed_builder.py` — replace `_format_platform_line` with the generic version (delete the two tiktok branches):

```python
def _format_platform_line(platform: str, ps: PlatformStatus) -> str:
    label = _PLATFORM_DISPLAY.get(platform, platform.title())
    emoji = _STATUS_EMOJI.get(ps.status, "·")
    if ps.url:
        suffix = f" — {ps.url}"
    elif ps.detail:
        suffix = f" — {ps.status.title()} ({ps.detail})"
    else:
        suffix = f" — {ps.status.title()}"
    return f"{emoji} {label}{suffix}"
```

2. In `build_embed`, make the device field conditional and the footer skip empty parts:

```python
    fields = []
    if job.device_id:
        fields.append({"name": "📱 Device", "value": job.device_id, "inline": True})
    fields.extend([
        {"name": "🆔 Project", "value": job.project_id, "inline": True},
        ...  # keep the existing remaining fields unchanged
    ])
```

```python
    footer_bits = [account.name]
    if job.device_id:
        footer_bits.append(job.device_id)
    footer_bits.append(f"{job.slot_time.strftime('%H:%M')} UTC")
    ...
        "footer": {"text": " · ".join(footer_bits)},
```

3. Delete `server/app/services/reminder_service.py` and `server/tests/test_reminder_service.py`:

```bash
git rm server/app/services/reminder_service.py server/tests/test_reminder_service.py
```

Then verify nothing still imports it: `grep -rn "reminder_service" server/ --include="*.py"` → expected: no matches (Task 4 already removed the scheduler import; if `server/scripts/send_test_embed.py` matches, update it to stop importing reminder helpers).

4. Comment out `server/app/services/reaction_listener.py`: keep the module docstring, then wrap the entire remaining module body in a block comment, prefixed with:

```python
# ============================================================================
# DISABLED 2026-07: TikTok posting is now automated via Post for Me
# (see docs/superpowers/specs/2026-07-02-tiktok-auto-upload-design.md).
# The ✅-reaction manual-ack flow is kept below, commented out, in case a
# manual fallback is ever needed again.
# ============================================================================
```

Comment every line (`# `-prefix). Do the same to the body of `server/tests/test_reaction_listener.py`.

5. `server/app/main.py`: comment out `from app.services.reaction_listener import ReactionListener` and the `listener = ReactionListener(...)` / `await listener.start()` / `await listener.stop()` lines (keep the `try/finally` structure valid — the `finally` block keeps `await _stop_scheduler(...)` and `app.state.discord = None`).

- [ ] **Step 4: Run the full server suite**

Run: `cd server && uv run pytest -v`
Expected: PASS (reaction-listener tests now collected as empty/skipped, reminder tests gone).

- [ ] **Step 5: Commit**

```bash
git add -A server/
git commit -m "feat(server): remove reminder system, comment out reaction listener, generic tiktok embed line"
```

---

### Task 7: Backend account config — PFM fields, optional device, TikTok pooling

**Files:**
- Modify: `backend/app/services/account_service.py`
- Test: `backend/tests/test_account_service.py`

**Interfaces:**
- Consumes: existing `AccountTikTokConfig`, `_parse_account`, `pool_key_for`.
- Produces:

```python
@dataclass
class AccountTikTokConfig:
    slots: list[str] | None = None
    post_for_me_account_id: str | None = None
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
```

`AccountConfig.device` may be `""`; `pool_key_for("tiktok")` returns `f"tiktok:{post_for_me_account_id}"` when configured, else `None`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_account_service.py`: replace `test_device_field_required` (device is now optional) and add TikTok tests. The file already shows the YAML-on-tmp-path + monkeypatch pattern — reuse it:

```python
def test_device_field_optional(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "accounts:\n"
        "  anime_fr:\n"
        "    name: Anime FR\n"
        "    language: fr\n"
    )
    monkeypatch.setattr(settings, "accounts_config_path", config)
    AccountService.invalidate()
    accounts = AccountService.list_accounts()
    assert accounts[0].device == ""


def test_tiktok_config_parsed(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "accounts:\n"
        "  anime_fr:\n"
        "    name: Anime FR\n"
        "    language: fr\n"
        "    device: iphone_16\n"
        "    tiktok:\n"
        "      slots:\n"
        "        - \"20:00\"\n"
        "      post_for_me_account_id: spc_123\n"
        "      privacy_status: private\n"
        "      allow_comment: false\n"
        "      allow_duet: false\n"
        "      allow_stitch: false\n"
    )
    monkeypatch.setattr(settings, "accounts_config_path", config)
    AccountService.invalidate()
    account = AccountService.get_account("anime_fr")
    assert account.tiktok.post_for_me_account_id == "spc_123"
    assert account.tiktok.privacy_status == "private"
    assert account.tiktok.allow_comment is False
    assert account.tiktok.allow_duet is False
    assert account.tiktok.allow_stitch is False
    assert account.slots_for("tiktok") == ["20:00"]


def test_tiktok_config_defaults(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "accounts:\n"
        "  anime_fr:\n"
        "    name: Anime FR\n"
        "    language: fr\n"
        "    device: iphone_16\n"
        "    tiktok:\n"
        "      post_for_me_account_id: spc_123\n"
    )
    monkeypatch.setattr(settings, "accounts_config_path", config)
    AccountService.invalidate()
    account = AccountService.get_account("anime_fr")
    assert account.tiktok.privacy_status == "public"
    assert account.tiktok.allow_comment is True


def test_tiktok_pool_key(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "accounts:\n"
        "  a1:\n"
        "    name: A1\n"
        "    language: fr\n"
        "    tiktok:\n"
        "      post_for_me_account_id: spc_123\n"
        "  a2:\n"
        "    name: A2\n"
        "    language: fr\n"
    )
    monkeypatch.setattr(settings, "accounts_config_path", config)
    AccountService.invalidate()
    assert AccountService.get_account("a1").pool_key_for("tiktok") == "tiktok:spc_123"
    assert AccountService.get_account("a2").pool_key_for("tiktok") is None
```

(Match the exact monkeypatch style of the existing tests in this file — if they patch `AccountService._config_path` instead of `settings.accounts_config_path`, do the same.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test -- tests/test_account_service.py -v` (or `cd backend && pytest tests/test_account_service.py -v` inside the project venv)
Expected: FAIL — device ValueError, unknown `post_for_me_account_id` attribute.

- [ ] **Step 3: Implement in `backend/app/services/account_service.py`**

1. Extend the dataclass:

```python
@dataclass
class AccountTikTokConfig:
    slots: list[str] | None = None
    post_for_me_account_id: str | None = None
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
```

2. In `_parse_account`, replace the tiktok parsing:

```python
        tiktok_raw = raw.get("tiktok")
        tiktok = None
        if isinstance(tiktok_raw, dict):
            tiktok = AccountTikTokConfig(
                slots=_normalize_slots(tiktok_raw.get("slots")),
                post_for_me_account_id=(
                    str(tiktok_raw["post_for_me_account_id"])
                    if tiktok_raw.get("post_for_me_account_id")
                    else None
                ),
                privacy_status=str(tiktok_raw.get("privacy_status") or "public"),
                allow_comment=bool(tiktok_raw.get("allow_comment", True)),
                allow_duet=bool(tiktok_raw.get("allow_duet", True)),
                allow_stitch=bool(tiktok_raw.get("allow_stitch", True)),
            )
```

3. Replace the device-required block with:

```python
        device = str(raw.get("device") or "")
```

(and delete the `raise ValueError(... 'device' ...)` block). Update the `AccountConfig` dataclass field to `device: str = ""` — move it after `language` is fine since `id/name/language` stay non-default.

4. In `pool_key_for`, replace the tiktok branch:

```python
        if platform == "tiktok":
            pfm_id = self.tiktok.post_for_me_account_id if self.tiktok else None
            return f"tiktok:{pfm_id}" if pfm_id else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test -- tests/test_account_service.py tests/test_scheduling_service.py -v`
Expected: PASS (scheduling tests confirm pooling change didn't break reservations).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/account_service.py backend/tests/test_account_service.py
git commit -m "feat(backend): tiktok account config for Post for Me + optional device + tiktok pooling"
```

---

### Task 8: Backend upload phase — build & send the TikTok payload

**Files:**
- Modify: `backend/app/services/upload_phase.py`, `backend/app/services/discord_service.py`
- Test: `backend/tests/test_upload_phase_tiktok.py` (new), `backend/tests/test_discord_service.py`

**Interfaces:**
- Consumes: `AccountTikTokConfig` (Task 7), server `TikTokPayload` shape (Task 5).
- Produces: `UploadPhaseService._build_tiktok_payload(account: AccountConfig | None, tiktok_description: str) -> dict | None`; `DiscordService.create_job(..., tiktok: dict | None = None)` forwards `body["tiktok"]`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_upload_phase_tiktok.py`:

```python
"""TikTok payload building for the VPS job."""
from __future__ import annotations

from app.services.account_service import (
    AccountConfig,
    AccountTikTokConfig,
)
from app.services.upload_phase import UploadPhaseService


def _account(tiktok: AccountTikTokConfig | None) -> AccountConfig:
    return AccountConfig(
        id="anime_fr", name="Anime FR", language="fr", device="", tiktok=tiktok
    )


def test_build_tiktok_payload_full():
    account = _account(AccountTikTokConfig(
        post_for_me_account_id="spc_123",
        privacy_status="public",
        allow_comment=True,
        allow_duet=False,
        allow_stitch=True,
    ))
    payload = UploadPhaseService._build_tiktok_payload(account, "my description")
    assert payload == {
        "social_account_id": "spc_123",
        "caption": "my description",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": False,
        "allow_stitch": True,
    }


def test_build_tiktok_payload_none_without_pfm_id():
    assert UploadPhaseService._build_tiktok_payload(
        _account(AccountTikTokConfig()), "d"
    ) is None
    assert UploadPhaseService._build_tiktok_payload(_account(None), "d") is None
    assert UploadPhaseService._build_tiktok_payload(None, "d") is None


def test_upfront_skip_tiktok_without_pfm_id():
    skips = UploadPhaseService._compute_upfront_skips(
        ("tiktok",), _account(AccountTikTokConfig())
    )
    assert skips["tiktok"].status == "skipped"
    assert "Post for Me" in skips["tiktok"].detail


def test_no_upfront_skip_with_pfm_id():
    skips = UploadPhaseService._compute_upfront_skips(
        ("tiktok",), _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    )
    assert "tiktok" not in skips
```

In `backend/tests/test_discord_service.py`, add (following the file's existing mocked-client pattern for `create_job`):

```python
def test_create_job_forwards_tiktok_payload(...):
    # reuse this file's existing create_job test setup; call with
    # tiktok={"social_account_id": "spc_1", "caption": "c"} and assert the
    # posted JSON body contains body["tiktok"] == {"social_account_id": "spc_1", "caption": "c"}
```

(Write it concretely against the file's actual fixture — the existing `create_job` test shows how the httpx client is mocked and the body captured.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test -- tests/test_upload_phase_tiktok.py tests/test_discord_service.py -v`
Expected: FAIL — `_build_tiktok_payload` missing; tiktok kwarg unexpected.

- [ ] **Step 3: Implement**

1. `backend/app/services/discord_service.py` — `create_job`: add parameter `tiktok: dict | None = None` (after `instagram`) and:

```python
        if tiktok is not None:
            body["tiktok"] = tiktok
```

2. `backend/app/services/upload_phase.py`:

a. Add the payload builder as a classmethod near `_compute_upfront_skips`:

```python
    @classmethod
    def _build_tiktok_payload(
        cls, account: AccountConfig | None, tiktok_description: str
    ) -> dict[str, Any] | None:
        """Payload for the VPS server's Post for Me publish (see server TikTokPayload)."""
        if account is None or account.tiktok is None:
            return None
        tiktok = account.tiktok
        if not tiktok.post_for_me_account_id:
            return None
        return {
            "social_account_id": tiktok.post_for_me_account_id,
            "caption": tiktok_description,
            "privacy_status": tiktok.privacy_status,
            "allow_comment": tiktok.allow_comment,
            "allow_duet": tiktok.allow_duet,
            "allow_stitch": tiktok.allow_stitch,
        }
```

b. In `_compute_upfront_skips`, add after the instagram branch:

```python
            elif platform == "tiktok":
                if account is not None and (
                    account.tiktok is None
                    or not account.tiktok.post_for_me_account_id
                ):
                    reason = "No Post for Me account configured for this account"
```

c. In `execute_upload`, next to the `ig_payload_base` construction, add:

```python
        tiktok_payload = cls._build_tiktok_payload(
            account, metadata.tiktok.description
        )
```

and pass it in the `DiscordService.create_job(...)` call: `tiktok=tiktok_payload,` (after `instagram=ig_payload,`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test -- tests/test_upload_phase_tiktok.py tests/test_discord_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/upload_phase.py backend/app/services/discord_service.py backend/tests/test_upload_phase_tiktok.py backend/tests/test_discord_service.py
git commit -m "feat(backend): build and send tiktok payload to VPS job"
```

---

### Task 9: Config examples + docs

**Files:**
- Modify: `config/accounts/config.example.yaml`, `server/config/config.example.yaml`, `server/README.md`, `server/DEPLOYMENT.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `config/accounts/config.example.yaml`**

- In the pooling comment block, replace `#   - TikTok    never pooled (manual post; slot is a Discord reminder)` with `#   - TikTok    pooled by tiktok.post_for_me_account_id`.
- Note `device` as optional: change its comment to `# optional: label shown in the Discord embed (legacy manual-post field)`.
- Add a documented tiktok block to the `anime_fr` example:

```yaml
    tiktok:
      # Post for Me social-account id (spc_...) — enables automatic TikTok
      # publishing via the VPS server. See docs/POST_FOR_ME_SETUP.md.
      post_for_me_account_id: "spc_..."
      # Optional post options (defaults shown):
      # privacy_status: "public"
      # allow_comment: true
      # allow_duet: true
      # allow_stitch: true
```

- [ ] **Step 2: Update `server/config/config.example.yaml`**

Mark `device` optional in the header comment: `# `device` is an optional free-form label shown in the Discord embed's "📱 Device" field (legacy manual-post metadata).`

- [ ] **Step 3: Update `server/README.md`**

- Smoke-test step 4: drop the "(no reminder yet)" phrasing; note that at `slot_time` the scheduler **publishes TikTok via Post for Me** (needs `ATR_PFM_API_KEY`; without it the TikTok status shows the retry detail).
- Replace smoke-test step 6 (reminder + forward) with: "Wait for slot_time → the TikTok line flips to ✅ with the published URL (or shows the failure detail after retries; a role ping is sent on terminal failure)."
- Mention `docs/POST_FOR_ME_SETUP.md` in an intro sentence.

- [ ] **Step 4: Update `server/DEPLOYMENT.md`**

Add `ATR_PFM_API_KEY` to the environment-variable list/section (grep for `ATR_DISCORD_BOT_TOKEN` to find it), with one line: `# Post for Me API key — TikTok auto-publish (docs/POST_FOR_ME_SETUP.md)`.

- [ ] **Step 5: Commit**

```bash
git add config/accounts/config.example.yaml server/config/config.example.yaml server/README.md server/DEPLOYMENT.md
git commit -m "docs: tiktok auto-publish config examples + deployment notes"
```

---

### Task 10: Full verification

**Files:** none new.

- [ ] **Step 1: Run the full server suite**

Run: `cd server && uv run pytest`
Expected: PASS, no reminder_service references, reaction-listener tests inert.

- [ ] **Step 2: Run the full backend suite**

Run: `pixi run test`
Expected: PASS.

- [ ] **Step 3: Grep for leftovers**

```bash
grep -rn "post_reminder\|reminder_service" server/ backend/ --include="*.py"
```
Expected: no matches outside commented-out blocks.

```bash
grep -rn "Pending handoff" server/ frontend/src backend/ 2>/dev/null
```
Expected: no matches (if the frontend renders this string, update `frontend/src/types/index.ts`/components accordingly — check `grep -rn "tiktok" frontend/src/types/index.ts`).

- [ ] **Step 4: Commit any stragglers**

```bash
git status --short   # expect clean; commit fixups if not
```
