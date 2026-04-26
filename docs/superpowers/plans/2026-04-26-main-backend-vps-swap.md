# Main Backend VPS Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the existing main backend so all Discord output goes through the VPS server (Plan A), delete the lazy "strikethrough on overdue" cross system, add `device:` to account config, and remove the now-unused `discord_upload_message_crossed` field. Bundles Phase 4 (final verification sweep) at the end.

**Architecture:** Surgical edits to `backend/`. `DiscordService` is rewritten as a thin HTTP client to `tiktok.sididi.tv/api/internal/*`. The upload-phase code keeps its overall shape but stops formatting Discord text — instead it calls `create_job` once at slot time + `update_job_platform` per platform completion + `delete_job` on cascade. The cross system code is fully deleted (~80 lines). No schema changes to projects on disk; the removed `discord_upload_message_crossed` field is silently ignored on read by Pydantic's default `extra="ignore"`.

**Tech Stack:** Same as the existing main backend (FastAPI, pydantic v2, `requests` for HTTP). Tests use `pytest` + `respx` for HTTP mocks. Note: `requests` is sync; we keep that to match the rest of `backend/`'s codebase conventions.

**Reference spec:** [docs/superpowers/specs/2026-04-26-mobile-tiktok-app-design.md](../specs/2026-04-26-mobile-tiktok-app-design.md). Sections 5 (Configuration) and 10 (Main Backend Changes) are most relevant.

**Prerequisites:** Plan A (VPS server) is **deployed and live** at `https://tiktok.sididi.tv`, with all `/api/internal/*` endpoints verified via the smoke test. The internal token is known.

---

## File Structure (changes only)

```
backend/
├── app/
│   ├── config.py                     # MOD: drop webhook URL, add VPS server URL/token
│   ├── models/
│   │   └── project.py                # MOD: remove discord_upload_message_crossed
│   ├── services/
│   │   ├── account_service.py        # MOD: AccountConfig.device required
│   │   ├── discord_service.py        # REWRITE: HTTP client to VPS
│   │   ├── upload_phase.py           # MOD: surgical edits (delete cross, swap calls)
│   │   └── integration_health_service.py  # MOD: ping VPS /healthz
│   └── api/routes/
│       └── processing.py             # NO CHANGE (uses DiscordService generically)
├── tests/
│   ├── test_discord_service.py       # NEW: respx-mocked HTTP tests
│   └── test_account_service.py       # NEW: device field validation
└── ...
config/
└── accounts/
    ├── config.example.yaml           # MOD: add device: line
    └── avatars/                      # MOVE: git mv to ../../server/avatars/
.env.example                          # MOD: drop webhook, add VPS env
```

---

## Task 1: Backend settings — add VPS server config, drop webhook

**Files:**
- Modify: `backend/app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Edit `backend/app/config.py` — replace the Discord webhook block**

Find:
```python
    # Discord webhook integration
    discord_webhook_url: str | None = None
    cep_trigger_url_template: str = "http://localhost:48653/p/{project_id}"
```

Replace with:
```python
    # TikTok server (VPS) integration — replaces previous Discord webhook
    tiktok_server_base_url: str | None = None
    tiktok_server_internal_token: str | None = None
    cep_trigger_url_template: str = "http://localhost:48653/p/{project_id}"
