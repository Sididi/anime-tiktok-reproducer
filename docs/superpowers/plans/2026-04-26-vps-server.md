# VPS Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the standalone FastAPI service deployed at `tiktok.sididi.tv` that owns the TikTok job lifecycle, all Discord bot interactions, the mobile API surface, and avatar serving. End-of-plan deliverable: a Dockerized service deployable to a VPS, fully verifiable end-to-end via `curl` without any changes to the existing main backend or any mobile client.

**Architecture:** A single FastAPI process. Three API namespaces (`/api/internal/*`, `/api/mobile/*`, `/api/avatars/*`), each with its own auth dependency. Persistence is a single JSON file at `data/jobs.json` with an `asyncio.Lock`. Discord interactions go through a thin REST client (`httpx` + bot token, no `discord.py`). Embed structure is built by a pure function so it's straightforwardly unit-testable. Configuration is a slim YAML for structural data (devices, accounts) plus environment variables for all secrets.

**Tech Stack:** Python 3.13, FastAPI, `httpx`, `pydantic`, `pyyaml`, `pytest` + `pytest-asyncio` + `respx` (HTTP mocks). Managed by `uv`. Deployed via Docker + Caddy reverse proxy.

**Reference spec:** [docs/superpowers/specs/2026-04-26-mobile-tiktok-app-design.md](../specs/2026-04-26-mobile-tiktok-app-design.md). When in doubt, the spec is authoritative.

---

## File Structure

This plan creates the entire `server/` subtree. Nothing outside `server/` is modified. Everything below lives at `server/<path>` unless otherwise noted.

```
server/
├── pyproject.toml                  # uv project, deps + tooling config
├── uv.lock                         # generated, committed
├── Dockerfile                      # standalone deploy
├── docker-compose.yml              # for VPS systemd
├── Caddyfile                       # reverse-proxy config example
├── .env.example                    # all env var names documented
├── .gitignore                      # .venv, __pycache__, data/jobs.json, .env
├── README.md                       # quickstart
├── config/
│   ├── config.example.yaml         # commented example of slim VPS config
│   └── config.yaml                 # gitignored; operator fills locally
├── avatars/                        # static avatar files (committed)
│   └── .gitkeep
├── data/
│   └── .gitkeep                    # jobs.json created at runtime
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI factory + lifespan
│   ├── config.py                   # Settings dataclass + loader + validation
│   ├── models/
│   │   ├── __init__.py
│   │   └── job.py                  # TikTokJob, PlatformStatus, serializers
│   ├── services/
│   │   ├── __init__.py
│   │   ├── job_store.py            # JSON file CRUD + asyncio.Lock
│   │   ├── discord_client.py       # REST-only Discord client (httpx)
│   │   ├── embed_builder.py        # pure: job + cfg -> embed dict
│   │   └── reminder_service.py     # cross-channel forward (Q1) + URL fallback (Q2)
│   ├── auth/
│   │   ├── __init__.py
│   │   └── dependencies.py         # require_internal_token, require_device_token
│   └── api/
│       ├── __init__.py
│       ├── health.py               # /healthz
│       ├── internal.py             # /api/internal/*
│       ├── mobile.py               # /api/mobile/*
│       └── public.py               # /api/avatars/*
└── tests/
    ├── __init__.py
    ├── conftest.py                 # shared fixtures
    ├── test_config.py
    ├── test_job_model.py
    ├── test_job_store.py
    ├── test_embed_builder.py
    ├── test_discord_client.py
    ├── test_reminder_service.py
    ├── test_auth.py
    ├── test_internal_api.py
    ├── test_mobile_api.py
    └── test_public_api.py
```

Each file has one responsibility:
- `config.py` — pure config loading and validation, no domain logic.
- `models/job.py` — data shape only, no I/O.
- `services/job_store.py` — persistence only, no Discord calls.
- `services/discord_client.py` — Discord REST only, no business state.
- `services/embed_builder.py` — pure transform, zero I/O.
- `services/reminder_service.py` — composes `discord_client` to post the reminder.
- `auth/dependencies.py` — token validation for FastAPI.
- `api/*.py` — thin route handlers; delegate to services.
- `main.py` — wires everything together at startup.

---

## Conventions

- All code uses Python 3.13 features. `from __future__ import annotations` for forward refs in dataclasses.
- All datetimes are timezone-aware UTC.
- Tests use `pytest-asyncio` in `auto` mode — `async def test_*` functions are awaited automatically.
- HTTP mocks use `respx` for `httpx`.
- Atomic file writes use the temp-file + `os.replace` pattern.
- Logging: stdlib `logging` configured to JSON-friendly format in `main.py`; module loggers obtained via `logging.getLogger(__name__)`.

---

## Task 1: Project skeleton

**Files:**
- Create: `server/pyproject.toml`
- Create: `server/.env.example`
- Create: `server/.gitignore`
- Create: `server/README.md`
- Create: `server/config/config.example.yaml`
- Create: `server/avatars/.gitkeep`
- Create: `server/data/.gitkeep`
- Create: `server/app/__init__.py`
- Create: `server/app/{models,services,auth,api}/__init__.py`
- Create: `server/tests/__init__.py`

- [ ] **Step 1: Create the directory tree**

```bash
mkdir -p server/{app/{models,services,auth,api},tests,config,avatars,data}
touch server/avatars/.gitkeep server/data/.gitkeep
touch server/app/{__init__.py,models/__init__.py,services/__init__.py,auth/__init__.py,api/__init__.py}
touch server/tests/__init__.py
```

- [ ] **Step 2: Create `server/pyproject.toml`**

```toml
[project]
name = "tiktok-server"
version = "0.1.0"
description = "VPS-deployed FastAPI service for the anime-tiktok-reproducer mobile flow"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "httpx>=0.28.0",
    "pydantic>=2.10.0",
    "pyyaml>=6.0",
    "python-multipart>=0.0.18",
]

[dependency-groups]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "respx>=0.21.0",
    "ruff>=0.7.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-ra -q"

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "PL"]
ignore = ["PLR2004", "PLR0913"]
```

- [ ] **Step 3: Create `server/.env.example`**

```bash
# Authn
ATR_TIKTOK_SERVER_INTERNAL_TOKEN=replace_me
ATR_MOBILE_TOKEN_IPHONE_13_PRO=replace_me
ATR_MOBILE_TOKEN_PIXEL_8=replace_me

# Discord
ATR_DISCORD_BOT_TOKEN=replace_me
ATR_DISCORD_GUILD_ID=replace_me
ATR_DISCORD_UPLOAD_CHANNEL_ID=replace_me
ATR_DISCORD_REMINDER_CHANNEL_ID=replace_me
ATR_DISCORD_REMINDER_ROLE_ID=replace_me

# Server
ATR_SERVER_HOST=0.0.0.0
ATR_SERVER_PORT=8000
ATR_PUBLIC_BASE_URL=https://tiktok.sididi.tv
```

- [ ] **Step 4: Create `server/.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.env
config/config.yaml
data/jobs.json
```

- [ ] **Step 5: Create `server/config/config.example.yaml`**

```yaml
# Slim VPS config: structural data only. Secrets live in .env.
#
# Cross-checks:
#   - every account.device must reference a key in `devices`
#   - every account.avatar must be a filename present in ../avatars/
#   - every device id must have an env var ATR_MOBILE_TOKEN_<UPPER(id)> set
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

- [ ] **Step 6: Create `server/README.md`**

```markdown
# TikTok Server

VPS-deployed FastAPI service for the anime-tiktok-reproducer mobile flow.

## Quickstart (local dev)

```bash
cd server
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env                    # fill in real values
cp config/config.example.yaml config/config.yaml   # fill in real values
uv run uvicorn app.main:app --reload
```

Tests:
```bash
uv run pytest
```

## Deployment

See `Dockerfile`, `docker-compose.yml`, and `Caddyfile`. Deployed at `tiktok.sididi.tv`.
```

- [ ] **Step 7: Initialize uv project and lockfile**

Run:
```bash
cd server && uv sync
```
Expected: `uv.lock` created, `.venv/` populated.

- [ ] **Step 8: Commit**

```bash
git add server/
git commit -m "feat(server): project skeleton (pyproject, dirs, env example)"
```

---

## Task 2: Test scaffold (`conftest.py`)

**Files:**
- Create: `server/tests/conftest.py`

Pytest fixtures used across all subsequent tests.

- [ ] **Step 1: Write `server/tests/conftest.py`**

```python
"""Shared pytest fixtures for the VPS server test suite."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def tmp_server_dir(tmp_path: Path) -> Path:
    """A temporary server-root with empty avatars/ and data/."""
    (tmp_path / "avatars").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def example_avatar(tmp_server_dir: Path) -> Path:
    """A 1x1 PNG file under tmp_server_dir/avatars/anime_fr.jpg."""
    # 1x1 PNG (smallest valid)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63f8cf00000000050001a5f645450000000049454e"
        "44ae426082"
    )
    p = tmp_server_dir / "avatars" / "anime_fr.jpg"
    p.write_bytes(png_bytes)
    return p


@pytest.fixture
def example_yaml(tmp_server_dir: Path, example_avatar: Path) -> Path:
    """A minimal but valid config YAML."""
    yaml_text = """\
devices:
  iphone_13_pro:
    platform: "ios"
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_13_pro"
    avatar: "anime_fr.jpg"
"""
    p = tmp_server_dir / "config.yaml"
    p.write_text(yaml_text)
    return p


@pytest.fixture
def example_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Sets the env vars referenced by the example_yaml fixture."""
    monkeypatch.setenv("ATR_TIKTOK_SERVER_INTERNAL_TOKEN", "internal_secret")
    monkeypatch.setenv("ATR_MOBILE_TOKEN_IPHONE_13_PRO", "mobile_secret")
    monkeypatch.setenv("ATR_DISCORD_BOT_TOKEN", "bot_secret")
    monkeypatch.setenv("ATR_DISCORD_GUILD_ID", "111")
    monkeypatch.setenv("ATR_DISCORD_UPLOAD_CHANNEL_ID", "222")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_CHANNEL_ID", "333")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_ROLE_ID", "444")
    monkeypatch.setenv("ATR_PUBLIC_BASE_URL", "https://tiktok.sididi.tv")
    yield
```

- [ ] **Step 2: Verify pytest collects without errors**

Run: `cd server && uv run pytest --collect-only`
Expected: `0 tests collected` with no errors.

- [ ] **Step 3: Commit**

```bash
git add server/tests/conftest.py
git commit -m "test(server): conftest with config + env fixtures"
```

---

## Task 3: Config loader

**Files:**
- Create: `server/app/config.py`
- Test: `server/tests/test_config.py`

Loads the YAML, layers env vars on top, and validates cross-references at startup.

- [ ] **Step 1: Write the failing test (`tests/test_config.py`)**

```python
"""Tests for app.config.Settings.load()."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, ConfigError


def test_load_minimal_valid_config(example_yaml: Path, example_env, tmp_server_dir: Path):
    s = Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")
    assert s.internal_api_token == "internal_secret"
    assert s.discord.bot_token == "bot_secret"
    assert s.discord.upload_channel_id == "222"
    assert "anime_fr" in s.accounts
    assert s.accounts["anime_fr"].device == "iphone_13_pro"
    assert s.accounts["anime_fr"].avatar == "anime_fr.jpg"
    assert s.devices["iphone_13_pro"].platform == "ios"


def test_resolve_device_for_token(example_yaml: Path, example_env, tmp_server_dir: Path):
    s = Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")
    assert s.resolve_device_for_token("mobile_secret") == "iphone_13_pro"
    assert s.resolve_device_for_token("wrong") is None


def test_missing_device_token_raises(example_yaml: Path, monkeypatch, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_INTERNAL_TOKEN", "x")
    monkeypatch.setenv("ATR_DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("ATR_DISCORD_GUILD_ID", "x")
    monkeypatch.setenv("ATR_DISCORD_UPLOAD_CHANNEL_ID", "x")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_CHANNEL_ID", "x")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_ROLE_ID", "x")
    monkeypatch.setenv("ATR_PUBLIC_BASE_URL", "x")
    monkeypatch.delenv("ATR_MOBILE_TOKEN_IPHONE_13_PRO", raising=False)
    with pytest.raises(ConfigError, match="ATR_MOBILE_TOKEN_IPHONE_13_PRO"):
        Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")


def test_account_device_must_exist_in_devices(tmp_server_dir: Path, example_env):
    bad = tmp_server_dir / "bad.yaml"
    bad.write_text(
        """\
devices: {iphone_13_pro: {platform: ios}}
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "missing_device"
    avatar: "anime_fr.jpg"
"""
    )
    (tmp_server_dir / "avatars" / "anime_fr.jpg").write_bytes(b"\x89PNG")
    with pytest.raises(ConfigError, match="missing_device"):
        Settings.load(config_path=bad, avatars_dir=tmp_server_dir / "avatars")


def test_account_avatar_must_exist_on_disk(tmp_server_dir: Path, example_env):
    bad = tmp_server_dir / "bad.yaml"
    bad.write_text(
        """\
devices: {iphone_13_pro: {platform: ios}}
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_13_pro"
    avatar: "missing.png"
"""
    )
    with pytest.raises(ConfigError, match="missing.png"):
        Settings.load(config_path=bad, avatars_dir=tmp_server_dir / "avatars")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_config.py -v`
Expected: `ImportError: cannot import name 'Settings'`.

- [ ] **Step 3: Write `server/app/config.py`**

```python
"""Server settings loader: YAML structural config + environment secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(RuntimeError):
    """Raised when configuration is missing/invalid at startup."""


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    platform: str


@dataclass(frozen=True)
class AccountConfig:
    id: str
    name: str
    language: str
    device: str
    avatar: str


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    guild_id: str
    upload_channel_id: str
    reminder_channel_id: str
    reminder_role_id: str