```

- [ ] **Step 2: Edit `.env.example` — remove webhook line, add VPS lines**

Find:
```
ATR_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
```

Replace with:
```
ATR_TIKTOK_SERVER_BASE_URL=https://tiktok.sididi.tv
ATR_TIKTOK_SERVER_INTERNAL_TOKEN=replace_me_match_vps
```

- [ ] **Step 3: Confirm settings still load**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run python -c "from backend.app.config import settings; print(settings.tiktok_server_base_url)"`
Expected: prints `None` (or whatever's in `.env`).

- [ ] **Step 4: Commit**

```bash
git add backend/app/config.py .env.example
git commit -m "feat(backend): swap Discord webhook setting for TikTok-server URL+token"
```

---

## Task 2: Rewrite `DiscordService` as a VPS HTTP client

**Files:**
- Modify: `backend/app/services/discord_service.py`
- Create: `backend/tests/test_discord_service.py`

The new `DiscordService` keeps the **same public method names** used by callers elsewhere (`is_configured`, `post_message`, `edit_message`, `delete_message`) so non-job-related call sites need no changes. It also gains three new job-oriented methods (`create_job`, `update_job_platform`, `delete_job`).

- [ ] **Step 1: Add `respx` to dev deps**

Add to `pixi.toml` under the appropriate section (or backend's `pyproject.toml` — match existing test dep patterns):

Run: `pixi add --pypi --feature dev respx` (if pixi project uses dev features) — OR ensure `respx>=0.21.0` is added wherever pytest deps live.

If unsure of the convention, check what `pytest` itself is added under in [pixi.toml](../../../pixi.toml) and follow the same pattern.

- [ ] **Step 2: Write the failing tests (`backend/tests/test_discord_service.py`)**

```python
"""Tests for the rewritten DiscordService (HTTP client to the VPS server)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from backend.app.services.discord_service import DiscordMessage, DiscordService


@pytest.fixture(autouse=True)
def _set_vps_env(monkeypatch):
    monkeypatch.setattr(
        "backend.app.services.discord_service.settings.tiktok_server_base_url",
        "https://tiktok.sididi.tv",
    )
    monkeypatch.setattr(
        "backend.app.services.discord_service.settings.tiktok_server_internal_token",
        "internal_secret",
    )


def test_is_configured_true_when_both_set():
    assert DiscordService.is_configured() is True


def test_is_configured_false_when_url_missing(monkeypatch):
    monkeypatch.setattr(
        "backend.app.services.discord_service.settings.tiktok_server_base_url", None
    )
    assert DiscordService.is_configured() is False


@respx.mock
def test_post_message_calls_generic_endpoint():
    route = respx.post("https://tiktok.sididi.tv/api/internal/discord/messages").mock(
        return_value=httpx.Response(200, json={"message_id": "msg_42"})
    )
    msg = DiscordService.post_message("hello")
    assert isinstance(msg, DiscordMessage)
    assert msg.id == "msg_42"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer internal_secret"
    assert b'"content":"hello"' in sent.content


@respx.mock
def test_edit_message_calls_generic_patch():
    route = respx.patch(
        "https://tiktok.sididi.tv/api/internal/discord/messages/m_1"
    ).mock(return_value=httpx.Response(200))
    DiscordService.edit_message("m_1", "new content")
    assert route.called


@respx.mock
def test_delete_message_calls_generic_delete():
    route = respx.delete(
        "https://tiktok.sididi.tv/api/internal/discord/messages/m_1"
    ).mock(return_value=httpx.Response(200))
    DiscordService.delete_message("m_1")
    assert route.called


@respx.mock
def test_create_job_returns_response_dict():
    route = respx.post("https://tiktok.sididi.tv/api/internal/jobs").mock(
        return_value=httpx.Response(
            200, json={"job_id": "j_x", "discord_message_id": "msg_100"}
        )
    )
    res = DiscordService.create_job(
        project_id="p1",
        account_id="anime_fr",
        slot_time=datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc),
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive.google.com/uc?id=xyz",
        platforms_requested=["youtube", "tiktok"],
    )
    assert res == {"job_id": "j_x", "discord_message_id": "msg_100"}
    assert route.called
    sent_body = route.calls.last.request.content
    assert b'"project_id":"p1"' in sent_body
    assert b'"account_id":"anime_fr"' in sent_body


@respx.mock
def test_update_job_platform():
    route = respx.post(
        "https://tiktok.sididi.tv/api/internal/jobs/p1/platform-status"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))
    DiscordService.update_job_platform(
        "p1", "youtube", status="uploaded", url="https://youtu.be/x"
    )
    assert route.called


@respx.mock
def test_delete_job():
    route = respx.delete("https://tiktok.sididi.tv/api/internal/jobs/p1").mock(
        return_value=httpx.Response(200, json={"ok": True, "deleted": True})
    )
    DiscordService.delete_job("p1")
    assert route.called


def test_post_message_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        "backend.app.services.discord_service.settings.tiktok_server_base_url", None
    )
    assert DiscordService.post_message("anything") is None


@respx.mock
def test_post_message_swallows_network_errors():
    respx.post("https://tiktok.sididi.tv/api/internal/discord/messages").mock(
        side_effect=httpx.ConnectError("boom")
    )
    # Must not raise
    assert DiscordService.post_message("x") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run pytest backend/tests/test_discord_service.py -v`
Expected: import errors or method-not-found errors.

- [ ] **Step 4: Rewrite `backend/app/services/discord_service.py`**

```python
"""Thin HTTP client to the TikTok VPS server. Replaces direct Discord webhook calls.

All Discord-related operations (posting messages, embed jobs, reactions) are
proxied through the VPS server at `settings.tiktok_server_base_url`. Network
errors are logged and swallowed so the main backend's pipeline never blocks on
Discord-related failures.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class DiscordMessage:
    id: str
    content: str = ""


def _client() -> httpx.Client:
    base = settings.tiktok_server_base_url
    if not base:
        raise RuntimeError("TikTok server base URL not configured")
    token = settings.tiktok_server_internal_token or ""
    return httpx.Client(
        base_url=base.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def _swallow(label: str):
    """Context-manager-like decorator that logs+swallows httpx errors."""

    def wrap(fn):
        def inner(*args, **kwargs):
            if not DiscordService.is_configured():
                return None
            try:
                return fn(*args, **kwargs)
            except httpx.HTTPError as e:
                logger.warning("%s failed: %s", label, e)
                return None

        return inner

    return wrap


class DiscordService:
    """Public API preserved for back-compat; calls go to VPS internally."""

    # ---- Configuration check -------------------------------------------------
    @classmethod
    def is_configured(cls) -> bool:
        return bool(
            settings.tiktok_server_base_url and settings.tiktok_server_internal_token
        )

    # ---- Generic message endpoints (used by processing.py, etc.) -------------
    @classmethod
    @_swallow("Discord post_message")
    def post_message(cls, content: str) -> DiscordMessage | None:
        with _client() as c:
            r = c.post("/api/internal/discord/messages", json={"content": content})
            r.raise_for_status()
            return DiscordMessage(id=str(r.json()["message_id"]), content=content)

    @classmethod
    @_swallow("Discord edit_message")
    def edit_message(cls, message_id: str, content: str) -> DiscordMessage | None:
        if not message_id:
            return None
        with _client() as c:
            r = c.patch(
                f"/api/internal/discord/messages/{message_id}",
                json={"content": content},
            )
            r.raise_for_status()
            return DiscordMessage(id=message_id, content=content)

    @classmethod
    @_swallow("Discord delete_message")
    def delete_message(cls, message_id: str) -> bool:
        if not message_id:
            return False
        with _client() as c:
            r = c.delete(f"/api/internal/discord/messages/{message_id}")
            return r.status_code in (200, 204, 404)

    # ---- Job-oriented endpoints (upload_phase.py) ----------------------------
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
        with _client() as c:
            r = c.post("/api/internal/jobs", json=body)
            r.raise_for_status()
            return r.json()

    @classmethod
    @_swallow("Discord update_job_platform")
    def update_job_platform(
        cls,
        project_id: str,
        platform: str,
        *,
        status: str,
        url: str | None = None,
        detail: str | None = None,
    ) -> None:
        body = {"platform": platform, "status": status, "url": url, "detail": detail}
        with _client() as c:
            r = c.post(f"/api/internal/jobs/{project_id}/platform-status", json=body)
            r.raise_for_status()
            return None

    @classmethod
    @_swallow("Discord delete_job")
    def delete_job(cls, project_id: str) -> None:
        with _client() as c:
            r = c.delete(f"/api/internal/jobs/{project_id}")
            r.raise_for_status()
            return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run pytest backend/tests/test_discord_service.py -v`
Expected: all 9 tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/discord_service.py backend/tests/test_discord_service.py
git add pixi.toml pixi.lock  # if respx was added there
git commit -m "feat(backend): rewrite DiscordService as VPS HTTP client + tests"
```

---

## Task 3: Add `device:` field to AccountConfig

**Files:**
- Modify: `backend/app/services/account_service.py`
- Modify: `config/accounts/config.example.yaml`
- Create: `backend/tests/test_account_service.py`

- [ ] **Step 1: Write the failing test (`backend/tests/test_account_service.py`)**

```python
"""Tests for the account_service.AccountService device field handling."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.services.account_service import AccountService


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "anime_fr.jpg").write_bytes(b"\x89PNG")
    return p


def test_device_field_required(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    avatar: "anime_fr.jpg"
    slots: ["14:00"]
""",
    )
    monkeypatch.setattr(
        "backend.app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    with pytest.raises(ValueError, match="anime_fr"):
        AccountService.list_accounts()


def test_device_field_loaded(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    avatar: "anime_fr.jpg"
    device: "iphone_13_pro"
    slots: ["14:00"]
""",
    )
    monkeypatch.setattr(
        "backend.app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    accounts = AccountService.list_accounts()
    assert accounts[0].id == "anime_fr"
    assert accounts[0].device == "iphone_13_pro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run pytest backend/tests/test_account_service.py -v`
Expected: `AttributeError: ... 'AccountConfig' object has no attribute 'device'` or similar.

- [ ] **Step 3: Modify `AccountConfig` in `backend/app/services/account_service.py`**

Find the `AccountConfig` dataclass (around line 70):
```python
@dataclass
class AccountConfig:
    id: str
    name: str
    language: str
    supported_types: list[LibraryType] = field(default_factory=lambda: [DEFAULT_LIBRARY_TYPE])
    avatar: str | None = None
    slots: list[str] = field(default_factory=list)
    youtube: AccountYouTubeConfig | None = None
    meta: AccountMetaConfig | None = None
    facebook: AccountFacebookConfig | None = None
    instagram: AccountInstagramConfig | None = None
    tiktok: AccountTikTokConfig | None = None
```

Add `device: str` as a required field (positionally before fields with defaults — pydantic dataclasses require this ordering):

```python
@dataclass
class AccountConfig:
    id: str
    name: str
    language: str
    device: str
    supported_types: list[LibraryType] = field(default_factory=lambda: [DEFAULT_LIBRARY_TYPE])
    avatar: str | None = None
    slots: list[str] = field(default_factory=list)
    youtube: AccountYouTubeConfig | None = None
    meta: AccountMetaConfig | None = None
    facebook: AccountFacebookConfig | None = None
    instagram: AccountInstagramConfig | None = None
    tiktok: AccountTikTokConfig | None = None
```

- [ ] **Step 4: Modify `_parse_account` to extract `device:`**

Locate `_parse_account` (around line 137). Inside the function, after extracting `name` / `language` / etc., add a strict device-field extraction. Find the line where `AccountConfig(...)` is constructed at the end of the function and modify:

Old construction (likely something like):
```python
        return AccountConfig(
            id=account_id,
            name=str(raw["name"]),
            language=str(raw.get("language", "en")),
            ...
        )
```

Add a `device` extraction near the top of the parser:
```python
        device = raw.get("device")
        if not device or not isinstance(device, str):
            raise ValueError(
                f"Account {account_id!r}: missing required field 'device'. "
                f"Set device: \"<device_id>\" matching a device in the VPS server's config."
            )
```

And include it in the construction:
```python
        return AccountConfig(
            id=account_id,
            name=str(raw["name"]),
            language=str(raw.get("language", "en")),
            device=device,
            ...
        )
```

(The exact construction call may differ; preserve every other field, only inject `device=device`.)

- [ ] **Step 5: Update `config/accounts/config.example.yaml`**

For each example account in [config/accounts/config.example.yaml](../../../config/accounts/config.example.yaml), add:

```yaml
    avatar: "anime_fr.jpg"
    device: "iphone_13_pro"     # must match a device id in the VPS server's config
    slots:
      - "14:00"
```

(Insert the `device:` line after `avatar:` and before `slots:`.)

- [ ] **Step 6: Run tests**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run pytest backend/tests/test_account_service.py -v`
Expected: 2 passed.

- [ ] **Step 7: Update real `config/accounts/config.yaml`**

The operator must add `device:` lines to every account in the real config before the next pipeline run, otherwise account loading raises. This is a manual edit, not committed (it's gitignored).

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/account_service.py
git add backend/tests/test_account_service.py
git add config/accounts/config.example.yaml
git commit -m "feat(backend): require device: field on AccountConfig"
```

---

## Task 4: Remove `discord_upload_message_crossed` from `Project`

**Files:**
- Modify: `backend/app/models/project.py`

Pydantic v2's default `extra="ignore"` means existing on-disk JSON projects with the field will deserialize cleanly without it. No migration needed.

- [ ] **Step 1: Confirm pydantic ignores unknown fields**

Run:
```bash
cd /home/sid/Projects/anime-tiktok-reproducer && pixi run python -c "
from backend.app.models.project import Project
p = Project.model_validate({
    'id': 'x', 'discord_upload_message_crossed': True, 'extra_garbage': 1
})
print('OK', p.id)
"
```
Expected: `OK x` (no error).

- [ ] **Step 2: Edit `backend/app/models/project.py`**

Find the line at [backend/app/models/project.py:62](../../../backend/app/models/project.py#L62):
```python
    discord_upload_message_crossed: bool = False
```

Delete it. The line above (`upload_last_result`) and below (script-phase settings) remain.

- [ ] **Step 3: Verify imports/serializers don't reference it**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && grep -rn "discord_upload_message_crossed" backend/`
Expected: only references in `upload_phase.py` (cleaned up in Task 6) and possibly `project_service.py` serializers (will be obvious if any).

If any other reference appears outside `upload_phase.py`, fix it now (likely `del project.discord_upload_message_crossed` removed, or attribute access removed). Report findings before committing.

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/project.py
git commit -m "feat(backend): remove discord_upload_message_crossed from Project model"
```

---

## Task 5: Move avatars to `server/avatars/`

**Files:**
- Move: `config/accounts/avatars/` → `server/avatars/`

- [ ] **Step 1: Verify `_avatars_dir` is unused**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && grep -rn "_avatars_dir\b" backend/ frontend/`
Expected: only the definition at [backend/app/services/account_service.py:133](../../../backend/app/services/account_service.py#L133), no callers.

If callers exist, repoint them at the new location instead of moving.

- [ ] **Step 2: If unused, remove the `_avatars_dir` method**

Edit [backend/app/services/account_service.py](../../../backend/app/services/account_service.py) — find:
```python
    @classmethod
    def _avatars_dir(cls) -> Path:
        return cls._config_path().parent / "avatars"
```

Delete those four lines.

- [ ] **Step 3: Move the avatars directory**

Run:
```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git mv config/accounts/avatars server/avatars
```

If `server/avatars` already exists from Plan A's skeleton (with only `.gitkeep`), instead do:
```bash
cd /home/sid/Projects/anime-tiktok-reproducer
mv config/accounts/avatars/* server/avatars/
git rm -r config/accounts/avatars/
git add server/avatars/
```

Verify no other code references `config/accounts/avatars` (grep).

- [ ] **Step 4: Update `config/accounts/config.example.yaml` comment**

Find the comment line referencing `avatars/ directory` (around line 13 of the example) and update to:
```yaml
    avatar: "anime_fr.jpg"       # filename in server/avatars/ (served by VPS)
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(backend): move avatars to server/avatars (single source for VPS)"
```

---

## Task 6a: Delete the cross system from `upload_phase.py`

**Files:**
- Modify: `backend/app/services/upload_phase.py`

Three deletions: the call site (line 257), the two cross methods (lines 378-431), the format helper (lines 444-518).

- [ ] **Step 1: Delete the call site at line 257**

In [backend/app/services/upload_phase.py:254-257](../../../backend/app/services/upload_phase.py#L254-L257), find:

```python
    @classmethod
    def list_manager_rows(cls) -> list[dict[str, Any]]:
        projects = ProjectService.list_all()
        cls._cross_overdue_upload_messages(projects)
```

Replace with:
```python
    @classmethod
    def list_manager_rows(cls) -> list[dict[str, Any]]:
        projects = ProjectService.list_all()
```

- [ ] **Step 2: Delete the cross helper methods**

Find the block at [upload_phase.py:378-431](../../../backend/app/services/upload_phase.py#L378-L431) starting with:

```python
    @classmethod
    def _cross_out_discord_message(cls, project: Project) -> bool:
```

Delete the entire `_cross_out_discord_message` and `_cross_overdue_upload_messages` methods (the full ~54 lines, ending where `_format_french_datetime` starts).

- [ ] **Step 3: Delete `_format_upload_discord_message`**

Find the block at [upload_phase.py:444-518](../../../backend/app/services/upload_phase.py#L444-L518) starting with:

```python
    @classmethod
    def _format_upload_discord_message(
        cls,
        *,
        project: Project,
        ...
```

Delete the entire method (75 lines).

- [ ] **Step 4: Verify nothing else references these**

Run:
```bash
cd /home/sid/Projects/anime-tiktok-reproducer
grep -rn "_cross_out_discord_message\|_cross_overdue_upload_messages\|_format_upload_discord_message" backend/
```
Expected: zero matches.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/upload_phase.py
git commit -m "refactor(backend): delete strikethrough cross system + Discord text formatter"
```

---

## Task 6b: Replace Discord call sites in `upload_phase.py`

**Files:**
- Modify: `backend/app/services/upload_phase.py`

Three call sites change. Each is replaced surgically. After this task, `upload_phase.py` no longer formats Discord text — the VPS owns that.

- [ ] **Step 1: Replace the `edit_discord_snapshot` closure with `update_job_platform` per platform**

Find the block around lines 666-708 in [upload_phase.py](../../../backend/app/services/upload_phase.py). The structure is:

```python
        def edit_discord_snapshot(is_final: bool) -> None:
            if discord_message_id is None:
                return
            try:
                content = cls._format_upload_discord_message(
                    project=project,
                    drive_download_url=direct_drive_download or drive_video_url,
                    requested_platforms=requested_platforms,
                    results_by_platform=dict(results_by_platform),
                    youtube_title=metadata.youtube.title,
                    youtube_description=metadata.youtube.description,
                    youtube_tags=metadata.youtube.tags,
                    tiktok_description=metadata.tiktok.description,
                    platform_scheduled_at=platform_scheduled_at,
                    is_final=is_final,
                )
                with discord_edit_lock:
                    DiscordService.edit_message(discord_message_id, content)
            except Exception:
                logger.warning(
                    "Discord edit failed for project %s", project_id, exc_info=True
                )

        def emit_platform_result(result: PlatformUploadResult) -> None:
            if platform_result_callback is not None:
                try:
                    platform_result_callback(asdict(result))
                except Exception:
                    logger.warning(
                        "Upload platform result callback failed: project_id=%s platform=%s",
                        project_id,
                        result.platform,
                        exc_info=True,
                    )
            edit_discord_snapshot(is_final=False)

        for skip_result in results_by_platform.values():
            emit_platform_result(skip_result)
```

Replace with:

```python
        def emit_platform_result(result: PlatformUploadResult) -> None:
            if platform_result_callback is not None:
                try:
                    platform_result_callback(asdict(result))
                except Exception:
                    logger.warning(
                        "Upload platform result callback failed: project_id=%s platform=%s",
                        project_id,
                        result.platform,
                        exc_info=True,
                    )
            try:
                DiscordService.update_job_platform(
                    project_id,
                    result.platform,
                    status=result.status,
                    url=result.url,
                    detail=result.detail,
                )
            except Exception:
                logger.warning(
                    "Discord platform update failed for %s/%s",
                    project_id, result.platform,
                    exc_info=True,
                )

        for skip_result in results_by_platform.values():
            emit_platform_result(skip_result)
```

The `discord_edit_lock` is no longer needed (VPS serializes edits). Remove its definition above this block (search for `discord_edit_lock = threading.Lock()` or similar within the function — delete that line). If `threading` is no longer imported elsewhere in the file, leave the import alone (it's likely used for other locks).

- [ ] **Step 2: Replace the initial Discord post**

Find the block at [upload_phase.py:737-770](../../../backend/app/services/upload_phase.py#L737-L770):

```python
        try:
            initial_message = DiscordService.post_message(
                cls._format_upload_discord_message(
                    project=project,
                    drive_download_url=direct_drive_download or drive_video_url,
                    requested_platforms=requested_platforms,
                    results_by_platform=results_by_platform,
                    youtube_title=metadata.youtube.title,
                    youtube_description=metadata.youtube.description,
                    youtube_tags=metadata.youtube.tags,
                    tiktok_description=metadata.tiktok.description,
                    platform_scheduled_at=platform_scheduled_at,
                    is_final=False,
                )
            )
        except Exception:
            logger.warning(
                "Initial Discord upload message failed for project %s",
                project_id,
                exc_info=True,
            )
            initial_message = None

        if initial_message is not None:
            discord_message_id = initial_message.id
            project.final_upload_discord_message_id = discord_message_id
            try:
                ProjectService.save(project)
            except Exception:
                logger.warning(
                    "Failed to persist early Discord message id for project %s",
                    project_id,
                    exc_info=True,
                )
```

Replace with:

```python
        discord_message_id = None
        try:
            job_response = DiscordService.create_job(
                project_id=project_id,
                account_id=project.scheduled_account_id or "",
                slot_time=project.scheduled_at or datetime.now(timezone.utc),
                anime_title=project.anime_name or "Unknown",
                description=metadata.tiktok.description,
                drive_video_url=direct_drive_download or drive_video_url,
                platforms_requested=list(requested_platforms),
            )
        except Exception:
            logger.warning(
                "Discord create_job failed for project %s",
                project_id,
                exc_info=True,
            )
            job_response = None

        if job_response is not None:
            discord_message_id = job_response.get("discord_message_id")
            if discord_message_id:
                project.final_upload_discord_message_id = discord_message_id
                try:
                    ProjectService.save(project)
                except Exception:
                    logger.warning(
                        "Failed to persist Discord message id for project %s",
                        project_id,
                        exc_info=True,
                    )
```

- [ ] **Step 3: Delete the redundant final-state post**

Find the block at [upload_phase.py:971-1002](../../../backend/app/services/upload_phase.py#L971-L1002):

```python
        # Finalize the Discord message: edit the early "upload in progress" message
        # into the final state.  If the early post failed (discord_message_id is
        # None), fall back to posting a single final message, matching the pre-
        # change behaviour.
        emit_progress(0.85, "finalize", "Finalizing upload state...")
        if discord_message_id is not None:
            edit_discord_snapshot(is_final=True)
        else:
            try:
                final_message = DiscordService.post_message(
                    cls._format_upload_discord_message(
                        project=project,
                        drive_download_url=direct_drive_download or drive_video_url,
                        requested_platforms=requested_platforms,
                        results_by_platform=results_by_platform,
                        youtube_title=metadata.youtube.title,
                        youtube_description=metadata.youtube.description,
                        youtube_tags=metadata.youtube.tags,
                        tiktok_description=metadata.tiktok.description,
                        platform_scheduled_at=platform_scheduled_at,
                        is_final=True,
                    )
                )
            except Exception:
                logger.warning(
                    "Final Discord upload message failed for project %s",
                    project_id,
                    exc_info=True,
                )
                final_message = None
            if final_message is not None:
                project.final_upload_discord_message_id = final_message.id
```

Replace with the simpler version (the embed is always live; there is no separate "final" state to format):

```python
        emit_progress(0.85, "finalize", "Finalizing upload state...")

        # YouTube quota fallback: if YouTube hit quota, post a follow-up generic
        # message with retry metadata so the operator can manually upload later.
        youtube_quota_hit = any(
            r.platform == "youtube" and r.status == "failed" and getattr(r, "quota_exceeded", False)
            for r in results_by_platform.values()
        )
        if youtube_quota_hit:
            quota_msg = (
                f"YouTube quota limit reached for **{project.anime_name or project_id}**. "
                "Manual retry metadata:\n```\n"
                f"Title: {metadata.youtube.title}\n\n"
                f"{metadata.youtube.description}\n\n"
                f"Tags: {', '.join(metadata.youtube.tags)}\n```"
            )
            try:
                DiscordService.post_message(quota_msg)
            except Exception:
                logger.warning(
                    "YouTube quota fallback message failed for %s",
                    project_id,
                    exc_info=True,
                )
```

- [ ] **Step 4: Replace the stale-message cleanup at the start of upload phase**

Find the block at [upload_phase.py:716-735](../../../backend/app/services/upload_phase.py#L716-L735):

```python
        if project.generation_discord_message_id:
            try:
                DiscordService.delete_message(project.generation_discord_message_id)
            except Exception:
                logger.warning(
                    "Failed to delete generation Discord message for project %s",
                    project_id,
                    exc_info=True,
                )
            project.generation_discord_message_id = None
        if project.final_upload_discord_message_id:
            try:
                DiscordService.delete_message(project.final_upload_discord_message_id)
            except Exception:
                logger.warning(
                    "Failed to delete stale upload Discord message for project %s",
                    project_id,
                    exc_info=True,
                )
            project.final_upload_discord_message_id = None
```

Replace with:

```python
        if project.generation_discord_message_id:
            try:
                DiscordService.delete_message(project.generation_discord_message_id)
            except Exception:
                logger.warning(
                    "Failed to delete generation Discord message for project %s",
                    project_id,
                    exc_info=True,
                )
            project.generation_discord_message_id = None
        if project.final_upload_discord_message_id:
            try:
                DiscordService.delete_job(project_id)
            except Exception:
                logger.warning(
                    "Failed to delete stale upload job for project %s",
                    project_id,
                    exc_info=True,
                )
            project.final_upload_discord_message_id = None
```

(Generation message keeps `delete_message`, upload message uses `delete_job` so the VPS removes both the embed AND the reminder message in one call.)

- [ ] **Step 5: Run static checks**

Run:
```bash
cd /home/sid/Projects/anime-tiktok-reproducer
pixi run python -c "from backend.app.services.upload_phase import UploadPhaseService; print('imported ok')"
```
Expected: imports cleanly. Any `NameError` (e.g., `discord_message_id` undefined, `discord_edit_lock` undefined, etc.) means a reference was missed — find and fix.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/upload_phase.py
git commit -m "refactor(backend): swap upload_phase Discord calls for VPS job APIs"
```

---

## Task 6c: Cascade-delete in `managed_delete`

**Files:**
- Modify: `backend/app/services/upload_phase.py`

- [ ] **Step 1: Update `managed_delete`'s Discord cleanup**

Find the block at [upload_phase.py:1361-1367](../../../backend/app/services/upload_phase.py#L1361-L1367):

```python
        try:
            if project.final_upload_discord_message_id:
                DiscordService.delete_message(project.final_upload_discord_message_id)
            elif project.generation_discord_message_id:
                DiscordService.delete_message(project.generation_discord_message_id)
        except Exception as exc:
            cleanup_warnings.append(f"discord cleanup failed: {exc}")
```

Replace with:

```python
        try:
            if project.final_upload_discord_message_id:
                DiscordService.delete_job(project_id)
            elif project.generation_discord_message_id:
                DiscordService.delete_message(project.generation_discord_message_id)
        except Exception as exc:
            cleanup_warnings.append(f"discord cleanup failed: {exc}")
```

(Upload-phase message → `delete_job` to cascade the VPS side. Generation-phase message → keeps `delete_message` since it's not part of a job.)

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/upload_phase.py
git commit -m "feat(backend): cascade-delete VPS job on managed_delete"
```

---

## Task 7: `processing.py` route — verify no change needed

**Files:**
- Modify (verify only): `backend/app/api/routes/processing.py`

- [ ] **Step 1: Confirm processing.py uses generic methods**

Run: `grep -n "DiscordService\." /home/sid/Projects/anime-tiktok-reproducer/backend/app/api/routes/processing.py`
Expected output:
```
129:                DiscordService.delete_message(project.generation_discord_message_id)
136:        discord_message = DiscordService.post_message(
```

Both are generic methods — they now go to VPS internally without code changes. **Nothing to edit.** Proceed.

If unexpected job-oriented method calls appear (`update_job_platform`, etc.), stop and revisit.

---

## Task 8: Extend `integration_health_service` with VPS healthz

**Files:**
- Modify: `backend/app/services/integration_health_service.py`

- [ ] **Step 1: Read the current Discord health snippet**

Run: `grep -n -A 8 "DiscordService.is_configured" /home/sid/Projects/anime-tiktok-reproducer/backend/app/services/integration_health_service.py`

Identify the block (around line 259) that reports Discord status. The exact wrapping varies; locate it.

- [ ] **Step 2: Add an active VPS ping after the `is_configured` check**

After the existing `is_configured()` check, add a quick GET on `/healthz` to verify the VPS is reachable. Example pattern (adapt to surrounding style):

```python
        # Existing is_configured() check stays as-is. Add active probe:
        if DiscordService.is_configured():
            try:
                import httpx
                base = settings.tiktok_server_base_url
                with httpx.Client(timeout=5.0) as c:
                    r = c.get(f"{base.rstrip('/')}/healthz")
                    r.raise_for_status()
            except Exception as e:
                # Treat as degraded but configured — health surfaces this.
                logger.warning("VPS healthz probe failed: %s", e)
                # The exact way to mark "degraded" depends on this module's existing
                # data-shape; mirror how other integrations report partial health.
```

(If the file uses dataclasses with explicit `degraded`/`error` fields, set those. If it returns a status string, set it to `"degraded"`. Don't invent a new shape.)

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/integration_health_service.py
git commit -m "feat(backend): integration health pings VPS /healthz"
```

---

## Task 9: End-to-end verification on a real project

**Files:**
- (no edits)

- [ ] **Step 1: Pre-flight checks**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
# Confirm VPS is reachable from main backend's host
curl -s https://tiktok.sididi.tv/healthz | jq

# Confirm config has device: lines on every account
pixi run python -c "
from backend.app.services.account_service import AccountService
for a in AccountService.list_accounts():
    assert a.device, f'{a.id}: missing device'
    print(a.id, '->', a.device)
"
```
Expected: every account prints with its device id.

- [ ] **Step 2: Run the full backend test suite**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run pytest backend/tests/ -v`
Expected: all tests pass (existing + new ones).

- [ ] **Step 3: Process one real project end-to-end**

In the running app (or via Project Manager UI), trigger an upload on a low-stakes project. Verify, in this order:

1. **Embed appears** in the upload Discord channel with the new layout: avatar, anime title, device, project id, platforms grid, description code-block, drive URL.
2. **Reminder posts** in the reminder channel with role ping + native forward (or URL fallback).
3. **Platforms update in place** as YT/FB/IG complete — no new messages, just embed edits.
4. **TikTok line stays "Pending handoff"** — there's no mobile app yet, so TikTok status doesn't change. That's expected.
5. **No "Upload removed" strikethrough behavior anywhere** — the cross system is gone.
6. **Project deletion via Project Manager** removes the embed and the reminder message in Discord (cascade).

If any of these fail, debug before proceeding.

- [ ] **Step 4: Manual cleanup of any leftover stale state**

Existing projects on disk may have `discord_upload_message_crossed: true` from before. Pydantic ignores it on read, but if you want to clean the JSON files explicitly:

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
pixi run python -c "
from pathlib import Path
import json
root = Path('backend/data/projects')
n = 0
for p in root.glob('*/project.json'):
    d = json.loads(p.read_text())
    if 'discord_upload_message_crossed' in d:
        del d['discord_upload_message_crossed']
        p.write_text(json.dumps(d, indent=2))
        n += 1
print(f'cleaned {n} project files')
"
```

(Optional — not required for correctness.)

---

## Task 10: Phase 4 — final verification sweep

A grep-driven cleanup pass to confirm nothing dead remains.

- [ ] **Step 1: Confirm no cross system references survive**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
grep -rn "_cross_overdue_upload_messages\|_cross_out_discord_message\|discord_upload_message_crossed" \
    backend/ frontend/src/
```
Expected: zero matches.

If any match, delete it (it shouldn't exist by this point).

- [ ] **Step 2: Confirm `ATR_DISCORD_WEBHOOK_URL` is gone**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
grep -rn "ATR_DISCORD_WEBHOOK_URL\|discord_webhook_url" \
    backend/ frontend/src/ .env.example
```
Expected: zero matches.

If found in `.env`, edit manually and remove. If found in code, delete the references.

- [ ] **Step 3: Confirm `_format_upload_discord_message` is gone**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
grep -rn "_format_upload_discord_message" backend/
```
Expected: zero matches.

- [ ] **Step 4: Confirm `_avatars_dir` is gone (if it was unused)**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
grep -rn "_avatars_dir" backend/
```
Expected: zero matches.

If matches still appear, the method had callers — Task 5 should have repointed them. Fix now.

- [ ] **Step 5: Confirm Project Manager UI doesn't reference the removed field**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
grep -rn "discord_upload_message_crossed\|crossed\|strikethrough" frontend/src/
```
Expected: zero matches related to the upload-message cross system. Other unrelated `crossed` mentions (CSS classes, etc.) are fine.

- [ ] **Step 6: Run the full test suite one more time**

Run: `cd /home/sid/Projects/anime-tiktok-reproducer && pixi run pytest backend/tests/ -v`
Expected: all green.

- [ ] **Step 7: Tag the milestone**

```bash
git tag -a backend-v0.2.0 -m "Phase 2+4: Main backend swapped to VPS Discord pipeline"
git push origin backend-v0.2.0
```

---

## Self-Review Notes

After all tasks complete:

1. **Spec coverage check** — every Section 10 (Main Backend Changes) item maps to a task above:
   - DiscordService rewrite → Task 2.
   - AccountConfig device field → Task 3.
   - upload_phase.py edits → Tasks 6a, 6b, 6c.
   - Project model field removal → Task 4.
   - processing.py route (no change) → Task 7.
   - integration_health extension → Task 8.
   - Frontend (Project Manager UI) → Task 10 step 5.
   - Avatar move → Task 5.
   - Section 5 env-var change → Task 1.
   - Section 12 Phase 4 (verification sweep) → Task 10.

2. **Type/method consistency:**
   - `DiscordService.create_job` signature matches the request body shape expected by VPS `/api/internal/jobs` (Plan A Task 11).
   - `update_job_platform(project_id, platform, *, status, url, detail)` matches VPS `POST /api/internal/jobs/{project_id}/platform-status`.
   - `delete_job(project_id)` matches VPS `DELETE /api/internal/jobs/{project_id}`.
   - `is_configured()` is True iff both `tiktok_server_base_url` and `tiktok_server_internal_token` are set — consistent across `discord_service.py`, `integration_health_service.py`, all callers.

3. **Out of scope (don't add):**
   - New mobile API surface — that's Plan A.
   - Mobile app — that's Plan C.
   - Bot-side Discord UI changes — VPS owns those.