@dataclass(frozen=True)
class Settings:
    internal_api_token: str
    public_base_url: str
    devices: dict[str, DeviceConfig]
    accounts: dict[str, AccountConfig]
    discord: DiscordConfig
    avatars_dir: Path
    # Maps mobile bearer token -> device id; built from env at load time.
    _device_tokens: dict[str, str] = field(default_factory=dict)

    def resolve_device_for_token(self, token: str) -> str | None:
        return self._device_tokens.get(token)

    @classmethod
    def load(cls, *, config_path: Path, avatars_dir: Path) -> "Settings":
        if not config_path.is_file():
            raise ConfigError(f"Config file not found: {config_path}")

        raw = yaml.safe_load(config_path.read_text()) or {}

        devices_raw = raw.get("devices", {}) or {}
        accounts_raw = raw.get("accounts", {}) or {}

        devices = {
            did: DeviceConfig(id=did, platform=str(d["platform"]))
            for did, d in devices_raw.items()
        }

        accounts: dict[str, AccountConfig] = {}
        for aid, a in accounts_raw.items():
            account = AccountConfig(
                id=aid,
                name=str(a["name"]),
                language=str(a["language"]),
                device=str(a["device"]),
                avatar=str(a["avatar"]),
            )
            if account.device not in devices:
                raise ConfigError(
                    f"Account {aid!r} references unknown device {account.device!r}"
                )
            if not (avatars_dir / account.avatar).is_file():
                raise ConfigError(
                    f"Account {aid!r} avatar {account.avatar!r} not found in {avatars_dir}"
                )
            accounts[aid] = account

        device_tokens: dict[str, str] = {}
        for did in devices:
            env_key = f"ATR_MOBILE_TOKEN_{did.upper()}"
            token = os.environ.get(env_key)
            if not token:
                raise ConfigError(f"Missing env var {env_key} for device {did!r}")
            device_tokens[token] = did

        def _required_env(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise ConfigError(f"Missing required env var {name}")
            return v

        return cls(
            internal_api_token=_required_env("ATR_TIKTOK_SERVER_INTERNAL_TOKEN"),
            public_base_url=_required_env("ATR_PUBLIC_BASE_URL"),
            devices=devices,
            accounts=accounts,
            discord=DiscordConfig(
                bot_token=_required_env("ATR_DISCORD_BOT_TOKEN"),
                guild_id=_required_env("ATR_DISCORD_GUILD_ID"),
                upload_channel_id=_required_env("ATR_DISCORD_UPLOAD_CHANNEL_ID"),
                reminder_channel_id=_required_env("ATR_DISCORD_REMINDER_CHANNEL_ID"),
                reminder_role_id=_required_env("ATR_DISCORD_REMINDER_ROLE_ID"),
            ),
            avatars_dir=avatars_dir,
            _device_tokens=device_tokens,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_config.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/config.py server/tests/test_config.py
git commit -m "feat(server): config loader with YAML + env validation"
```

---

## Task 4: Job model

**Files:**
- Create: `server/app/models/job.py`
- Test: `server/tests/test_job_model.py`

Pure data shape, no I/O. Carries snapshot of metadata + mutable state.

- [ ] **Step 1: Write the failing test (`tests/test_job_model.py`)**

```python
"""Tests for app.models.job."""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.job import PlatformStatus, TikTokJob


def _make_job(**overrides) -> TikTokJob:
    defaults = dict(
        project_id="proj_1",
        job_id="j_abc",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece Episode 1063",
        description="Description text",
        drive_video_url="https://drive.google.com/uc?export=download&id=xyz",
        slot_time=datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc),
        platforms_requested=["youtube", "facebook", "instagram", "tiktok"],
        status="pending",
        platform_statuses={
            "tiktok": PlatformStatus(status="pending"),
        },
        discord_message_id=None,
        reminder_message_id=None,
        acked_at=None,
        created_at=datetime(2026, 4, 26, 21, 0, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 26, 21, 0, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return TikTokJob(**defaults)


def test_job_round_trips_through_dict():
    job = _make_job()
    d = job.to_dict()
    assert d["project_id"] == "proj_1"
    assert d["status"] == "pending"
    assert d["slot_time"] == "2026-04-26T21:00:00+00:00"
    assert d["platform_statuses"]["tiktok"]["status"] == "pending"

    restored = TikTokJob.from_dict(d)
    assert restored == job


def test_platform_status_round_trip():
    ps = PlatformStatus(status="uploaded", url="https://youtu.be/abc", detail=None)
    assert PlatformStatus.from_dict(ps.to_dict()) == ps


def test_job_with_acked_state():
    job = _make_job(
        status="acked",
        acked_at=datetime(2026, 4, 26, 21, 5, tzinfo=timezone.utc),
    )
    d = job.to_dict()
    assert d["status"] == "acked"
    assert d["acked_at"] == "2026-04-26T21:05:00+00:00"
    assert TikTokJob.from_dict(d) == job
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_job_model.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Write `server/app/models/job.py`**

```python
"""Data shapes for TikTok jobs. No I/O, no dependencies on services."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

PlatformStatusName = Literal["pending", "uploading", "uploaded", "skipped", "failed"]
JobStatus = Literal["pending", "acked"]


@dataclass(frozen=True)
class PlatformStatus:
    status: PlatformStatusName
    url: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "url": self.url, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlatformStatus":
        return cls(status=d["status"], url=d.get("url"), detail=d.get("detail"))


@dataclass
class TikTokJob:
    project_id: str
    job_id: str
    account_id: str
    device_id: str
    anime_title: str
    description: str
    drive_video_url: str
    slot_time: datetime
    platforms_requested: list[str]
    status: JobStatus
    platform_statuses: dict[str, PlatformStatus]
    discord_message_id: str | None
    reminder_message_id: str | None
    acked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "job_id": self.job_id,
            "account_id": self.account_id,
            "device_id": self.device_id,
            "anime_title": self.anime_title,
            "description": self.description,
            "drive_video_url": self.drive_video_url,
            "slot_time": self.slot_time.isoformat(),
            "platforms_requested": list(self.platforms_requested),
            "status": self.status,
            "platform_statuses": {
                p: ps.to_dict() for p, ps in self.platform_statuses.items()
            },
            "discord_message_id": self.discord_message_id,
            "reminder_message_id": self.reminder_message_id,
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TikTokJob":
        return cls(
            project_id=d["project_id"],
            job_id=d["job_id"],
            account_id=d["account_id"],
            device_id=d["device_id"],
            anime_title=d["anime_title"],
            description=d["description"],
            drive_video_url=d["drive_video_url"],
            slot_time=datetime.fromisoformat(d["slot_time"]),
            platforms_requested=list(d["platforms_requested"]),
            status=d["status"],
            platform_statuses={
                p: PlatformStatus.from_dict(ps) for p, ps in d["platform_statuses"].items()
            },
            discord_message_id=d.get("discord_message_id"),
            reminder_message_id=d.get("reminder_message_id"),
            acked_at=datetime.fromisoformat(d["acked_at"]) if d.get("acked_at") else None,
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_job_model.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/models/
git add server/tests/test_job_model.py
git commit -m "feat(server): TikTokJob + PlatformStatus dataclasses"
```

---

## Task 5: Job store

**Files:**
- Create: `server/app/services/job_store.py`
- Test: `server/tests/test_job_store.py`

JSON-file CRUD with `asyncio.Lock`. Atomic writes via temp-file + replace.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for app.services.job_store."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.job import PlatformStatus, TikTokJob
from app.services.job_store import JobStore


def _make_job(project_id: str = "proj_1", device_id: str = "iphone_13_pro") -> TikTokJob:
    now = datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc)
    return TikTokJob(
        project_id=project_id,
        job_id=f"j_{project_id}",
        account_id="anime_fr",
        device_id=device_id,
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        slot_time=now,
        platforms_requested=["tiktok"],
        status="pending",
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=None,
        reminder_message_id=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )


async def test_create_and_get(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job()
    await store.create(job)
    fetched = await store.get(job.project_id)
    assert fetched == job


async def test_get_missing_returns_none(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    assert await store.get("missing") is None


async def test_create_duplicate_is_noop(tmp_path: Path):
    """Idempotency: re-creating same project_id keeps the existing record."""
    store = JobStore(tmp_path / "jobs.json")
    j1 = _make_job()
    await store.create(j1)
    j2 = _make_job()
    j2.anime_title = "Different"
    await store.create(j2)  # should NOT overwrite
    fetched = await store.get(j1.project_id)
    assert fetched is not None
    assert fetched.anime_title == "Title"


async def test_update(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job()
    await store.create(job)
    updated = await store.update(
        job.project_id,
        status="acked",
        acked_at=datetime(2026, 4, 26, 21, 5, tzinfo=timezone.utc),
    )
    assert updated.status == "acked"
    assert updated.acked_at is not None


async def test_update_missing_raises(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(KeyError):
        await store.update("missing", status="acked")


async def test_delete(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job())
    await store.delete("proj_1")
    assert await store.get("proj_1") is None


async def test_delete_missing_is_noop(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.delete("never_existed")  # must not raise


async def test_list_for_device_filters_by_device_and_status(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job(project_id="a", device_id="iphone_13_pro"))
    await store.create(_make_job(project_id="b", device_id="pixel_8"))
    j_acked = _make_job(project_id="c", device_id="iphone_13_pro")
    j_acked.status = "acked"
    await store.create(j_acked)

    pending_iphone = await store.list_for_device("iphone_13_pro", status="pending")
    assert {j.project_id for j in pending_iphone} == {"a"}

    all_iphone = await store.list_for_device("iphone_13_pro")
    assert {j.project_id for j in all_iphone} == {"a", "c"}


async def test_persists_across_instances(tmp_path: Path):
    """JSON file survives store re-instantiation."""
    p = tmp_path / "jobs.json"
    s1 = JobStore(p)
    await s1.create(_make_job())
    s2 = JobStore(p)
    assert (await s2.get("proj_1")) is not None


async def test_concurrent_writes_serialize(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job())

    async def bump(i: int):
        await store.update("proj_1", anime_title=f"v{i}")

    await asyncio.gather(*[bump(i) for i in range(50)])
    final = await store.get("proj_1")
    assert final is not None
    assert final.anime_title.startswith("v")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_job_store.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Write `server/app/services/job_store.py`**

```python
"""JSON-file persistence for TikTokJob. Async-safe via asyncio.Lock."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.models.job import TikTokJob


class JobStore:
    """Single JSON file at `path`, schema: {"jobs": {project_id: <job-dict>}}."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text())
            return data.get("jobs", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, jobs: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(prefix=".jobs.", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"jobs": jobs}, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def create(self, job: TikTokJob) -> None:
        async with self._lock:
            jobs = self._read()
            if job.project_id in jobs:
                return  # idempotent
            jobs[job.project_id] = job.to_dict()
            self._write(jobs)

    async def get(self, project_id: str) -> TikTokJob | None:
        async with self._lock:
            jobs = self._read()
            d = jobs.get(project_id)
            return TikTokJob.from_dict(d) if d else None

    async def update(self, project_id: str, **fields) -> TikTokJob:
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = TikTokJob.from_dict(jobs[project_id])
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = datetime.now(tz=timezone.utc)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job

    async def delete(self, project_id: str) -> None:
        async with self._lock:
            jobs = self._read()
            if project_id in jobs:
                del jobs[project_id]
                self._write(jobs)

    async def list_for_device(
        self, device_id: str, *, status: str | None = None
    ) -> list[TikTokJob]:
        async with self._lock:
            jobs = self._read()
            result: list[TikTokJob] = []
            for d in jobs.values():
                if d["device_id"] != device_id:
                    continue
                if status is not None and d["status"] != status:
                    continue
                result.append(TikTokJob.from_dict(d))
            return result
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_job_store.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/services/job_store.py server/tests/test_job_store.py
git commit -m "feat(server): JSON-file JobStore with asyncio.Lock"
```

---

## Task 6: Embed builder (pure function)

**Files:**
- Create: `server/app/services/embed_builder.py`
- Test: `server/tests/test_embed_builder.py`

Pure function: takes `(job, accounts, devices, public_base_url)` → returns Discord embed dict.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for app.services.embed_builder.build_embed."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import AccountConfig, DeviceConfig
from app.models.job import PlatformStatus, TikTokJob
from app.services.embed_builder import build_embed


def _job_fixture() -> TikTokJob:
    now = datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc)
    return TikTokJob(
        project_id="2ee46c92a4ce",
        job_id="j_abc",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece Episode 1063 — TikTok 2x3",
        description="Posted today!",
        drive_video_url="https://drive.google.com/uc?id=xyz",
        slot_time=now,
        platforms_requested=["youtube", "facebook", "instagram", "tiktok"],
        status="pending",
        platform_statuses={
            "youtube": PlatformStatus(status="uploaded", url="https://youtu.be/abc"),
            "facebook": PlatformStatus(status="skipped", detail="Not configured"),
            "instagram": PlatformStatus(status="uploading"),
            "tiktok": PlatformStatus(status="pending"),
        },
        discord_message_id=None,
        reminder_message_id=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )


def _accounts() -> dict[str, AccountConfig]:
    return {
        "anime_fr": AccountConfig(
            id="anime_fr",
            name="Anime FR",
            language="fr",
            device="iphone_13_pro",
            avatar="anime_fr.jpg",
        )
    }


def _devices() -> dict[str, DeviceConfig]:
    return {"iphone_13_pro": DeviceConfig(id="iphone_13_pro", platform="ios")}


def test_embed_has_author_with_avatar_url():
    embed = build_embed(
        _job_fixture(), _accounts(), _devices(), "https://tiktok.sididi.tv"
    )
    assert embed["author"]["name"] == "Anime FR"
    assert (
        embed["author"]["icon_url"]
        == "https://tiktok.sididi.tv/api/avatars/anime_fr.jpg"
    )


def test_embed_title_is_anime_title():
    embed = build_embed(
        _job_fixture(), _accounts(), _devices(), "https://tiktok.sididi.tv"
    )
    assert embed["title"] == "One Piece Episode 1063 — TikTok 2x3"


def test_embed_inline_fields_include_device_and_project():
    embed = build_embed(
        _job_fixture(), _accounts(), _devices(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f for f in embed["fields"]}
    assert any("iphone_13_pro" in f["value"] for f in fields.values())
    assert any("2ee46c92a4ce" in f["value"] for f in fields.values())


def test_embed_platforms_field_renders_all_statuses():
    embed = build_embed(
        _job_fixture(), _accounts(), _devices(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    plats = fields["Plateformes"]
    assert "✅" in plats and "YouTube" in plats and "youtu.be/abc" in plats
    assert "⚠️" in plats and "Facebook" in plats and "Not configured" in plats
    assert "⏳" in plats and "Instagram" in plats
    assert "🎯" in plats and "TikTok" in plats and "Pending" in plats


def test_embed_description_field_uses_code_block():
    embed = build_embed(
        _job_fixture(), _accounts(), _devices(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Description TikTok"].startswith("```")
    assert "Posted today!" in fields["Description TikTok"]
    assert fields["Description TikTok"].endswith("```")


def test_embed_includes_drive_link():
    embed = build_embed(
        _job_fixture(), _accounts(), _devices(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "drive.google.com/uc?id=xyz" in fields["Lien vidéo"]


def test_embed_after_ack_marks_tiktok_uploaded():
    job = _job_fixture()
    job.status = "acked"
    job.acked_at = datetime(2026, 4, 26, 21, 4, tzinfo=timezone.utc)
    job.platform_statuses["tiktok"] = PlatformStatus(status="uploaded")
    embed = build_embed(job, _accounts(), _devices(), "https://tiktok.sididi.tv")
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    plats = fields["Plateformes"]
    assert "✅ TikTok" in plats
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_embed_builder.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Write `server/app/services/embed_builder.py`**

```python
"""Pure function: build a Discord embed dict from a TikTokJob + config."""
from __future__ import annotations

from typing import Any

from app.config import AccountConfig, DeviceConfig
from app.models.job import PlatformStatus, TikTokJob

# Months in French for footer/description rendering.
_FR_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

_PLATFORM_DISPLAY = {
    "youtube": "YouTube",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

_STATUS_EMOJI = {
    "pending": "⏳",
    "uploading": "⏳",
    "uploaded": "✅",
    "skipped": "⚠️",
    "failed": "❌",
}


def _format_french_datetime(dt) -> str:
    return (
        f"{_FR_DAYS[dt.weekday()]} {dt.day} {_FR_MONTHS[dt.month - 1]} "
        f"{dt.year} à {dt.strftime('%H:%M')} UTC"
    )


def _format_platform_line(platform: str, ps: PlatformStatus) -> str:
    label = _PLATFORM_DISPLAY.get(platform, platform.title())
    if platform == "tiktok" and ps.status == "uploaded":
        emoji = "✅"
        suffix = " — Posté"
    elif platform == "tiktok" and ps.status == "pending":
        emoji = "🎯"
        suffix = " — Pending handoff"
    else:
        emoji = _STATUS_EMOJI.get(ps.status, "·")
        if ps.url:
            suffix = f" — {ps.url}"
        elif ps.detail:
            suffix = f" — {ps.status.title()} ({ps.detail})"
        else:
            suffix = f" — {ps.status.title()}"
    return f"{emoji} {label}{suffix}"


def build_embed(
    job: TikTokJob,
    accounts: dict[str, AccountConfig],
    devices: dict[str, DeviceConfig],
    public_base_url: str,
) -> dict[str, Any]:
    account = accounts[job.account_id]
    avatar_url = f"{public_base_url.rstrip('/')}/api/avatars/{account.avatar}"

    plat_lines = [
        _format_platform_line(p, job.platform_statuses.get(p, PlatformStatus(status="pending")))
        for p in job.platforms_requested
    ]

    fields = [
        {"name": "📱 Device", "value": job.device_id, "inline": True},
        {"name": "🆔 Project", "value": job.project_id, "inline": True},
        {"name": "Plateformes", "value": "\n".join(plat_lines), "inline": False},
        {
            "name": "Description TikTok",
            "value": f"```\n{job.description}\n```",
            "inline": False,
        },
        {"name": "Lien vidéo", "value": job.drive_video_url, "inline": False},
    ]

    return {
        "author": {"name": account.name, "icon_url": avatar_url},
        "title": job.anime_title,
        "description": f"Programmé le **{_format_french_datetime(job.slot_time)}**",
        "fields": fields,
        "footer": {
            "text": f"{account.name} · {job.device_id} · {job.slot_time.strftime('%H:%M')} UTC"
        },
    }
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_embed_builder.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/services/embed_builder.py server/tests/test_embed_builder.py
git commit -m "feat(server): pure embed builder for TikTok job state"
```

---

## Task 7: Discord REST client

**Files:**
- Create: `server/app/services/discord_client.py`
- Test: `server/tests/test_discord_client.py`

Thin httpx wrapper over Discord's REST API. Bot-token auth. Respects rate limits.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for app.services.discord_client.DiscordClient using respx."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.services.discord_client import DiscordClient


@respx.mock
async def test_post_message_sends_bot_auth_and_returns_id():
    route = respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg_42"})
    )
    async with DiscordClient(bot_token="abc") as client:
        msg_id = await client.post_message("c1", content="hello")
    assert msg_id == "msg_42"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bot abc"
    assert b'"content":"hello"' in sent.content


@respx.mock
async def test_post_message_with_embed_and_files_uses_multipart():
    route = respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg_43"})
    )
    async with DiscordClient(bot_token="abc") as client:
        await client.post_message(
            "c1",
            embed={"title": "T", "fields": []},
            message_reference={"type": 1, "channel_id": "c0", "message_id": "m0"},
        )
    assert route.called


@respx.mock
async def test_edit_message():
    route = respx.patch("https://discord.com/api/v10/channels/c1/messages/m1").mock(
        return_value=httpx.Response(200, json={"id": "m1"})
    )
    async with DiscordClient(bot_token="abc") as client:
        await client.edit_message("c1", "m1", embed={"title": "x"})
    assert route.called


@respx.mock
async def test_delete_message():
    route = respx.delete("https://discord.com/api/v10/channels/c1/messages/m1").mock(
        return_value=httpx.Response(204)
    )
    async with DiscordClient(bot_token="abc") as client:
        await client.delete_message("c1", "m1")
    assert route.called


@respx.mock
async def test_add_reaction_url_encodes_emoji():
    route = respx.put(
        "https://discord.com/api/v10/channels/c1/messages/m1/reactions/%E2%9C%85/@me"
    ).mock(return_value=httpx.Response(204))
    async with DiscordClient(bot_token="abc") as client:
        await client.add_reaction("c1", "m1", "✅")
    assert route.called


@respx.mock
async def test_retries_on_429_with_retry_after():
    """When Discord returns 429, the client should sleep and retry."""
    responses = [
        httpx.Response(429, json={"retry_after": 0.01}),
        httpx.Response(200, json={"id": "msg_99"}),
    ]
    route = respx.post(
        "https://discord.com/api/v10/channels/c1/messages"
    ).mock(side_effect=responses)
    async with DiscordClient(bot_token="abc") as client:
        msg_id = await client.post_message("c1", content="hi")
    assert msg_id == "msg_99"
    assert route.call_count == 2


@respx.mock
async def test_5xx_propagates_after_retry_budget_exhausted():
    respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with DiscordClient(bot_token="abc", max_retries=2) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_message("c1", content="hi")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_discord_client.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Write `server/app/services/discord_client.py`**

```python
"""REST-only Discord client using httpx with bot-token auth."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://discord.com/api/v10"


class DiscordClient:
    def __init__(self, *, bot_token: str, max_retries: int = 3) -> None:
        self._token = bot_token
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DiscordClient":
        self._client = httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bot {self._token}"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> httpx.Response:
        assert self._client is not None, "use within `async with` block"
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json_body)
            except httpx.RequestError as e:
                last_exc = e
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(min(2**attempt, 5))
                continue
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 1.0))
                logger.warning("Discord rate-limited; sleeping %.2fs", retry_after)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code >= 500 and attempt < self._max_retries:
                await asyncio.sleep(min(2**attempt, 5))
                continue
            resp.raise_for_status()
            return resp
        raise last_exc or RuntimeError("retries exhausted")

    async def post_message(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embed: dict | None = None,
        message_reference: dict | None = None,
    ) -> str:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        if message_reference is not None:
            body["message_reference"] = message_reference
        resp = await self._request("POST", f"/channels/{channel_id}/messages", json_body=body)
        return resp.json()["id"]

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        content: str | None = None,
        embed: dict | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        await self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}", json_body=body
        )

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        encoded = urllib.parse.quote(emoji, safe="")
        await self._request(
            "PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        )
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_discord_client.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/services/discord_client.py server/tests/test_discord_client.py
git commit -m "feat(server): Discord REST client with retry + rate-limit handling"
```

---

## Task 8: Reminder service

**Files:**
- Create: `server/app/services/reminder_service.py`
- Test: `server/tests/test_reminder_service.py`

Posts the cross-channel reminder. Tries Discord native FORWARD; falls back to URL-paste on any failure.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for app.services.reminder_service."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.reminder_service import post_reminder


async def test_forward_path_uses_message_reference_and_role_ping():
    """Q1: native forward succeeds first try."""
    discord = AsyncMock()
    discord.post_message.return_value = "rem_42"

    msg_id = await post_reminder(
        discord,
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        embed_message_id="m_embed",
        anime_title="One Piece 1063",
        account_name="Anime FR",
        device_name="iphone_13_pro",
        role_id="r_99",
        guild_id="g_1",
    )

    assert msg_id == "rem_42"
    assert discord.post_message.call_count == 1
    args = discord.post_message.call_args
    assert args.kwargs["message_reference"] == {
        "type": 1,
        "channel_id": "c_upload",
        "message_id": "m_embed",
    }
    assert "<@&r_99>" in args.kwargs["content"]
    assert "Anime FR" in args.kwargs["content"]


async def test_falls_back_to_url_when_forward_fails():
    """Q2 fallback: any error on forward -> retry without message_reference."""

    async def post_side_effect(*args, **kwargs):
        if kwargs.get("message_reference"):
            raise httpx.HTTPStatusError(
                "forbidden", request=httpx.Request("POST", "u"),
                response=httpx.Response(403)
            )
        return "rem_43"

    discord = AsyncMock()
    discord.post_message.side_effect = post_side_effect

    msg_id = await post_reminder(
        discord,
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        embed_message_id="m_embed",
        anime_title="One Piece 1063",
        account_name="Anime FR",
        device_name="iphone_13_pro",
        role_id="r_99",
        guild_id="g_1",
    )

    assert msg_id == "rem_43"
    assert discord.post_message.call_count == 2
    fallback_call = discord.post_message.call_args
    assert fallback_call.kwargs.get("message_reference") is None
    fallback_url = (
        "https://discord.com/channels/g_1/c_upload/m_embed"
    )
    assert fallback_url in fallback_call.kwargs["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_reminder_service.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Write `server/app/services/reminder_service.py`**

```python
"""Posts the cross-channel reminder, with Q1 native forward + Q2 URL fallback."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _ping_text(*, role_id: str, anime_title: str, account_name: str, device_name: str) -> str:
    return (
        f"<@&{role_id}> Time to post **{anime_title}** "
        f"on **{account_name}** ({device_name})"
    )


async def post_reminder(
    discord,
    *,
    upload_channel_id: str,
    reminder_channel_id: str,
    embed_message_id: str,
    anime_title: str,
    account_name: str,
    device_name: str,
    role_id: str,
    guild_id: str,
) -> str:
    """Post the reminder. Returns the reminder message id."""
    base_content = _ping_text(
        role_id=role_id,
        anime_title=anime_title,
        account_name=account_name,
        device_name=device_name,
    )
    forward_ref = {
        "type": 1,
        "channel_id": upload_channel_id,
        "message_id": embed_message_id,
    }
    try:
        return await discord.post_message(
            reminder_channel_id,
            content=base_content,
            message_reference=forward_ref,
        )
    except Exception as e:
        logger.warning("Native forward failed (%s); falling back to URL paste", e)
        url = f"https://discord.com/channels/{guild_id}/{upload_channel_id}/{embed_message_id}"
        return await discord.post_message(
            reminder_channel_id,
            content=f"{base_content}\n{url}",
        )
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_reminder_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/services/reminder_service.py server/tests/test_reminder_service.py
git commit -m "feat(server): reminder service with forward + URL fallback"
```

---

## Task 9: Auth dependencies

**Files:**
- Create: `server/app/auth/dependencies.py`
- Test: `server/tests/test_auth.py`

FastAPI dependencies for the two auth flavors. Tested by mounting them on a tiny throwaway app inside the test.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for app.auth.dependencies."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import require_device_token, require_internal_token
from app.config import Settings


def _make_settings(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


@pytest.fixture
def app(example_yaml: Path, example_env, tmp_server_dir: Path) -> FastAPI:
    settings = _make_settings(example_yaml, tmp_server_dir / "avatars")
    a = FastAPI()
    a.state.settings = settings

    @a.get("/internal", dependencies=[Depends(require_internal_token)])
    async def internal_route():
        return {"ok": True}

    @a.get("/mobile")
    async def mobile_route(device_id: str = Depends(require_device_token)):
        return {"device_id": device_id}

    return a


def test_internal_route_rejects_missing_auth(app: FastAPI):
    client = TestClient(app)
    r = client.get("/internal")
    assert r.status_code == 401


def test_internal_route_rejects_wrong_token(app: FastAPI):
    client = TestClient(app)
    r = client.get("/internal", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_internal_route_accepts_correct_token(app: FastAPI):
    client = TestClient(app)
    r = client.get("/internal", headers={"Authorization": "Bearer internal_secret"})
    assert r.status_code == 200


def test_mobile_route_returns_resolved_device(app: FastAPI):
    client = TestClient(app)
    r = client.get("/mobile", headers={"Authorization": "Bearer mobile_secret"})
    assert r.status_code == 200
    assert r.json() == {"device_id": "iphone_13_pro"}


def test_mobile_route_rejects_unknown_token(app: FastAPI):
    client = TestClient(app)
    r = client.get("/mobile", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_auth.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Write `server/app/auth/dependencies.py`**

```python
"""FastAPI auth dependencies. Settings are read from app.state at request time."""
from __future__ import annotations

from fastapi import Header, HTTPException, Request


def _bearer(authorization: str) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization[7:].strip()


async def require_internal_token(
    request: Request, authorization: str = Header(default="")
) -> None:
    token = _bearer(authorization)
    expected = request.app.state.settings.internal_api_token
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")


async def require_device_token(
    request: Request, authorization: str = Header(default="")
) -> str:
    token = _bearer(authorization)
    device_id = request.app.state.settings.resolve_device_for_token(token)
    if device_id is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return device_id
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_auth.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/auth/ server/tests/test_auth.py
git commit -m "feat(server): bearer-token deps for internal + mobile auth"
```

---

## Task 10: Health endpoint + main app skeleton

**Files:**
- Create: `server/app/api/health.py`
- Create: `server/app/main.py`

Bootstrap FastAPI app + lifespan that wires settings, JobStore, DiscordClient. Include `/healthz` for sanity checks. Subsequent tasks (11–13) will register their routers.

- [ ] **Step 1: Write the failing test (`tests/test_health.py`)**

```python
"""Tests for /healthz."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_healthz_returns_status_ok(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "jobs_pending" in body
```

- [ ] **Step 2: Write `server/app/api/health.py`**

```python
"""GET /healthz — uptime + counts. No auth."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    store = request.app.state.job_store
    settings = request.app.state.settings
    pending = 0
    for device_id in settings.devices:
        pending += len(await store.list_for_device(device_id, status="pending"))
    return {"status": "ok", "jobs_pending": pending}
```

- [ ] **Step 3: Write `server/app/main.py`**

```python
"""FastAPI app factory + lifespan."""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings
from app.services.discord_client import DiscordClient
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)


def _resolve_paths() -> tuple[Path, Path, Path]:
    base = Path(__file__).resolve().parent.parent
    config_path = Path(os.environ.get("ATR_TIKTOK_SERVER_CONFIG_PATH", base / "config" / "config.yaml"))
    avatars_dir = Path(os.environ.get("ATR_TIKTOK_SERVER_AVATARS_DIR", base / "avatars"))
    data_dir = Path(os.environ.get("ATR_TIKTOK_SERVER_DATA_DIR", base / "data"))
    return config_path, avatars_dir, data_dir


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config_path, avatars_dir, data_dir = _resolve_paths()
    settings = Settings.load(config_path=config_path, avatars_dir=avatars_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    job_store = JobStore(data_dir / "jobs.json")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with DiscordClient(bot_token=settings.discord.bot_token) as discord:
            app.state.settings = settings
            app.state.job_store = job_store
            app.state.discord = discord
            yield

    app = FastAPI(title="TikTok Server", lifespan=lifespan)
    # Bind for tests that don't go through lifespan
    app.state.settings = settings
    app.state.job_store = job_store
    app.include_router(health_router)
    return app


app = create_app()
```

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_health.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add server/app/main.py server/app/api/health.py server/tests/test_health.py
git commit -m "feat(server): app factory + lifespan + /healthz"
```

---

## Task 11: Internal API

**Files:**
- Create: `server/app/api/internal.py`
- Test: `server/tests/test_internal_api.py`

Implements the six `/api/internal/*` routes. Uses `JobStore`, `DiscordClient`, `embed_builder`, `reminder_service`.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for /api/internal/* endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

    app = create_app()
    discord = AsyncMock()
    discord.post_message.return_value = "msg_1"
    app.state.discord = discord
    return app, discord


JOB_PAYLOAD = {
    "project_id": "p1",
    "account_id": "anime_fr",
    "slot_time": "2026-04-26T21:00:00+00:00",
    "anime_title": "One Piece 1063",
    "description": "Posted today",
    "drive_video_url": "https://drive.google.com/uc?id=xyz",
    "platforms_requested": ["youtube", "tiktok"],
}
INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}


def test_create_job_posts_embed_and_reminder(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]

    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["discord_message_id"] == "msg_embed"
    # Two posts: embed in upload channel, reminder in reminder channel.
    assert discord.post_message.call_count == 2


def test_create_job_idempotent(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        r1 = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r2 = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
    assert r1.json()["discord_message_id"] == "msg_embed"
    assert r2.json()["discord_message_id"] == "msg_embed"  # same, no re-post
    assert discord.post_message.call_count == 2


def test_platform_status_edits_embed(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.post(
            "/api/internal/jobs/p1/platform-status",
            json={"platform": "youtube", "status": "uploaded", "url": "https://youtu.be/x"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    discord.edit_message.assert_called()


def test_delete_job_removes_messages(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
    assert r.status_code == 200
    # embed delete + reminder delete
    assert discord.delete_message.call_count == 2


def test_delete_missing_returns_200(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.delete("/api/internal/jobs/never", headers=INTERNAL_AUTH)
    assert r.status_code == 200


def test_generic_message_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_generic"
    with TestClient(app) as client:
        r = client.post(
            "/api/internal/discord/messages",
            json={"content": "hello"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    assert r.json()["message_id"] == "msg_generic"
    discord.post_message.assert_called_once()


def test_generic_message_edit(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.patch(
            "/api/internal/discord/messages/m_42",
            json={"content": "updated"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    discord.edit_message.assert_called_once()


def test_generic_message_delete(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.delete("/api/internal/discord/messages/m_42", headers=INTERNAL_AUTH)
    assert r.status_code == 200
    discord.delete_message.assert_called_once()


def test_unauthenticated_rejected(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD)
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_internal_api.py -v`
Expected: `ImportError` (router not registered yet).

- [ ] **Step 3: Write `server/app/api/internal.py`**

```python
"""Internal API routes consumed by the main backend."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import require_internal_token
from app.models.job import PlatformStatus, TikTokJob
from app.services.embed_builder import build_embed
from app.services.reminder_service import post_reminder

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/internal",
    dependencies=[Depends(require_internal_token)],
)


class CreateJobRequest(BaseModel):
    project_id: str
    account_id: str
    slot_time: datetime
    anime_title: str
    description: str
    drive_video_url: str
    platforms_requested: list[str]


class CreateJobResponse(BaseModel):
    job_id: str
    discord_message_id: str | None


class PlatformStatusRequest(BaseModel):
    platform: str
    status: str
    url: str | None = None
    detail: str | None = None


class GenericMessageRequest(BaseModel):
    channel_id: str | None = None
    content: str | None = None
    embed: dict | None = None


class GenericMessageEditRequest(BaseModel):
    content: str | None = None
    embed: dict | None = None


@router.post("/jobs", response_model=CreateJobResponse)
async def create_job(req: CreateJobRequest, request: Request) -> CreateJobResponse:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    if req.account_id not in settings.accounts:
        raise HTTPException(400, f"Unknown account {req.account_id!r}")
    account = settings.accounts[req.account_id]

    existing = await store.get(req.project_id)
    if existing is not None:
        return CreateJobResponse(
            job_id=existing.job_id, discord_message_id=existing.discord_message_id
        )

    now = datetime.now(tz=timezone.utc)
    platform_statuses = {
        p: PlatformStatus(status="pending") for p in req.platforms_requested
    }
    job = TikTokJob(
        project_id=req.project_id,
        job_id=f"j_{secrets.token_hex(4)}",
        account_id=req.account_id,
        device_id=account.device,
        anime_title=req.anime_title,
        description=req.description,
        drive_video_url=req.drive_video_url,
        slot_time=req.slot_time,
        platforms_requested=list(req.platforms_requested),
        status="pending",
        platform_statuses=platform_statuses,
        discord_message_id=None,
        reminder_message_id=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )

    embed_msg_id: str | None = None
    reminder_msg_id: str | None = None
    try:
        embed = build_embed(job, settings.accounts, settings.devices, settings.public_base_url)
        embed_msg_id = await discord.post_message(
            settings.discord.upload_channel_id, embed=embed
        )
        job.discord_message_id = embed_msg_id
        try:
            reminder_msg_id = await post_reminder(
                discord,
                upload_channel_id=settings.discord.upload_channel_id,
                reminder_channel_id=settings.discord.reminder_channel_id,
                embed_message_id=embed_msg_id,
                anime_title=job.anime_title,
                account_name=account.name,
                device_name=account.device,
                role_id=settings.discord.reminder_role_id,
                guild_id=settings.discord.guild_id,
            )
            job.reminder_message_id = reminder_msg_id
        except Exception as e:
            logger.warning("Reminder post failed for %s: %s", job.project_id, e)
    except Exception as e:
        logger.warning("Embed post failed for %s: %s", job.project_id, e)

    await store.create(job)
    return CreateJobResponse(job_id=job.job_id, discord_message_id=embed_msg_id)


@router.post("/jobs/{project_id}/platform-status")
async def platform_status(
    project_id: str, req: PlatformStatusRequest, request: Request
) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    new_status = PlatformStatus(status=req.status, url=req.url, detail=req.detail)
    existing = job.platform_statuses.get(req.platform)
    if existing == new_status:
        return {"ok": True, "noop": True}

    job.platform_statuses[req.platform] = new_status
    updated = await store.update(
        project_id, platform_statuses=job.platform_statuses
    )

    if updated.discord_message_id:
        try:
            embed = build_embed(
                updated, settings.accounts, settings.devices, settings.public_base_url
            )
            await discord.edit_message(
                settings.discord.upload_channel_id,
                updated.discord_message_id,
                embed=embed,
            )
        except Exception as e:
            logger.warning("Embed edit failed for %s: %s", project_id, e)

    return {"ok": True, "noop": False}


@router.delete("/jobs/{project_id}")
async def delete_job(project_id: str, request: Request) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await store.get(project_id)
    if job is None:
        return {"ok": True, "deleted": False}

    if job.discord_message_id:
        try:
            await discord.delete_message(
                settings.discord.upload_channel_id, job.discord_message_id
            )
        except Exception as e:
            logger.warning("Embed delete failed for %s: %s", project_id, e)
    if job.reminder_message_id:
        try:
            await discord.delete_message(
                settings.discord.reminder_channel_id, job.reminder_message_id
            )
        except Exception as e:
            logger.warning("Reminder delete failed for %s: %s", project_id, e)

    await store.delete(project_id)
    return {"ok": True, "deleted": True}


@router.post("/discord/messages")
async def post_discord_message(req: GenericMessageRequest, request: Request) -> dict:
    settings = request.app.state.settings
    discord = request.app.state.discord
    channel_id = req.channel_id or settings.discord.upload_channel_id
    msg_id = await discord.post_message(channel_id, content=req.content, embed=req.embed)
    return {"message_id": msg_id}


@router.patch("/discord/messages/{message_id}")
async def patch_discord_message(
    message_id: str, req: GenericMessageEditRequest, request: Request
) -> dict:
    settings = request.app.state.settings
    discord = request.app.state.discord
    await discord.edit_message(
        settings.discord.upload_channel_id, message_id, content=req.content, embed=req.embed
    )
    return {"ok": True}


@router.delete("/discord/messages/{message_id}")
async def delete_discord_message(message_id: str, request: Request) -> dict:
    settings = request.app.state.settings
    discord = request.app.state.discord
    await discord.delete_message(settings.discord.upload_channel_id, message_id)
    return {"ok": True}
```

- [ ] **Step 4: Register the router in `server/app/main.py`**

In `create_app()`, after the existing `app.include_router(health_router)` line, add:

```python
from app.api.internal import router as internal_router
app.include_router(internal_router)
```

(Replace the `from app.api.health import ...` import line with both imports at the top of `main.py`.)

- [ ] **Step 5: Run tests**

Run: `cd server && uv run pytest tests/test_internal_api.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add server/app/api/internal.py server/app/main.py server/tests/test_internal_api.py
git commit -m "feat(server): /api/internal/* job + generic Discord routes"
```

---

## Task 12: Mobile API

**Files:**
- Create: `server/app/api/mobile.py`
- Test: `server/tests/test_mobile_api.py`

Per-device authenticated routes for the mobile app.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for /api/mobile/* endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


JOB_PAYLOAD = {
    "project_id": "p1",
    "account_id": "anime_fr",
    "slot_time": "2026-04-26T21:00:00+00:00",
    "anime_title": "One Piece 1063",
    "description": "Posted today",
    "drive_video_url": "https://drive.google.com/uc?id=xyz",
    "platforms_requested": ["youtube", "tiktok"],
}
INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}
MOBILE_AUTH = {"Authorization": "Bearer mobile_secret"}


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

    app = create_app()
    discord = AsyncMock()
    discord.post_message.return_value = "msg_x"
    app.state.discord = discord
    return app, discord


def test_me_returns_device_and_accounts(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/mobile/me", headers=MOBILE_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "iphone_13_pro"
    assert {a["id"] for a in body["accounts"]} == {"anime_fr"}
    assert body["accounts"][0]["avatar_url"].endswith("/api/avatars/anime_fr.jpg")


def test_jobs_list_filters_by_device_pending_only(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.get("/api/mobile/jobs", headers=MOBILE_AUTH)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["project_id"] == "p1"
    assert item["status"] == "pending"
    assert item["account_avatar_url"].endswith("/api/avatars/anime_fr.jpg")


def test_video_url_returned(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r_create = client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        )
        job_id = r_create.json()["job_id"]
        r = client.get(f"/api/mobile/jobs/{job_id}/video-url", headers=MOBILE_AUTH)
    assert r.status_code == 200
    assert r.json()["video_url"] == "https://drive.google.com/uc?id=xyz"


def test_ack_marks_acked_and_adds_reaction(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        r_create = client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        )
        job_id = r_create.json()["job_id"]
        r = client.post(f"/api/mobile/jobs/{job_id}/ack", headers=MOBILE_AUTH)
        # Confirm acked job is gone from pending list
        r_list = client.get("/api/mobile/jobs", headers=MOBILE_AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "acked"
    discord.add_reaction.assert_called_once()
    discord.edit_message.assert_called()
    assert r_list.json() == []


def test_ack_idempotent(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        r_create = client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        )
        job_id = r_create.json()["job_id"]
        client.post(f"/api/mobile/jobs/{job_id}/ack", headers=MOBILE_AUTH)
        r2 = client.post(f"/api/mobile/jobs/{job_id}/ack", headers=MOBILE_AUTH)
    assert r2.status_code == 200
    # add_reaction called only once total
    assert discord.add_reaction.call_count == 1


def test_unauthenticated_mobile_rejected(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/mobile/jobs")
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_mobile_api.py -v`
Expected: errors (router missing).

- [ ] **Step 3: Write `server/app/api/mobile.py`**

```python
"""Mobile API: per-device-bearer-token routes consumed by the React Native app."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_device_token
from app.models.job import PlatformStatus
from app.services.embed_builder import build_embed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mobile")


def _avatar_url(public_base_url: str, filename: str) -> str:
    return f"{public_base_url.rstrip('/')}/api/avatars/{filename}"


@router.get("/me")
async def me(request: Request, device_id: str = Depends(require_device_token)) -> dict:
    settings = request.app.state.settings
    accounts = [
        {
            "id": acc.id,
            "name": acc.name,
            "avatar_url": _avatar_url(settings.public_base_url, acc.avatar),
        }
        for acc in settings.accounts.values()
        if acc.device == device_id
    ]
    return {"device_id": device_id, "accounts": accounts}


@router.get("/jobs")
async def list_jobs(
    request: Request, device_id: str = Depends(require_device_token)
) -> list[dict]:
    settings = request.app.state.settings
    store = request.app.state.job_store
    jobs = await store.list_for_device(device_id, status="pending")
    out: list[dict] = []
    for j in jobs:
        account = settings.accounts[j.account_id]
        out.append(
            {
                "job_id": j.job_id,
                "project_id": j.project_id,
                "account_id": j.account_id,
                "account_name": account.name,
                "account_avatar_url": _avatar_url(settings.public_base_url, account.avatar),
                "anime_title": j.anime_title,
                "description": j.description,
                "slot_time": j.slot_time.isoformat(),
                "status": j.status,
            }
        )
    return out


async def _job_for_device_or_404(request: Request, job_id: str, device_id: str):
    store = request.app.state.job_store
    # job_id-based lookup: scan jobs, since store is keyed by project_id.
    # Volume is tiny so this is fine.
    for j in await store.list_for_device(device_id):
        if j.job_id == job_id:
            return j
    raise HTTPException(404, "Job not found")


@router.get("/jobs/{job_id}/video-url")
async def video_url(
    job_id: str, request: Request, device_id: str = Depends(require_device_token)
) -> dict:
    job = await _job_for_device_or_404(request, job_id, device_id)
    return {"video_url": job.drive_video_url}


@router.post("/jobs/{job_id}/ack")
async def ack(
    job_id: str, request: Request, device_id: str = Depends(require_device_token)
) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await _job_for_device_or_404(request, job_id, device_id)
    if job.status == "acked":
        return {"ok": True, "status": "acked"}

    job.platform_statuses["tiktok"] = PlatformStatus(status="uploaded")
    updated = await store.update(
        job.project_id,
        status="acked",
        acked_at=datetime.now(tz=timezone.utc),
        platform_statuses=job.platform_statuses,
    )

    if updated.discord_message_id:
        try:
            embed = build_embed(
                updated, settings.accounts, settings.devices, settings.public_base_url
            )
            await discord.edit_message(
                settings.discord.upload_channel_id,
                updated.discord_message_id,
                embed=embed,
            )
            await discord.add_reaction(
                settings.discord.upload_channel_id, updated.discord_message_id, "✅"
            )
        except Exception as e:
            logger.warning("Discord ack-side updates failed for %s: %s", job.project_id, e)

    return {"ok": True, "status": updated.status}
```

- [ ] **Step 4: Register router in `server/app/main.py`**

Add to `create_app()`:
```python
from app.api.mobile import router as mobile_router
app.include_router(mobile_router)
```

- [ ] **Step 5: Run tests**

Run: `cd server && uv run pytest tests/test_mobile_api.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add server/app/api/mobile.py server/app/main.py server/tests/test_mobile_api.py
git commit -m "feat(server): /api/mobile/* per-device routes (jobs, video-url, ack, me)"
```

---

## Task 13: Public avatar API

**Files:**
- Create: `server/app/api/public.py`
- Test: `server/tests/test_public_api.py`

Static file serving for avatar images. No auth, with cache headers.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for /api/avatars/*."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

    return create_app()


def test_returns_avatar_bytes(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/avatars/anime_fr.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert "cache-control" in {k.lower() for k in r.headers}
    assert len(r.content) > 0


def test_404_on_missing(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/avatars/missing.png")
    assert r.status_code == 404


def test_path_traversal_rejected(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/avatars/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_public_api.py -v`
Expected: 404s on every test (router missing).

- [ ] **Step 3: Write `server/app/api/public.py`**

```python
"""Public avatar serving. No auth."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/avatars")


@router.get("/{filename}")
async def get_avatar(filename: str, request: Request) -> FileResponse:
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "Invalid filename")
    avatars_dir: Path = request.app.state.settings.avatars_dir
    try:
        resolved = (avatars_dir / filename).resolve()
        avatars_root = avatars_dir.resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "Invalid path") from None
    if avatars_root not in resolved.parents:
        raise HTTPException(400, "Invalid path")
    if not resolved.is_file():
        raise HTTPException(404, "Avatar not found")
    mime, _ = mimetypes.guess_type(resolved.name)
    return FileResponse(
        resolved,
        media_type=mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
```

- [ ] **Step 4: Register router in `server/app/main.py`**

Add to `create_app()`:
```python
from app.api.public import router as public_router
app.include_router(public_router)
```

- [ ] **Step 5: Run tests**

Run: `cd server && uv run pytest tests/test_public_api.py -v`
Expected: 3 passed.

- [ ] **Step 6: Run the full suite to confirm nothing regressed**

Run: `cd server && uv run pytest -v`
Expected: all tests pass (~50+).

- [ ] **Step 7: Commit**

```bash
git add server/app/api/public.py server/app/main.py server/tests/test_public_api.py
git commit -m "feat(server): /api/avatars/{filename} static serving"
```

---

## Task 14: Dockerfile, docker-compose, Caddyfile

**Files:**
- Create: `server/Dockerfile`
- Create: `server/docker-compose.yml`
- Create: `server/Caddyfile`

No new tests; deployment artifacts. Verify locally with `docker build`.

- [ ] **Step 1: Write `server/Dockerfile`**

```Dockerfile
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv (fast Python package manager).
RUN pip install --no-cache-dir uv==0.5.14

# Install dependencies first for better cache reuse.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy app code + static config.
COPY app ./app
COPY config ./config
COPY avatars ./avatars

# data/ is a volume mount in production.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Write `server/docker-compose.yml`**

```yaml
services:
  server:
    build: .
    image: tiktok-server:latest
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - jobs-data:/app/data
      - ./config/config.yaml:/app/config/config.yaml:ro
    ports:
      - "127.0.0.1:8000:8000"   # only Caddy reaches it on the VPS

volumes:
  jobs-data:
```

- [ ] **Step 3: Write `server/Caddyfile`**

```
# Place this file at /etc/caddy/Caddyfile on the VPS (or include from there).
# Caddy auto-provisions a Let's Encrypt cert for the domain.
tiktok.sididi.tv {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
```

- [ ] **Step 4: Verify the Docker build works locally**

Run: `cd server && docker build -t tiktok-server:dev .`
Expected: build succeeds. (No `docker run` needed yet — credentials would be missing.)

- [ ] **Step 5: Add deployment notes to `server/README.md`**

Append to the README:

```markdown
## Deployment (VPS)

1. SSH into the VPS, install Docker + docker-compose-plugin and Caddy.
2. Clone or sparse-checkout the `server/` subtree.
3. Copy `.env.example` → `.env` and `config/config.example.yaml` → `config/config.yaml`; fill in real values.
4. Place real avatar files in `avatars/`.
5. Add the `Caddyfile` snippet to `/etc/caddy/Caddyfile`; reload Caddy: `systemctl reload caddy`.
6. Bring the service up: `docker compose up -d --build`.
7. Verify: `curl https://tiktok.sididi.tv/healthz`.

## Update flow

```bash
git pull
docker compose up -d --build
```
```

- [ ] **Step 6: Commit**

```bash
git add server/Dockerfile server/docker-compose.yml server/Caddyfile server/README.md
git commit -m "feat(server): Dockerfile, compose, Caddy config + deploy docs"
```

---

## Task 15: Manual smoke test (curl plan)

**Files:**
- Modify: `server/README.md` (add a "Smoke test" section)

After deployment, run this checklist with `curl` against the live VPS. End-to-end verification of all the plumbing.

- [ ] **Step 1: Append a smoke-test section to `server/README.md`**

```markdown
## Smoke test (post-deploy)

Replace `INTERNAL`, `MOBILE`, `BASE_URL`, and channel/role IDs with real values.

```bash
INTERNAL="<ATR_TIKTOK_SERVER_INTERNAL_TOKEN>"
MOBILE="<ATR_MOBILE_TOKEN_IPHONE_13_PRO>"
BASE="https://tiktok.sididi.tv"
```

### 1. Health check

```bash
curl -s "$BASE/healthz" | jq
# Expected: {"status":"ok","jobs_pending":0}
```

### 2. Avatar serves

```bash
curl -sI "$BASE/api/avatars/anime_fr.jpg"
# Expected: HTTP/2 200, content-type: image/jpeg
```

### 3. Mobile auth gate

```bash
curl -s "$BASE/api/mobile/me"
# Expected: 401

curl -s "$BASE/api/mobile/me" -H "Authorization: Bearer $MOBILE" | jq
# Expected: {"device_id":"iphone_13_pro", "accounts":[...]}
```

### 4. Create a fake job → verify Discord embed + reminder

```bash
curl -s -X POST "$BASE/api/internal/jobs" \
  -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "smoke-1",
    "account_id": "anime_fr",
    "slot_time": "2026-04-26T21:00:00+00:00",
    "anime_title": "Smoke Test",
    "description": "Hello from the smoke test",
    "drive_video_url": "https://drive.google.com/uc?id=fake",
    "platforms_requested": ["youtube", "facebook", "instagram", "tiktok"]
  }' | jq
```

In Discord:
- The upload channel should show a rich embed with avatar + device + project + platforms grid + description + drive URL.
- The reminder channel should show a forwarded copy with the role ping.

### 5. Update a platform status → verify embed edits

```bash
curl -s -X POST "$BASE/api/internal/jobs/smoke-1/platform-status" \
  -H "Authorization: Bearer $INTERNAL" \
  -H "Content-Type: application/json" \
  -d '{"platform":"youtube","status":"uploaded","url":"https://youtu.be/SMOKE"}'
```

In Discord: the embed's YouTube line should change to `✅ YouTube — https://youtu.be/SMOKE`.

### 6. Mobile job list → ack flow

```bash
curl -s "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE" | jq
# Expected: array containing the smoke-1 job

JOB_ID=$(curl -s "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE" | jq -r '.[0].job_id')

curl -s "$BASE/api/mobile/jobs/$JOB_ID/video-url" -H "Authorization: Bearer $MOBILE" | jq
# Expected: {"video_url":"https://drive.google.com/uc?id=fake"}

curl -s -X POST "$BASE/api/mobile/jobs/$JOB_ID/ack" -H "Authorization: Bearer $MOBILE" | jq
# Expected: {"ok":true, "status":"acked"}
```

In Discord: the embed's TikTok line should change to `✅ TikTok — Posté`. The bot should add a `✅` reaction below the embed.

### 7. Cascade delete

```bash
curl -s -X DELETE "$BASE/api/internal/jobs/smoke-1" -H "Authorization: Bearer $INTERNAL" | jq
# Expected: {"ok":true, "deleted":true}
```

In Discord: the embed message and the reminder message both disappear.

### 8. Final state

```bash
curl -s "$BASE/api/mobile/jobs" -H "Authorization: Bearer $MOBILE" | jq
# Expected: []
```
```

- [ ] **Step 2: Commit**

```bash
git add server/README.md
git commit -m "docs(server): smoke-test checklist"
```

- [ ] **Step 3: Run the smoke test against the deployed VPS**

Follow the steps in the README. Each step has a clear "Expected" outcome — verify each one before declaring Phase 1 done.

- [ ] **Step 4: Tag the release**

```bash
git tag -a server-v0.1.0 -m "Phase 1: VPS server initial deployment"
git push origin server-v0.1.0
```

---

## Self-Review Notes

After all tasks complete:

1. **Spec coverage check:** every section of the spec that mentions VPS server behavior should map to a task above.
   - Section 6 (VPS Server) → Tasks 3–14.
   - Section 7 (Job Data Model & Lifecycle) → Tasks 4 (model), 5 (store), 11 (state transitions in API).
   - Section 11 (Error handling) → Tasks 7 (retry/rate-limit), 11 (Discord-failure swallowing), 12 (idempotent ack).
   - Section 12 Phase 1 ("VPS server up") → Task 15 smoke test.

2. **Consistency check:**
   - `discord_message_id` is `str | None` everywhere (model, internal API response, mobile API ack).
   - `device_id` resolution is uniformly via `resolve_device_for_token`.
   - All datetimes are timezone-aware UTC.
   - Avatar URL pattern `f"{public_base_url.rstrip('/')}/api/avatars/{avatar}"` used identically in both `embed_builder.py` and `mobile.py`.

3. **Out of scope (don't add):**
   - Drive API integration (the spec explicitly says VPS just stores the URL handed in by main backend).
   - SQLite (JSON file is intentional).
   - Any /api/internal change related to project/account creation (those live on main backend).
