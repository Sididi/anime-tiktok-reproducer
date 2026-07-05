# LAN Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Premiere Pro CEP panel (PC2, Windows) download project assets from and upload render outputs to the FastAPI backend (PC1, Arch) over the LAN, with automatic fallback to the existing Google Drive path, and make PC1 consume locally-received outputs without Drive round-trips.

**Architecture:** New `/api/lan/*` router on the existing FastAPI app serves a manifest built by `ExportService.build_manifest` (same tree as Drive) and receives output files (atomic write + background relay to Drive so Drive converges to today's state). A new `lan_tasks.js` CEP module implements the same task interface as `drive_tasks.js`; `main.js` probes `/api/lan/ping` per job and picks the engine. PC1-side consumers (readiness, preview, copyright, upload phase) become local-first with Drive fallback.

**Tech Stack:** FastAPI + pytest (backend), ES5 CommonJS Node in CEP panel (no test infra — manual/smoke verification), React/TS frontend with Playwright e2e.

**Spec:** `docs/superpowers/specs/2026-07-05-lan-transfer-design.md`

## Global Constraints

- Auth header: `X-ATR-LAN-Token`; token env var `ATR_LAN_TRANSFER_TOKEN` (pydantic setting `lan_transfer_token`, default `None` → endpoints return 503).
- `LanTransferService.API_VERSION = 1`; CEP requires exact match, else Drive fallback.
- Output upload whitelist: `output.mp4`, `output_no_music.wav`, `ATR_*.mp4` (case-insensitive) — reject `*__atr_proxy.mp4` and any name with path separators or leading dot.
- CEP settings: `lan_base_url` (prod value `http://arch-sid.local:8000`, empty = feature off), `lan_token`, `lan_probe_timeout_ms` (default 2500).
- Never change `drive_tasks.js`.
- LAN download layout on PC2 must be byte-identical to the Drive layout (manifest `relative_path` has the `SPM_*/` prefix stripped, matching `walkDriveTree`'s `relativePath`).
- Temp upload suffix on PC1: `.lan_tmp` (uuid-infixed), atomic `Path.replace` to final name, startup sweep.
- Backend code style: class-with-classmethods services, `asyncio.to_thread` for blocking work in routes, `logger = logging.getLogger(__name__)`.
- CEP code style: ES5 (`var`, no arrow functions, no template literals, no `const/let`), CommonJS `require`, Promise chains.
- Python via pixi: run tests with `pixi run -e default pytest ...` from repo root, or activate env accordingly (check `pixi.toml` task `test` if present; otherwise `cd backend && pixi run pytest tests/...`).

---

### Task 1: Backend setting, token guard, ping endpoint

**Files:**
- Modify: `backend/app/config.py` (add one setting near `cep_trigger_url_template`)
- Create: `backend/app/api/routes/lan_transfer.py`
- Modify: `backend/app/api/routes/__init__.py`
- Test: `backend/tests/test_lan_transfer_routes.py`

**Interfaces:**
- Produces: `settings.lan_transfer_token: str | None`; router `lan_router` with prefix `/lan` mounted under `/api`; `GET /api/lan/ping` → `{"ok": true, "api_version": 1}`; dependency `require_lan_token`.
- Later tasks add endpoints to this same router and reuse `require_lan_token` via router-level `dependencies`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_lan_transfer_routes.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr("app.config.settings.lan_transfer_token", "test-token")
    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c


AUTH = {"X-ATR-LAN-Token": "test-token"}


def test_ping_requires_token(client):
    assert client.get("/api/lan/ping").status_code == 401


def test_ping_rejects_wrong_token(client):
    resp = client.get("/api/lan/ping", headers={"X-ATR-LAN-Token": "wrong"})
    assert resp.status_code == 401


def test_ping_returns_api_version(client):
    resp = client.get("/api/lan/ping", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "api_version": 1}


def test_ping_503_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.lan_transfer_token", None)
    resp = client.get("/api/lan/ping", headers=AUTH)
    assert resp.status_code == 503
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pixi run --manifest-path ../pixi.toml pytest tests/test_lan_transfer_routes.py -v` (adapt to how other backend tests are run in this repo — check `pixi.toml` tasks; the important thing is the same interpreter/env as existing tests).
Expected: FAIL — 404 on `/api/lan/ping` (router doesn't exist).

- [ ] **Step 3: Add the setting**

In `backend/app/config.py`, inside `class Settings`, right after the `cep_trigger_url_template` line:

```python
    # LAN transfer (Premiere Pro PC pulls assets / pushes outputs over the LAN)
    lan_transfer_token: str | None = None
```

- [ ] **Step 4: Create the router**

```python
# backend/app/api/routes/lan_transfer.py
"""LAN transfer endpoints for the Premiere Pro CEP panel (spec:
docs/superpowers/specs/2026-07-05-lan-transfer-design.md)."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from ...config import settings

logger = logging.getLogger(__name__)

API_VERSION = 1


def require_lan_token(x_atr_lan_token: str | None = Header(default=None)) -> None:
    expected = settings.lan_transfer_token
    if not expected:
        raise HTTPException(status_code=503, detail="LAN transfer not configured")
    if not x_atr_lan_token or not hmac.compare_digest(x_atr_lan_token, expected):
        raise HTTPException(status_code=401, detail="Invalid LAN token")


router = APIRouter(prefix="/lan", tags=["lan-transfer"], dependencies=[Depends(require_lan_token)])


@router.get("/ping")
async def ping():
    return {"ok": True, "api_version": API_VERSION}
```

- [ ] **Step 5: Register the router**

In `backend/app/api/routes/__init__.py`: add `from .lan_transfer import router as lan_transfer_router` next to the other imports, and `api_router.include_router(lan_transfer_router)` after the `scheduling_router` line.

- [ ] **Step 6: Run tests to verify they pass**

Run: same command as Step 2. Expected: 4 PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py backend/app/api/routes/lan_transfer.py backend/app/api/routes/__init__.py backend/tests/test_lan_transfer_routes.py
git commit -m "feat(lan): LAN transfer router with token guard and ping endpoint"
```

---

### Task 2: Manifest service + manifest/file-download endpoints

**Files:**
- Create: `backend/app/services/lan_transfer_service.py`
- Modify: `backend/app/api/routes/lan_transfer.py`
- Test: `backend/tests/test_lan_transfer_service.py`, extend `backend/tests/test_lan_transfer_routes.py`

**Interfaces:**
- Consumes: `ExportService.build_manifest(project, matches) -> tuple[str, list[ManifestEntry]]` (entry fields: `relative_path` prefixed with `SPM_*/`, `source_path: Path | None`, `inline_content: bytes | None`); `ProjectService.load_matches(project_id) -> MatchList | None`.
- Produces: `LanTransferService.API_VERSION = 1`; `build_manifest_payload(project) -> dict` returning `{"api_version", "project_id", "folder_name", "drive_folder_id", "files": [{"relative_path", "size"}]}` (relative paths WITHOUT the folder prefix); `resolve_entry(project, relative_path) -> ManifestEntry | None`; `GET /api/lan/projects/{id}/manifest`; `GET /api/lan/projects/{id}/files/{relative_path:path}`.

- [ ] **Step 1: Verify the matches container shape**

Run: `grep -n "class MatchList" -A 8 backend/app/models/match.py` and `sed -n '190,230p' backend/app/api/routes/processing.py` (the existing `upload_manifest_to_drive` caller) to confirm the attribute holding `list[SceneMatch]` (expected: `.matches`) and how callers pass it to `ExportService`. Use exactly that shape below.

- [ ] **Step 2: Write the failing service tests**

```python
# backend/tests/test_lan_transfer_service.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.export_service import ManifestEntry
from app.services.lan_transfer_service import LanTransferService


class _FakeProject:
    id = "p1"
    drive_folder_id = "drv-folder-1"


@pytest.fixture
def fake_manifest(tmp_path: Path, monkeypatch):
    jsx = tmp_path / "import_project.jsx"
    jsx.write_bytes(b"// jsx" * 10)
    entries = [
        ManifestEntry(relative_path="SPM_demo_p1/import_project.jsx", source_path=jsx),
        ManifestEntry(
            relative_path="SPM_demo_p1/README.txt",
            inline_content=b"hello readme",
            mime_type="text/plain",
        ),
    ]
    monkeypatch.setattr(
        "app.services.lan_transfer_service.LanTransferService._build_entries",
        classmethod(lambda cls, project: ("SPM_demo_p1", entries)),
    )
    return entries


def test_manifest_payload_strips_folder_prefix(fake_manifest):
    payload = LanTransferService.build_manifest_payload(_FakeProject())
    assert payload["api_version"] == 1
    assert payload["folder_name"] == "SPM_demo_p1"
    assert payload["drive_folder_id"] == "drv-folder-1"
    paths = [f["relative_path"] for f in payload["files"]]
    assert paths == ["import_project.jsx", "README.txt"]
    assert payload["files"][0]["size"] == 60
    assert payload["files"][1]["size"] == len(b"hello readme")


def test_resolve_entry_by_stripped_path(fake_manifest):
    entry = LanTransferService.resolve_entry(_FakeProject(), "README.txt")
    assert entry is not None and entry.inline_content == b"hello readme"
    assert LanTransferService.resolve_entry(_FakeProject(), "../../etc/passwd") is None
    assert LanTransferService.resolve_entry(_FakeProject(), "nope.bin") is None
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_lan_transfer_service.py -v` (same env as Task 1). Expected: FAIL — `ModuleNotFoundError: app.services.lan_transfer_service`.

- [ ] **Step 4: Write the service**

```python
# backend/app/services/lan_transfer_service.py
"""LAN transfer: manifest building, output receiving, Drive relay.

The manifest reuses ExportService.build_manifest so the LAN tree is exactly
the tree uploaded to Drive; files are served by manifest lookup (never by
filesystem path join), which removes path-traversal risk by construction.
"""
from __future__ import annotations

import logging
from typing import Any

from .export_service import ExportService, ManifestEntry
from .project_service import ProjectService

logger = logging.getLogger(__name__)


class LanTransferService:
    API_VERSION = 1

    @classmethod
    def _build_entries(cls, project) -> tuple[str, list[ManifestEntry]]:
        match_list = ProjectService.load_matches(project.id)
        matches = list(match_list.matches) if match_list else []  # adjust per Step 1
        if not matches:
            raise FileNotFoundError("No matches found for project; run processing first")
        return ExportService.build_manifest(project, matches)

    @staticmethod
    def _strip_folder_prefix(relative_path: str) -> str:
        return relative_path.split("/", 1)[1] if "/" in relative_path else relative_path

    @staticmethod
    def _entry_size(entry: ManifestEntry) -> int:
        if entry.source_path is not None:
            return entry.source_path.stat().st_size
        return len(entry.inline_content or b"")

    @classmethod
    def build_manifest_payload(cls, project) -> dict[str, Any]:
        folder_name, entries = cls._build_entries(project)
        return {
            "api_version": cls.API_VERSION,
            "project_id": project.id,
            "folder_name": folder_name,
            "drive_folder_id": project.drive_folder_id,
            "files": [
                {
                    "relative_path": cls._strip_folder_prefix(entry.relative_path),
                    "size": cls._entry_size(entry),
                }
                for entry in entries
            ],
        }

    @classmethod
    def resolve_entry(cls, project, relative_path: str) -> ManifestEntry | None:
        try:
            _, entries = cls._build_entries(project)
        except FileNotFoundError:
            return None
        for entry in entries:
            if cls._strip_folder_prefix(entry.relative_path) == relative_path:
                return entry
        return None
```

Note: `ManifestEntry` import location — confirm with `grep -n "class ManifestEntry" backend/app/services/export_service.py` (if it lives elsewhere, import from there). Update `API_VERSION` in `lan_transfer.py` routes to reference `LanTransferService.API_VERSION` instead of its own constant.

- [ ] **Step 5: Run service tests**

Run: `pytest tests/test_lan_transfer_service.py -v`. Expected: 2 PASS.

- [ ] **Step 6: Add the two GET endpoints**

Append to `backend/app/api/routes/lan_transfer.py` (add imports `asyncio`, `FileResponse`, `Response`, `LanTransferService`, `ProjectService`):

```python
import asyncio

from fastapi.responses import FileResponse, Response

from ...services.lan_transfer_service import LanTransferService
from ...services.project_service import ProjectService


def _load_project_or_404(project_id: str):
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/manifest")
async def get_manifest(project_id: str):
    project = await asyncio.to_thread(_load_project_or_404, project_id)
    try:
        return await asyncio.to_thread(LanTransferService.build_manifest_payload, project)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/projects/{project_id}/files/{relative_path:path}")
async def download_manifest_file(project_id: str, relative_path: str):
    project = await asyncio.to_thread(_load_project_or_404, project_id)
    entry = await asyncio.to_thread(LanTransferService.resolve_entry, project, relative_path)
    if entry is None:
        raise HTTPException(status_code=404, detail="File not in project manifest")
    if entry.source_path is not None:
        return FileResponse(path=entry.source_path, filename=entry.source_path.name)
    return Response(content=entry.inline_content or b"", media_type=entry.mime_type or "application/octet-stream")
```

Check `ManifestEntry` has a `mime_type` attribute (it is constructed with one for inline entries); if the attribute is optional/absent on file entries, guard with `getattr(entry, "mime_type", None)`.

- [ ] **Step 7: Add route tests and run**

Append to `backend/tests/test_lan_transfer_routes.py`:

```python
def test_manifest_and_file_download(client, monkeypatch, tmp_path):
    from app.services.export_service import ManifestEntry

    src = tmp_path / "tts_edited.wav"
    src.write_bytes(b"RIFFxxxx")
    entries = [ManifestEntry(relative_path="SPM_x_p1/tts_edited.wav", source_path=src)]

    class _P:
        id = "p1"
        drive_folder_id = None

    monkeypatch.setattr("app.api.routes.lan_transfer._load_project_or_404", lambda pid: _P())
    monkeypatch.setattr(
        "app.services.lan_transfer_service.LanTransferService._build_entries",
        classmethod(lambda cls, project: ("SPM_x_p1", entries)),
    )

    manifest = client.get("/api/lan/projects/p1/manifest", headers=AUTH).json()
    assert manifest["files"] == [{"relative_path": "tts_edited.wav", "size": 8}]

    resp = client.get("/api/lan/projects/p1/files/tts_edited.wav", headers=AUTH)
    assert resp.status_code == 200 and resp.content == b"RIFFxxxx"

    assert client.get("/api/lan/projects/p1/files/missing.bin", headers=AUTH).status_code == 404
```

Run: `pytest tests/test_lan_transfer_routes.py tests/test_lan_transfer_service.py -v`. Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/lan_transfer_service.py backend/app/api/routes/lan_transfer.py backend/tests/test_lan_transfer_service.py backend/tests/test_lan_transfer_routes.py
git commit -m "feat(lan): manifest + file download endpoints backed by export manifest"
```

---

### Task 3: Output upload endpoint (whitelist, atomic write, startup sweep)

**Files:**
- Modify: `backend/app/services/lan_transfer_service.py`
- Modify: `backend/app/api/routes/lan_transfer.py`
- Modify: `backend/app/main.py` (startup sweep hook)
- Test: extend both test files

**Interfaces:**
- Consumes: `ExportService.get_output_dir(project_id) -> Path`.
- Produces: `LanTransferService.is_allowed_output_filename(name: str) -> bool`; `async receive_output_stream(project_id: str, filename: str, stream) -> Path`; `sweep_stale_tmp_files() -> int`; `POST /api/lan/projects/{id}/outputs/{filename}` → `{"ok": true, "filename", "size"}` (relay wired in Task 4).

- [ ] **Step 1: Write failing whitelist + write tests**

Append to `backend/tests/test_lan_transfer_service.py`:

```python
@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("output.mp4", True),
        ("OUTPUT.MP4", True),
        ("output_no_music.wav", True),
        ("ATR_final_v2.mp4", True),
        ("atr_final.mp4", True),           # ATR pattern is case-insensitive
        ("ATR_final__atr_proxy.mp4", False),
        ("output_instagram.mp4", False),
        ("evil/../output.mp4", False),
        ("..\\output.mp4", False),
        (".hidden.mp4", False),
        ("random.mp4", False),
    ],
)
def test_output_filename_whitelist(name, allowed):
    assert LanTransferService.is_allowed_output_filename(name) is allowed


@pytest.mark.anyio
async def test_receive_output_stream_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: tmp_path / pid / "output"),
    )

    async def _chunks():
        yield b"abc"
        yield b"def"

    dest = await LanTransferService.receive_output_stream("p1", "output.mp4", _chunks())
    assert dest == tmp_path / "p1" / "output" / "output.mp4"
    assert dest.read_bytes() == b"abcdef"
    assert not list(dest.parent.glob("*.lan_tmp"))


def test_sweep_stale_tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.lan_transfer_service.settings.projects_dir", tmp_path)
    out = tmp_path / "p1" / "output"
    out.mkdir(parents=True)
    (out / "output.mp4.deadbeef.lan_tmp").write_bytes(b"partial")
    (out / "output.mp4").write_bytes(b"keep")
    assert LanTransferService.sweep_stale_tmp_files() == 1
    assert (out / "output.mp4").exists()
    assert not list(out.glob("*.lan_tmp"))
```

If `pytest.mark.anyio` is not available in this repo's test env (check with `grep -rn "anyio\|asyncio_mode" backend/pyproject.toml backend/pytest.ini pixi.toml 2>/dev/null`), wrap the async test body with `asyncio.get_event_loop().run_until_complete(...)` inside a sync test instead — follow whatever an existing async test in `backend/tests/` does (`grep -rln "async def test_" backend/tests/`).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_lan_transfer_service.py -v`. Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement in the service**

Add to `lan_transfer_service.py` (new imports: `re`, `uuid`, `asyncio`, `from pathlib import Path`, `from ..config import settings`):

```python
    TMP_SUFFIX = ".lan_tmp"
    _ALLOWED_OUTPUT_EXACT = {"output.mp4", "output_no_music.wav"}
    _ATR_OUTPUT_RE = re.compile(r"^atr_.*\.mp4$", re.IGNORECASE)
    _PROXY_SUFFIX = "__atr_proxy.mp4"

    @classmethod
    def is_allowed_output_filename(cls, name: str) -> bool:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return False
        lowered = name.casefold()
        if lowered in cls._ALLOWED_OUTPUT_EXACT:
            return True
        if lowered.endswith(cls._PROXY_SUFFIX):
            return False
        return bool(cls._ATR_OUTPUT_RE.match(lowered))

    @classmethod
    async def receive_output_stream(cls, project_id: str, filename: str, stream) -> Path:
        output_dir = ExportService.get_output_dir(project_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = output_dir / f"{filename}.{uuid.uuid4().hex}{cls.TMP_SUFFIX}"
        final_path = output_dir / filename
        try:
            with tmp_path.open("wb") as fh:
                async for chunk in stream:
                    if chunk:
                        await asyncio.to_thread(fh.write, chunk)
            tmp_path.replace(final_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return final_path

    @classmethod
    def sweep_stale_tmp_files(cls) -> int:
        removed = 0
        projects_dir = settings.projects_dir
        if not projects_dir.exists():
            return 0
        for tmp_file in projects_dir.glob(f"*/output/*{cls.TMP_SUFFIX}"):
            try:
                tmp_file.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not remove stale LAN temp file: %s", tmp_file)
        if removed:
            logger.info("Swept %d stale LAN temp file(s)", removed)
        return removed
```

- [ ] **Step 4: Run service tests** — Expected: PASS.

- [ ] **Step 5: Add the POST route + route tests**

Route (append to `lan_transfer.py`; import `Request`):

```python
@router.post("/projects/{project_id}/outputs/{filename}")
async def upload_output(project_id: str, filename: str, request: Request):
    await asyncio.to_thread(_load_project_or_404, project_id)
    if not LanTransferService.is_allowed_output_filename(filename):
        raise HTTPException(status_code=422, detail="Filename not allowed")
    destination = await LanTransferService.receive_output_stream(project_id, filename, request.stream())
    size = destination.stat().st_size
    logger.info("LAN output received: project=%s file=%s bytes=%d", project_id, filename, size)
    return {"ok": True, "filename": filename, "size": size}
```

Tests (append to `test_lan_transfer_routes.py`):

```python
def _patch_project(monkeypatch):
    class _P:
        id = "p1"
        drive_folder_id = None
    monkeypatch.setattr("app.api.routes.lan_transfer._load_project_or_404", lambda pid: _P())


def test_upload_output_rejects_bad_filename(client, monkeypatch):
    _patch_project(monkeypatch)
    resp = client.post("/api/lan/projects/p1/outputs/output_instagram.mp4", headers=AUTH, content=b"x")
    assert resp.status_code == 422


def test_upload_output_writes_file(client, monkeypatch, tmp_path):
    _patch_project(monkeypatch)
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: tmp_path / "output"),
    )
    resp = client.post("/api/lan/projects/p1/outputs/output.mp4", headers=AUTH, content=b"videobytes")
    assert resp.status_code == 200
    assert resp.json()["size"] == 10
    assert (tmp_path / "output" / "output.mp4").read_bytes() == b"videobytes"
```

- [ ] **Step 6: Hook the startup sweep**

In `backend/app/main.py`, find the lifespan/startup section (near `project_startup_queue` usage) and add, as a plain synchronous call early in startup:

```python
from .services.lan_transfer_service import LanTransferService
...
    LanTransferService.sweep_stale_tmp_files()
```

- [ ] **Step 7: Run the full new-file test suite** — `pytest tests/test_lan_transfer_routes.py tests/test_lan_transfer_service.py -v`. Expected: all PASS. Also boot check: `python -c "from app.main import app"` (from `backend/`, env active).

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/lan_transfer_service.py backend/app/api/routes/lan_transfer.py backend/app/main.py backend/tests/
git commit -m "feat(lan): output upload endpoint with whitelist, atomic write, startup sweep"
```

---

### Task 4: Relay received outputs to Drive (background, with retry + status file)

**Files:**
- Modify: `backend/app/services/lan_transfer_service.py`
- Modify: `backend/app/api/routes/lan_transfer.py` (BackgroundTasks wiring)
- Test: extend `backend/tests/test_lan_transfer_service.py`

**Interfaces:**
- Consumes: `GoogleDriveService.is_configured()`, `GoogleDriveService.upsert_local_file(parent_id=, filename=, local_path=, chunksize=) -> dict`, `UploadPhaseService._resolve_drive_folder(project) -> tuple[str | None, str | None]`, `GoogleDriveService.ensure_project_folder(...)` (verify exact signature: `grep -n "def ensure_project_folder" -A 10 backend/app/services/google_drive_service.py`), `settings.drive_upload_chunk_mb`.
- Produces: `LanTransferService.relay_output_to_drive(project_id: str, local_path: Path) -> dict` (status dict `{"filename", "status": "uploaded"|"skipped"|"failed", "attempts", "file_id", "error"}`), relay status persisted to `<output_dir>/.lan_relay_status.json` (dict keyed by filename); POST endpoint schedules it via `BackgroundTasks`.

- [ ] **Step 1: Write failing tests**

```python
def test_relay_output_uploads_and_writes_status(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir(parents=True)
    video = out / "output.mp4"
    video.write_bytes(b"v")
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: out),
    )

    class _P:
        id = "p1"
        drive_folder_id = "folder-1"

    monkeypatch.setattr("app.services.lan_transfer_service.ProjectService.load", classmethod(lambda cls, pid: _P()))

    import app.services.google_drive_service as gds
    monkeypatch.setattr(gds.GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    calls = []
    monkeypatch.setattr(
        gds.GoogleDriveService, "upsert_local_file",
        classmethod(lambda cls, **kw: calls.append(kw) or {"id": "file-9"}),
    )
    import app.services.upload_phase as up
    monkeypatch.setattr(up.UploadPhaseService, "_resolve_drive_folder", classmethod(lambda cls, p, **kw: ("folder-1", None)))

    status = LanTransferService.relay_output_to_drive("p1", video)
    assert status["status"] == "uploaded" and status["file_id"] == "file-9"
    assert calls[0]["parent_id"] == "folder-1" and calls[0]["filename"] == "output.mp4"

    import json
    saved = json.loads((out / ".lan_relay_status.json").read_text())
    assert saved["output.mp4"]["status"] == "uploaded"


def test_relay_skips_when_drive_unconfigured(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir(parents=True)
    video = out / "output.mp4"
    video.write_bytes(b"v")
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: out),
    )
    import app.services.google_drive_service as gds
    monkeypatch.setattr(gds.GoogleDriveService, "is_configured", classmethod(lambda cls: False))
    status = LanTransferService.relay_output_to_drive("p1", video)
    assert status["status"] == "skipped"
```

- [ ] **Step 2: Run to verify failure** — Expected: AttributeError.

- [ ] **Step 3: Implement relay**

Add to the service (imports: `json`, `time`):

```python
    RELAY_STATUS_FILENAME = ".lan_relay_status.json"
    _RELAY_MAX_ATTEMPTS = 3
    _RELAY_RETRY_DELAY_S = 5.0

    @classmethod
    def _write_relay_status(cls, project_id: str, entry: dict) -> None:
        status_path = ExportService.get_output_dir(project_id) / cls.RELAY_STATUS_FILENAME
        data: dict = {}
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data[entry["filename"]] = entry
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(status_path)

    @classmethod
    def relay_output_to_drive(cls, project_id: str, local_path):
        # Local imports avoid circular deps (upload_phase imports are heavy).
        from .google_drive_service import GoogleDriveService
        from .upload_phase import UploadPhaseService

        entry = {"filename": local_path.name, "status": "pending", "attempts": 0, "file_id": None, "error": None}
        if not GoogleDriveService.is_configured():
            entry.update(status="skipped", error="Drive not configured")
            cls._write_relay_status(project_id, entry)
            return entry

        project = ProjectService.load(project_id)
        if not project:
            entry.update(status="failed", error="project not found")
            cls._write_relay_status(project_id, entry)
            return entry

        for attempt in range(1, cls._RELAY_MAX_ATTEMPTS + 1):
            entry["attempts"] = attempt
            try:
                folder_id, _ = UploadPhaseService._resolve_drive_folder(project)
                if not folder_id:
                    folder = GoogleDriveService.ensure_project_folder(ExportService.output_folder_name(project))
                    folder_id = folder["id"]  # adjust to actual return shape (Step 0 grep)
                uploaded = GoogleDriveService.upsert_local_file(
                    parent_id=folder_id,
                    filename=local_path.name,
                    local_path=local_path,
                    chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
                )
                entry.update(status="uploaded", file_id=str(uploaded.get("id") or ""), error=None)
                cls._write_relay_status(project_id, entry)
                # Drop the readiness Drive-video cache so the next readiness poll re-reads.
                UploadPhaseService._drive_video_cache.pop(project_id, None)
                logger.info("LAN relay uploaded %s to Drive (project=%s)", local_path.name, project_id)
                return entry
            except Exception as exc:
                entry.update(status="failed", error=str(exc))
                cls._write_relay_status(project_id, entry)
                logger.warning("LAN relay attempt %d/%d failed for %s: %s", attempt, cls._RELAY_MAX_ATTEMPTS, local_path.name, exc)
                if attempt < cls._RELAY_MAX_ATTEMPTS:
                    time.sleep(cls._RELAY_RETRY_DELAY_S)
        return entry
```

Also confirm `settings.drive_upload_chunk_mb` exists (`grep -n "drive_upload_chunk_mb" backend/app/config.py`) — it's used by `upload_phase.py` already.

- [ ] **Step 4: Wire BackgroundTasks in the POST route**

Change `upload_output` signature to `async def upload_output(project_id: str, filename: str, request: Request, background_tasks: BackgroundTasks):` (import `BackgroundTasks` from fastapi) and add before `return`:

```python
    background_tasks.add_task(LanTransferService.relay_output_to_drive, project_id, destination)
```

Also monkeypatch the relay to a no-op in `test_upload_output_writes_file` so route tests don't attempt Drive access:

```python
    monkeypatch.setattr(
        "app.api.routes.lan_transfer.LanTransferService.relay_output_to_drive",
        classmethod(lambda cls, pid, path: {"status": "skipped"}),
    )
```

- [ ] **Step 5: Run all LAN tests** — Expected: PASS (relay retry test runs fast because failures aren't triggered; do NOT add a test exercising the 5s sleeps).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/lan_transfer_service.py backend/app/api/routes/lan_transfer.py backend/tests/
git commit -m "feat(lan): background relay of received outputs to Drive with retry + status file"
```

---

### Task 5: Local-first upload readiness

**Files:**
- Modify: `backend/app/services/lan_transfer_service.py` (`find_local_upload_video`)
- Modify: `backend/app/services/upload_phase.py` (`UploadReadiness`, `_build_readiness`, `compute_readiness`, `list_manager_rows`)
- Test: `backend/tests/test_upload_readiness_local_first.py`

**Interfaces:**
- Consumes: `ExportService.VIDEO_EXTENSIONS`, `ExportService.get_output_dir`.
- Produces: `LanTransferService.find_local_upload_video(project_id) -> Path | None`; `UploadReadiness` gains `local_video_path: str | None = None` and `local_video_name: str | None = None` (dataclass fields with defaults, appended at the end); readiness is `green` when `metadata_exists` and a local video exists, **without any Drive video lookup**; manager rows gain `"local_video_available": bool`.

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_upload_readiness_local_first.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.lan_transfer_service import LanTransferService


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    out = tmp_path / "p1" / "output"
    out.mkdir(parents=True)
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: tmp_path / pid / "output"),
    )
    return out


def test_find_local_video_prefers_output_mp4(output_dir):
    (output_dir / "output.mp4").write_bytes(b"v")
    (output_dir / "ATR_alt.mp4").write_bytes(b"v")
    found = LanTransferService.find_local_upload_video("p1")
    assert found is not None and found.name == "output.mp4"


def test_find_local_video_single_atr(output_dir):
    (output_dir / "ATR_final.mp4").write_bytes(b"v")
    found = LanTransferService.find_local_upload_video("p1")
    assert found is not None and found.name == "ATR_final.mp4"


def test_find_local_video_ignores_proxies_and_conflicts(output_dir):
    (output_dir / "ATR_a__atr_proxy.mp4").write_bytes(b"v")
    assert LanTransferService.find_local_upload_video("p1") is None
    (output_dir / "ATR_a.mp4").write_bytes(b"v")
    (output_dir / "ATR_b.mp4").write_bytes(b"v")
    assert LanTransferService.find_local_upload_video("p1") is None


def test_readiness_green_with_local_video_and_no_drive(output_dir, monkeypatch):
    (output_dir / "output.mp4").write_bytes(b"v")
    from app.services.upload_phase import UploadPhaseService
    from app.services import upload_phase as up

    class _P:
        id = "p1"
        drive_folder_id = None
        drive_folder_url = None

    monkeypatch.setattr(up.ProjectService, "get_metadata_file", classmethod(lambda cls, pid: output_dir / "output.mp4"))  # any existing file
    monkeypatch.setattr(up.GoogleDriveService, "is_configured", classmethod(lambda cls: True))

    def _boom(*a, **kw):
        raise AssertionError("Drive must not be queried when a local video exists")

    monkeypatch.setattr(up.ExportService, "detect_upload_video_in_drive_root", classmethod(lambda cls, *a: _boom()))
    monkeypatch.setattr(up.GoogleDriveService, "find_project_folder_by_name", classmethod(lambda cls, *a, **kw: _boom()))

    readiness = UploadPhaseService.compute_readiness(_P())
    assert readiness.status == "green"
    assert readiness.local_video_name == "output.mp4"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_upload_readiness_local_first.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `find_local_upload_video`**

Add to `LanTransferService`:

```python
    @classmethod
    def find_local_upload_video(cls, project_id: str):
        output_dir = ExportService.get_output_dir(project_id)
        if not output_dir.exists():
            return None
        candidates = [
            p for p in output_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in ExportService.VIDEO_EXTENSIONS
            and cls.is_allowed_output_filename(p.name)
            and p.name.casefold() != "output_no_music.wav"
        ]
        for candidate in candidates:
            if candidate.name.casefold() == "output.mp4":
                return candidate
        if len(candidates) == 1:
            return candidates[0]
        return None
```

- [ ] **Step 4: Extend `UploadReadiness` and `_build_readiness`**

In `upload_phase.py`:
1. Append to the `UploadReadiness` dataclass (after the existing fields, with defaults so existing constructor calls stay valid): `local_video_path: str | None = None` and `local_video_name: str | None = None`.
2. Add parameter `local_video: Path | None = None` to `_build_readiness`. Inside, replace the video-presence logic: a project "has a video" when `local_video is not None` OR `video_count == 1`. When `local_video` is set: do not append "no output video found" / lookup-failure reasons, and set `status = "green"` iff `metadata_exists` (else `"orange"`). Pass `local_video_path=str(local_video) if local_video else None` and `local_video_name=local_video.name if local_video else None` to the constructor.
3. In `compute_readiness`, right after `metadata_exists = ...`, insert:

```python
        from .lan_transfer_service import LanTransferService

        local_video = LanTransferService.find_local_upload_video(project.id)
        if local_video is not None:
            folder_id, folder_url = cls._resolve_drive_folder(project, resolve_remote_url=False)
            return cls._build_readiness(
                metadata_exists=metadata_exists,
                folder_id=folder_id,
                folder_url=folder_url,
                video_files=[],
                local_video=local_video,
            )
```

(Keep the import local — `lan_transfer_service` imports `upload_phase` lazily in the relay, and this avoids a module-level cycle.)

- [ ] **Step 5: Local-first in `list_manager_rows`**

In `list_manager_rows`: before the Drive batch loop, compute `local_videos = {p.id: LanTransferService.find_local_upload_video(p.id) for p in projects}`. In the loop that collects `folder_ids` for the batch video lookup, skip projects where `local_videos[project.id] is not None` (their videos never get queried). In `_build_row`, when `local_videos.get(project.id)` is set, build readiness via the same local-first `_build_readiness` call as `compute_readiness` (bypassing `drive_root_videos`), and add `"local_video_available": local_videos.get(project.id) is not None` to every row dict (False for the others). Read the `_build_row` body first and keep all existing row keys untouched.

- [ ] **Step 6: Run the new tests AND the existing upload-phase suites**

Run: `pytest tests/test_upload_readiness_local_first.py tests/test_instagram_drive_preparation.py tests/test_managed_project_delete.py -v` plus `grep -rln "compute_readiness\|list_manager_rows" backend/tests/` and run whatever else matches. Expected: all PASS (regressions here mean `_build_readiness` defaults broke Drive-only behavior).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/lan_transfer_service.py backend/app/services/upload_phase.py backend/tests/test_upload_readiness_local_first.py
git commit -m "feat(lan): local-first upload readiness (skip Drive lookups when output exists locally)"
```

---

### Task 6: Local-first consumers (upload phase, duration checks, copyright, preview route)

**Files:**
- Modify: `backend/app/services/upload_phase.py`
- Modify: `backend/app/api/routes/project_manager.py`
- Test: `backend/tests/test_upload_phase_local_source.py`

**Interfaces:**
- Consumes: `UploadReadiness.local_video_path` (Task 5), `LanTransferService.find_local_upload_video`.
- Produces: `UploadPhaseService._ensure_drive_video(project, readiness) -> tuple[str | None, str | None]` (`(drive_file_id, video_name)`; upserts local video when Drive copy missing); all four `GoogleDriveService.download_file` call sites become local-first; new route `GET /api/project-manager/projects/{project_id}/local-video`.

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_upload_phase_local_source.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.upload_phase import UploadPhaseService, UploadReadiness


def _readiness(**overrides):
    base = dict(
        status="green", metadata_exists=True, drive_video_count=0,
        drive_video_id=None, drive_video_name=None, drive_video_web_url=None,
        reasons=[], drive_folder_id="folder-1", drive_folder_url=None,
        local_video_path=None, local_video_name=None,
    )
    base.update(overrides)
    return UploadReadiness(**base)


def test_ensure_drive_video_passthrough_when_drive_id_present():
    readiness = _readiness(drive_video_id="d1", drive_video_name="output.mp4")
    file_id, name = UploadPhaseService._ensure_drive_video(object(), readiness)
    assert (file_id, name) == ("d1", "output.mp4")


def test_ensure_drive_video_upserts_local(tmp_path, monkeypatch):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"v")
    readiness = _readiness(local_video_path=str(video), local_video_name="output.mp4")

    import app.services.upload_phase as up
    monkeypatch.setattr(up.GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    seen = {}
    monkeypatch.setattr(
        up.GoogleDriveService, "upsert_local_file",
        classmethod(lambda cls, **kw: seen.update(kw) or {"id": "new-id"}),
    )
    file_id, name = UploadPhaseService._ensure_drive_video(object(), readiness)
    assert file_id == "new-id" and name == "output.mp4"
    assert seen["parent_id"] == "folder-1"
```

- [ ] **Step 2: Run to verify failure** — Expected: no `_ensure_drive_video`. (The `UploadReadiness(**base)` construction also validates Task 5's field additions.)

- [ ] **Step 3: Implement `_ensure_drive_video`**

Add to `UploadPhaseService` (near `_resolve_drive_folder`):

```python
    @classmethod
    def _ensure_drive_video(cls, project, readiness: UploadReadiness) -> tuple[str | None, str | None]:
        """Drive file id/name of the final video, uploading the local copy if Drive lacks it."""
        if readiness.drive_video_id:
            return readiness.drive_video_id, readiness.drive_video_name
        if not readiness.local_video_path or not GoogleDriveService.is_configured():
            return None, None
        local = Path(readiness.local_video_path)
        if not local.exists():
            return None, None
        folder_id = readiness.drive_folder_id
        if not folder_id:
            folder = GoogleDriveService.ensure_project_folder(ExportService.output_folder_name(project))
            folder_id = folder["id"]  # match actual return shape (same as Task 4)
        uploaded = GoogleDriveService.upsert_local_file(
            parent_id=folder_id,
            filename=local.name,
            local_path=local,
            chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
        )
        return str(uploaded.get("id") or "") or None, local.name
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Convert the four consumers to local-first (no new tests; guarded by existing suites)**

Locate each with `grep -n "GoogleDriveService.download_file\|drive_video_id" backend/app/services/upload_phase.py backend/app/api/routes/project_manager.py`, then:

1. **`execute_upload` source video** (the `tempfile.TemporaryDirectory` block downloading `readiness.drive_video_id`): replace with

```python
            local_video = Path(readiness.local_video_path) if readiness.local_video_path else None
            video_name = readiness.drive_video_name or (local_video.name if local_video else "final_video.mp4")
            local_video_path = Path(tmp_dir) / video_name
            if local_video is not None and local_video.exists():
                emit_progress(0.30, "download", "Copying final video from local output...")
                shutil.copy2(local_video, local_video_path)
            else:
                emit_progress(0.30, "download", "Downloading final video from Drive...")
                GoogleDriveService.download_file(readiness.drive_video_id, local_video_path)
```

   Then, still in `execute_upload`, find every other use of `readiness.drive_video_id` / `readiness.drive_video_name` (public-read URL for the VPS payload, Discord message, gate checks). At the top of `execute_upload` right after readiness is computed and validated, insert `drive_video_id, drive_video_name = cls._ensure_drive_video(project, readiness)` and use those variables in place of the readiness attributes below. Relax the readiness gate from `not readiness.drive_video_id` to `not readiness.drive_video_id and not readiness.local_video_path`, and after `_ensure_drive_video` raise a clear error if `drive_video_id` is still `None` (this is the "relay failed and PC1 offline" case from the spec).

2. **`_check_platform_duration`**: change the gate `if readiness.status != "green" or not readiness.drive_video_id:` to `if readiness.status != "green" or not (readiness.drive_video_id or readiness.local_video_path):`, and the download block to:

```python
        video_name = readiness.drive_video_name or readiness.local_video_name or "final_video.mp4"
        original_path = prep_dir / video_name
        if not original_path.exists():
            if readiness.local_video_path and Path(readiness.local_video_path).exists():
                shutil.copy2(readiness.local_video_path, original_path)
            else:
                GoogleDriveService.download_file(readiness.drive_video_id, original_path)
```

3. **`build_copyright_audio`**: make the parameter optional (`no_music_file_id: str | None`) and replace the download block with:

```python
        no_music_path = prep_dir / "output_no_music.wav"
        if not no_music_path.exists():
            local_no_music = ExportService.get_output_dir(project_id) / "output_no_music.wav"
            if local_no_music.exists():
                shutil.copy2(local_no_music, no_music_path)
            elif no_music_file_id:
                GoogleDriveService.download_file(no_music_file_id, no_music_path)
            else:
                raise ValueError("output_no_music.wav not found locally or on Drive")
```

   In `check_copyright`, before the Drive children listing, add:

```python
        local_no_music = ExportService.get_output_dir(project_id) / "output_no_music.wav"
        if local_no_music.exists():
            no_music_available = True
```

   (keep the Drive listing as-is — it may additionally fill `no_music_file_id`; the route payload model for build-audio must make `no_music_file_id` optional: `grep -n "no_music_file_id" backend/app/api/routes/project_manager.py` and change the pydantic field to `str | None = None`).

4. **`copyright_video` route** in `project_manager.py`: at the top of the handler, before the cached-video check, add:

```python
    local_video = await asyncio.to_thread(LanTransferService.find_local_upload_video, project_id)
    if local_video is not None:
        return FileResponse(path=local_video, media_type="video/mp4")
```

   (import `LanTransferService`). Also add the new preview route:

```python
@router.get("/projects/{project_id}/local-video")
async def local_video(project_id: str):
    """Serve the locally stored final video (LAN transfer), if present."""
    video = await asyncio.to_thread(LanTransferService.find_local_upload_video, project_id)
    if video is None:
        raise HTTPException(status_code=404, detail="No local video for this project")
    return FileResponse(path=video, media_type="video/mp4", filename=video.name)
```

- [ ] **Step 6: Run the full backend test suite**

Run: `pytest backend/tests -x -q` (from the proper env). Expected: PASS — pay special attention to `test_instagram_drive_preparation.py` and any test touching `execute_upload`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/upload_phase.py backend/app/api/routes/project_manager.py backend/tests/test_upload_phase_local_source.py
git commit -m "feat(lan): local-first source video in upload phase, duration checks, copyright, preview"
```

---

### Task 7: Frontend — local-first video preview

**Files:**
- Modify: `frontend/src/types/index.ts` (project-manager row type)
- Modify: `frontend/src/components/project-manager/VideoPreviewModal.tsx`
- Modify: whichever component opens the modal (find with `grep -rn "VideoPreviewModal" frontend/src`)
- Test: extend `frontend/e2e/project-manager-upload-queue.spec.ts` fixtures (rows gain `local_video_available`)

**Interfaces:**
- Consumes: row field `local_video_available: boolean` (Task 5), endpoint `GET /api/project-manager/projects/{id}/local-video` (Task 6).
- Produces: preview modal renders a native `<video controls>` sourced from the local-video endpoint when `local_video_available`, else the existing Drive iframe.

- [ ] **Step 1: Inspect current wiring** — `grep -rn "VideoPreviewModal\|driveVideoId" frontend/src/components/project-manager/ frontend/src/types/index.ts` to see the modal's props and the row type name. Add `local_video_available: boolean` to the row type (optional `?` if the type is shared with older fixtures).

- [ ] **Step 2: Modify the modal**

`VideoPreviewModal.tsx` currently renders `<iframe src={"https://drive.google.com/file/d/" + driveVideoId + "/preview"} ...>`. Add props `projectId: string` and `localVideoAvailable: boolean` (threaded from the row by the opener component) and render:

```tsx
{localVideoAvailable ? (
  <video
    controls
    autoPlay
    className="h-full w-full"  /* match the iframe's existing classes */
    src={`/api/project-manager/projects/${projectId}/local-video`}
  />
) : (
  /* existing iframe unchanged */
)}
```

Match the surrounding styling/props conventions of the file (check how the iframe is sized and reuse the same classes).

- [ ] **Step 3: Update e2e fixtures**

In `frontend/e2e/project-manager-upload-queue.spec.ts` (and any other e2e spec that mocks `/api/project-manager/projects` rows — `grep -rln "project-manager/projects" frontend/e2e/`), add `local_video_available: false` to the mocked row objects so types stay valid. Add one test: with a row where `local_video_available: true`, open the preview and assert `page.locator("video")` is visible (route-mock the local-video URL with a tiny response or just assert the `src` attribute).

- [ ] **Step 4: Run checks**

Run: `cd frontend && npx tsc --noEmit` then the e2e suite the way this repo runs it (`grep -n "e2e\|playwright" frontend/package.json` for the script name, e.g. `npm run e2e -- project-manager-upload-queue`). Expected: type-check clean, e2e PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src frontend/e2e
git commit -m "feat(frontend): local-first video preview in project manager"
```

---

### Task 8: CEP settings — LAN fields

**Files:**
- Modify: `premiere-extension/tiktok-reproducer/client/main.js` (`DEFAULT_SETTINGS`, `loadSettings`/`readSettingsForm` wiring, `buildDrivePayloadBase`)
- Modify: `premiere-extension/tiktok-reproducer/client/index.html` (settings form fields)

**Interfaces:**
- Produces: `settings.lan_base_url`, `settings.lan_token`, `settings.lan_probe_timeout_ms` persisted in `%APPDATA%/Adobe/TiktokReproducer/state/settings.json`; `buildDrivePayloadBase().settings` carries the three LAN keys so both engines receive them.

- [ ] **Step 1: Extend `DEFAULT_SETTINGS`** (main.js ~line 67):

```js
  var DEFAULT_SETTINGS = {
    client_id: "",
    client_secret: "",
    refresh_token: "",
    parent_folder_id: "",
    lan_base_url: "",
    lan_token: "",
    lan_probe_timeout_ms: 2500,
    port: DEFAULT_PORT,
    preset_epr_path: "",
    audio_preset_epr_path: "",
    delete_after_upload_default: true,
    export_audio_no_music_default: true,
    auto_proxy_non_h264_default: false,
  };
```

- [ ] **Step 2: Add form fields**

In `index.html`, next to the existing `setting-parent-folder-id` input, add two text inputs with ids `setting-lan-base-url` (label: "LAN base URL (empty = Drive only)") and `setting-lan-token` (label: "LAN token"), copying the exact markup pattern of the parent-folder field. (`lan_probe_timeout_ms` stays JSON-only — no UI field, YAGNI.)

In `main.js`: mirror the `settingParentFolderId` pattern — `var settingLanBaseUrl = document.getElementById("setting-lan-base-url");` etc.; populate them in the same place `loadSettings()` values are pushed into the form (find with `grep -n "settingParentFolderId" premiere-extension/tiktok-reproducer/client/main.js` — every hit is a wiring point to mirror: populate, read in `readSettingsForm()`, any trim/validation). In `readSettingsForm()`, read `lan_base_url` with `String(...).trim().replace(/\/+$/, "")` (strip trailing slash).

- [ ] **Step 3: Extend `buildDrivePayloadBase`** (main.js ~line 2226):

```js
  function buildDrivePayloadBase() {
    return {
      settings: {
        client_id: settings.client_id,
        client_secret: settings.client_secret,
        refresh_token: settings.refresh_token,
        parent_folder_id: settings.parent_folder_id,
        lan_base_url: settings.lan_base_url || "",
        lan_token: settings.lan_token || "",
        lan_probe_timeout_ms: Number(settings.lan_probe_timeout_ms || 2500),
      },
      app_data_path: APPDATA,
    };
  }
```

- [ ] **Step 4: Verify manually**

Reload the panel in Premiere (or open `index.html` markup review if PC2 unavailable): fields render, save/reload round-trips values into `settings.json`. On PC1, `node -e "..."` cannot exercise this — a visual check on PC2 later (Task 11) is the true gate; for now `node --check premiere-extension/tiktok-reproducer/client/main.js` must pass.

- [ ] **Step 5: Commit**

```bash
git add premiere-extension/tiktok-reproducer/client/main.js premiere-extension/tiktok-reproducer/client/index.html
git commit -m "feat(cep): LAN transfer settings fields"
```

---

### Task 9: CEP `lan_tasks.js` engine

**Files:**
- Create: `premiere-extension/tiktok-reproducer/client/lan_tasks.js`
- Create: `scripts/lan_tasks_smoke.js` (dev-only smoke runner)

**Interfaces:**
- Consumes: backend endpoints from Tasks 1–3; `subtitle_archive.js` (`expandSubtitleArchiveSync({archivePath, destinationDir, ...})` — verify exact call shape with `grep -n "expandSubtitleArchiveSync" premiere-extension/tiktok-reproducer/client/drive_tasks.js` and mirror it); `download_progress.js` (`createProgressState`, `buildSummaryEvent`) — mirror `drive_tasks.js` usage.
- Produces: `module.exports = { probe: probe, runTask: runTask }` where:
  - `probe(settings) -> Promise<{ok, api_version}>` — GET `<lan_base_url>/api/lan/ping`, timeout `lan_probe_timeout_ms`, rejects on non-200/timeout/version≠1.
  - `runTask("downloadProject", payload, emitProgress)` — resolves the same shape as the Drive engine: `{project_id, drive_folder_id, drive_folder_name, local_root, output_path, used_fallback_root, download_elapsed_ms, download_avg_mb_per_sec, download_file_count, download_total_bytes, subtitle_archive_extracted, orchestration_metrics, transfer_mode: "lan"}`, writes the same `.atr_project_context.json`, uses the same `pickTargetBasePaths` + `.partial` + finalize flow (import `pickTargetBasePaths` from `drive_tasks.js` — it's already exported).
  - `runTask("uploadOutput", payload, emitProgress)` — streams `payload.output_path` to `POST /api/lan/projects/<project_id>/outputs/<payload.output_file_name>`, emitting `{stage: "upload_progress", uploaded_bytes, total_bytes}` (same event shape main.js already consumes), resolves `{ok: true, transfer_mode: "lan", file_name: payload.output_file_name}`.

- [ ] **Step 1: Write the module**

Full file (ES5, Node `http`, per-file 3 retries, size verification):

```js
/**
 * lan_tasks.js — LAN transfer engine (HTTP to the PC1 FastAPI backend).
 * Same task interface as drive_tasks.js; selected per-job by main.js after
 * a successful probe. Spec: docs/superpowers/specs/2026-07-05-lan-transfer-design.md
 */
var fs = require("fs");
var path = require("path");
var http = require("http");
var https = require("https");
var urlModule = require("url");

var driveTasks = require("./drive_tasks.js");
var subtitleArchive = require("./subtitle_archive");
var downloadProgress = require("./download_progress");

var LAN_API_VERSION = 1;
var FILE_MAX_ATTEMPTS = 3;
var FILE_RETRY_DELAY_MS = 2000;
var DOWNLOAD_CONCURRENCY = 2;
var SUBTITLES_DIRNAME = "subtitles";
var SUBTITLES_ARCHIVE_FILENAME = "atr_subtitles.zip";
var PROJECT_CONTEXT_FILENAME = ".atr_project_context.json";
var OUTPUT_FILENAME = "output.mp4";

function lanRequestOptions(settings, apiPath, method, extraHeaders) {
  var base = String(settings.lan_base_url || "").replace(/\/+$/, "");
  var parsed = urlModule.parse(base + apiPath);
  var headers = { "X-ATR-LAN-Token": String(settings.lan_token || "") };
  Object.keys(extraHeaders || {}).forEach(function (key) {
    headers[key] = extraHeaders[key];
  });
  return {
    transport: parsed.protocol === "https:" ? https : http,
    options: {
      protocol: parsed.protocol,
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.path,
      method: method || "GET",
      headers: headers,
    },
  };
}

function requestJson(settings, apiPath, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var built = lanRequestOptions(settings, apiPath, "GET");
    var req = built.transport.request(built.options, function (res) {
      var chunks = [];
      res.on("data", function (c) { chunks.push(c); });
      res.on("end", function () {
        var body = Buffer.concat(chunks).toString("utf8");
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error("LAN HTTP " + res.statusCode + " on " + apiPath + ": " + body.slice(0, 200)));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (e) {
          reject(new Error("LAN invalid JSON on " + apiPath));
        }
      });
    });
    req.on("error", reject);
    if (timeoutMs) {
      req.setTimeout(timeoutMs, function () {
        req.destroy(new Error("LAN request timed out after " + timeoutMs + "ms"));
      });
    }
    req.end();
  });
}

function probe(settings) {
  if (!settings || !settings.lan_base_url) {
    return Promise.reject(new Error("lan_base_url not configured"));
  }
  var timeoutMs = Number(settings.lan_probe_timeout_ms || 2500);
  return requestJson(settings, "/api/lan/ping", timeoutMs).then(function (body) {
    if (!body || body.ok !== true) {
      throw new Error("LAN ping returned unexpected body");
    }
    if (Number(body.api_version) !== LAN_API_VERSION) {
      throw new Error("LAN api_version mismatch: got " + body.api_version + ", need " + LAN_API_VERSION);
    }
    return body;
  });
}

function ensureDir(dirPath) {
  if (!fs.existsSync(dirPath)) {
    fs.mkdirSync(dirPath, { recursive: true });
  }
}

function delay(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

function downloadOneFile(settings, projectId, file, destination, onBytes) {
  ensureDir(path.dirname(destination));
  var apiPath =
    "/api/lan/projects/" + encodeURIComponent(projectId) + "/files/" +
    file.relative_path.split("/").map(encodeURIComponent).join("/");
  return new Promise(function (resolve, reject) {
    var built = lanRequestOptions(settings, apiPath, "GET");
    var req = built.transport.request(built.options, function (res) {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error("LAN HTTP " + res.statusCode + " downloading " + file.relative_path));
        return;
      }
      var out = fs.createWriteStream(destination);
      var received = 0;
      res.on("data", function (chunk) {
        received += chunk.length;
        onBytes(chunk.length);
      });
      res.pipe(out);
      out.on("finish", function () {
        if (Number(file.size) >= 0 && received !== Number(file.size)) {
          reject(new Error(
            "Size mismatch for " + file.relative_path + ": expected " + file.size + ", got " + received,
          ));
          return;
        }
        resolve(received);
      });
      out.on("error", reject);
      res.on("error", reject);
    });
    req.on("error", reject);
    req.end();
  });
}

function downloadFileWithRetries(settings, projectId, file, destination, onBytes) {
  var attempt = 0;
  function tryOnce() {
    attempt += 1;
    var attemptBytes = 0;
    return downloadOneFile(settings, projectId, file, destination, function (n) {
      attemptBytes += n;
      onBytes(n);
    }).catch(function (err) {
      onBytes(-attemptBytes); // roll back this attempt's progress contribution
      try { fs.unlinkSync(destination); } catch (e) {}
      if (attempt >= FILE_MAX_ATTEMPTS) {
        throw err;
      }
      return delay(FILE_RETRY_DELAY_MS * attempt).then(tryOnce);
    });
  }
  return tryOnce();
}

function runWithConcurrency(items, limit, workerFn) {
  return new Promise(function (resolve, reject) {
    var nextIndex = 0;
    var active = 0;
    var failed = false;
    function launch() {
      if (failed) { return; }
      if (nextIndex >= items.length && active === 0) { resolve(); return; }
      while (active < limit && nextIndex < items.length) {
        var item = items[nextIndex];
        nextIndex += 1;
        active += 1;
        workerFn(item).then(function () {
          active -= 1;
          launch();
        }, function (err) {
          if (!failed) { failed = true; reject(err); }
        });
      }
    }
    launch();
  });
}

function writeJsonAtomic(filePath, data) {
  var tmpPath = filePath + ".tmp";
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2));
  fs.renameSync(tmpPath, filePath);
}

function extractSubtitles(targetRoot, projectId, emitProgress) {
  var archivePath = path.join(targetRoot, SUBTITLES_DIRNAME, SUBTITLES_ARCHIVE_FILENAME);
  if (!fs.existsSync(archivePath)) {
    return { extracted: false };
  }
  emitProgress({ stage: "subtitle_archive_extract_start", project_id: projectId });
  var extraction = subtitleArchive.expandSubtitleArchiveSync({
    archivePath: archivePath,
    destinationDir: path.join(targetRoot, SUBTITLES_DIRNAME),
  }); // ! mirror the exact argument object used in drive_tasks.js
  emitProgress({ stage: "subtitle_archive_extract_complete", project_id: projectId });
  return extraction || { extracted: true };
}

function performDownloadProject(payload, emitProgress) {
  var settings = payload.settings || {};
  var projectId = payload.project_id;
  if (!projectId) {
    return Promise.reject(new Error("Missing project_id"));
  }
  emitProgress({ stage: "resolve_folder", project_id: projectId, transfer_mode: "lan" });
  return requestJson(
    settings,
    "/api/lan/projects/" + encodeURIComponent(projectId) + "/manifest",
    30000,
  ).then(function (manifest) {
    var files = manifest.files || [];
    var folderName = String(manifest.folder_name || "project_" + projectId);
    var target = driveTasks.pickTargetBasePaths(folderName, payload.app_data_path);
    var targetRoot = path.join(target.parent, target.folderName);
    var partialRoot = targetRoot + ".partial";
    ensureDir(partialRoot);

    var totalBytes = 0;
    files.forEach(function (f) { totalBytes += Number(f.size || 0); });
    var downloadedBytes = 0;
    var progressState = downloadProgress.createProgressState();
    var startedAt = Date.now();

    emitProgress({
      stage: "download_start",
      project_id: projectId,
      file_count: files.length,
      total_bytes: totalBytes,
      target_root: targetRoot,
      transfer_mode: "lan",
    });

    function onBytes(n) {
      downloadedBytes += n;
      var summary = downloadProgress.buildSummaryEvent(progressState, {
        project_id: projectId,
        file_count: files.length,
        downloaded_bytes: downloadedBytes,
        total_bytes: totalBytes,
      });
      if (summary) { emitProgress(summary); }
    }

    return runWithConcurrency(files, DOWNLOAD_CONCURRENCY, function (file) {
      var destination = path.join(partialRoot, file.relative_path);
      return downloadFileWithRetries(settings, projectId, file, destination, onBytes);
    }).then(function () {
      fs.renameSync(partialRoot, targetRoot);
      var elapsedMs = Math.max(1, Date.now() - startedAt);
      var extraction = extractSubtitles(targetRoot, projectId, emitProgress);
      var avgMbPerSec = totalBytes > 0 ? totalBytes / (1024 * 1024) / (elapsedMs / 1000) : 0;
      var outputPath = path.join(targetRoot, OUTPUT_FILENAME);
      writeJsonAtomic(path.join(targetRoot, PROJECT_CONTEXT_FILENAME), {
        project_id: projectId,
        drive_folder_id: manifest.drive_folder_id || null,
        local_root: targetRoot,
        output_path: outputPath,
        downloaded_at: new Date().toISOString(),
        download_elapsed_ms: elapsedMs,
        download_avg_mb_per_sec: avgMbPerSec,
        download_file_count: files.length,
        subtitle_archive_extracted: !!(extraction && extraction.extracted),
        transfer_mode: "lan",
      });
      emitProgress({
        stage: "download_complete",
        project_id: projectId,
        target_root: targetRoot,
        output_path: outputPath,
        elapsed_ms: elapsedMs,
        avg_mb_per_sec: avgMbPerSec,
        file_count: files.length,
        total_bytes: totalBytes,
        transfer_mode: "lan",
      });
      return {
        project_id: projectId,
        drive_folder_id: manifest.drive_folder_id || null,
        drive_folder_name: folderName,
        local_root: targetRoot,
        output_path: outputPath,
        used_fallback_root: !!target.isFallback,
        download_elapsed_ms: elapsedMs,
        download_avg_mb_per_sec: avgMbPerSec,
        download_file_count: files.length,
        download_total_bytes: totalBytes,
        subtitle_archive_extracted: !!(extraction && extraction.extracted),
        orchestration_metrics: null,
        transfer_mode: "lan",
      };
    });
  });
}

function performUploadOutput(payload, emitProgress) {
  var settings = payload.settings || {};
  var projectId = payload.project_id;
  var outputPath = String(payload.output_path || "");
  var fileName = String(payload.output_file_name || path.basename(outputPath));
  if (!projectId || !outputPath) {
    return Promise.reject(new Error("Missing project_id or output_path"));
  }
  if (!fs.existsSync(outputPath)) {
    return Promise.reject(new Error("Output file not found: " + outputPath));
  }
  var totalBytes = fs.statSync(outputPath).size;
  var attempt = 0;

  function tryOnce() {
    attempt += 1;
    return new Promise(function (resolve, reject) {
      var apiPath =
        "/api/lan/projects/" + encodeURIComponent(projectId) +
        "/outputs/" + encodeURIComponent(fileName);
      var built = lanRequestOptions(settings, apiPath, "POST", {
        "Content-Type": "application/octet-stream",
        "Content-Length": totalBytes,
      });
      var req = built.transport.request(built.options, function (res) {
        var chunks = [];
        res.on("data", function (c) { chunks.push(c); });
        res.on("end", function () {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            reject(new Error("LAN upload HTTP " + res.statusCode + ": " + Buffer.concat(chunks).toString("utf8").slice(0, 200)));
            return;
          }
          resolve({ ok: true, transfer_mode: "lan", file_name: fileName });
        });
      });
      req.on("error", reject);
      var uploaded = 0;
      var source = fs.createReadStream(outputPath);
      source.on("data", function (chunk) {
        uploaded += chunk.length;
        emitProgress({ stage: "upload_progress", uploaded_bytes: uploaded, total_bytes: totalBytes });
      });
      source.on("error", reject);
      source.pipe(req);
    }).catch(function (err) {
      if (attempt >= FILE_MAX_ATTEMPTS) { throw err; }
      return delay(FILE_RETRY_DELAY_MS * attempt).then(tryOnce);
    });
  }

  emitProgress({ stage: "upload_start", project_id: projectId, total_bytes: totalBytes, transfer_mode: "lan" });
  return tryOnce();
}

function runTask(task, payload, emitProgress) {
  var reporter = typeof emitProgress === "function" ? emitProgress : function () {};
  var safePayload = payload || {};
  if (task === "downloadProject") {
    return performDownloadProject(safePayload, reporter);
  }
  if (task === "uploadOutput") {
    return performUploadOutput(safePayload, reporter);
  }
  return Promise.reject(new Error("Unknown LAN task: " + task));
}

module.exports = {
  probe: probe,
  runTask: runTask,
};
```

Before finalizing: (a) mirror the exact `expandSubtitleArchiveSync` argument object from `drive_tasks.js` (line ~104); (b) mirror the exact finalize step — `drive_tasks.js` uses `finalizeDownloadedFolderWithRetry(partialRoot, targetRoot)`; if that function is exported or trivially copyable (rename with retries for Windows file locks), copy its retry pattern instead of the bare `fs.renameSync` above; (c) `orchestration_metrics` — check what `main.js` does with a `null` value (`grep -n "orchestration_metrics" premiere-extension/tiktok-reproducer/client/main.js`) and if it requires an object, reuse `buildPhaseMetricsPatch`-equivalent minimal shape.

- [ ] **Step 2: Syntax check** — `node --check premiere-extension/tiktok-reproducer/client/lan_tasks.js`. Expected: silent success.

- [ ] **Step 3: Write the smoke runner**

```js
// scripts/lan_tasks_smoke.js — dev-only: exercise lan_tasks against a running backend.
// Usage: ATR_LAN_TOKEN=... node scripts/lan_tasks_smoke.js <base_url> <project_id> [<file_to_upload>]
var lanTasks = require("../premiere-extension/tiktok-reproducer/client/lan_tasks.js");

var baseUrl = process.argv[2];
var projectId = process.argv[3];
var uploadFile = process.argv[4];
var settings = {
  lan_base_url: baseUrl,
  lan_token: process.env.ATR_LAN_TOKEN || "",
  lan_probe_timeout_ms: 2500,
};

lanTasks
  .probe(settings)
  .then(function (ping) {
    console.log("probe OK:", JSON.stringify(ping));
    if (!projectId) { return null; }
    if (uploadFile) {
      return lanTasks.runTask(
        "uploadOutput",
        { settings: settings, project_id: projectId, output_path: uploadFile },
        function (p) { if (p.stage !== "upload_progress") { console.log(p.stage); } },
      );
    }
    return lanTasks.runTask(
      "downloadProject",
      { settings: settings, project_id: projectId, app_data_path: "/tmp/atr-lan-smoke" },
      function (p) { console.log(p.stage || "progress", p.downloaded_bytes || ""); },
    );
  })
  .then(function (result) { console.log("RESULT:", JSON.stringify(result, null, 2)); })
  .catch(function (err) { console.error("FAILED:", err.message); process.exit(1); });
```

Note: `pickTargetBasePaths` may assume a Windows layout; if the smoke download fails on Linux because of it, pass an `app_data_path` that exists and inspect where files land — smoke goal is transport correctness, not path semantics.

- [ ] **Step 4: Run the smoke test on PC1** (backend running, `ATR_LAN_TRANSFER_TOKEN` set in `.env`, backend restarted):

```bash
ATR_LAN_TOKEN=<token> node scripts/lan_tasks_smoke.js http://127.0.0.1:8000
ATR_LAN_TOKEN=<token> node scripts/lan_tasks_smoke.js http://127.0.0.1:8000 <real_project_id>
```

Expected: `probe OK: {"ok":true,"api_version":1}`; download completes with all manifest files on disk and a final RESULT JSON. Then upload a small file: `ATR_LAN_TOKEN=<token> node scripts/lan_tasks_smoke.js http://127.0.0.1:8000 <pid> /path/to/small/output.mp4` — expect the file to appear in `backend/data/projects/<pid>/output/` and a relay status JSON entry.

- [ ] **Step 5: Commit**

```bash
git add premiere-extension/tiktok-reproducer/client/lan_tasks.js scripts/lan_tasks_smoke.js
git commit -m "feat(cep): lan_tasks.js LAN transfer engine + smoke runner"
```

---

### Task 10: main.js engine selection (`runTransferTask`)

**Files:**
- Modify: `premiere-extension/tiktok-reproducer/client/main.js`

**Interfaces:**
- Consumes: `lanTasks.probe(settings)`, `lanTasks.runTask(...)` (Task 9); existing `runDriveTask(taskName, payload, onProgress, options)`.
- Produces: `runTransferTask(taskName, payload, onProgress, options)` — the single entry point both job types call; per-job probe; Drive fallback on any probe failure.

- [ ] **Step 1: Add the selector** (place right after `runDriveTask`'s definition, ~line 3300):

```js
  var lanTasks = null;

  function runTransferTask(taskName, payload, onProgress, options) {
    var lanBaseUrl =
      payload && payload.settings ? String(payload.settings.lan_base_url || "") : "";
    if (!lanBaseUrl) {
      return runDriveTask(taskName, payload, onProgress, options);
    }
    if (!lanTasks) {
      lanTasks = require(getClientFilePath("lan_tasks.js"));
    }
    return lanTasks.probe(payload.settings).then(
      function () {
        log("LAN mode selected for " + taskName, "info");
        return lanTasks.runTask(taskName, payload, onProgress).catch(function (err) {
          log("LAN task failed: " + err.message, "error");
          throw err; // clean failure — re-run re-probes (spec: no silent mid-job engine switch)
        });
      },
      function (probeErr) {
        log(
          "LAN probe failed (" + probeErr.message + "), falling back to Drive for " + taskName,
          "warn",
        );
        return runDriveTask(taskName, payload, onProgress, options);
      },
    );
  }
```

- [ ] **Step 2: Switch the two call sites**

1. Download: line ~3354, `runDriveTask("downloadProject", downloadPayload, ...)` → `runTransferTask("downloadProject", downloadPayload, ...)` (same args).
2. Upload: line ~3735, `runDriveTask("uploadOutput", uploadPayload, ...)` → `runTransferTask("uploadOutput", uploadPayload, ...)`.

- [ ] **Step 3: Relax the Drive-configured guards**

`grep -n "isDriveConfigured()" premiere-extension/tiktok-reproducer/client/main.js` — for each guard that aborts a download/upload job (e.g. line ~3694 `if (!isDriveConfigured()) { return Promise.reject(new Error("Drive settings are incomplete")); }`), change to:

```js
    if (!isDriveConfigured() && !String(settings.lan_base_url || "")) {
      return Promise.reject(new Error("Neither Drive nor LAN transfer is configured"));
    }
```

Inspect each hit individually — guards protecting Drive-only actions (e.g. `testConnection`, cleanup of Drive folders) must stay untouched.

- [ ] **Step 4: Syntax check** — `node --check premiere-extension/tiktok-reproducer/client/main.js`. Expected: silent success.

- [ ] **Step 5: Commit**

```bash
git add premiere-extension/tiktok-reproducer/client/main.js
git commit -m "feat(cep): per-job LAN/Drive engine selection with probe + fallback"
```

---

### Task 11: Deployment, docs, and end-to-end validation

**Files:**
- Modify: `pixi.toml` (backend task bind address)
- Modify: `premiere-extension/README.md` (LAN settings documentation)
- Modify: `.env` on PC1 (NOT committed — verify it's gitignored)

**Interfaces:** none new — this task turns the feature on and proves the spec's three E2E scenarios.

- [ ] **Step 1: Bind the backend to the LAN**

In `pixi.toml`, change the backend task: `--host 127.0.0.1` → `--host 0.0.0.0`. (ufw already restricts port 8000 to `192.168.1.0/24` — rule added during Milestone 0.)

- [ ] **Step 2: Generate and set the token**

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add `ATR_LAN_TRANSFER_TOKEN=<value>` to the repo-root `.env` (confirm `.env` is in `.gitignore` first: `grep -n "^\.env" .gitignore`). Restart the backend. Verify: `curl -s -H "X-ATR-LAN-Token: <value>" http://127.0.0.1:8000/api/lan/ping` → `{"ok":true,"api_version":1}` and `curl -s http://192.168.1.76:8000/api/lan/ping` → 401 (reachable but guarded).

- [ ] **Step 3: Document the CEP settings**

Add a "LAN transfer" section to `premiere-extension/README.md`: the two panel fields, prod values (`http://arch-sid.local:8000` + the token), the empty-URL kill switch, and the fallback behavior.

- [ ] **Step 4: Configure PC2** — reinstall/reload the extension (per `premiere-extension/install_extension.bat` / README), fill `lan_base_url` = `http://arch-sid.local:8000` and `lan_token`, save.

- [ ] **Step 5: Run the spec's three E2E scenarios on a small real project**

1. **LAN mode**: trigger the normal Discord→localhost flow on PC2. Panel log must show "LAN mode selected for downloadProject"; project builds; after render, "LAN mode selected for uploadOutput"; `output.mp4` (+ `output_no_music.wav`) appear in `backend/data/projects/<id>/output/` on PC1; relay status JSON shows `"status": "uploaded"`; the Drive folder contains the video; Project Manager shows the project green **before** the relay finishes (local-first); preview button plays the local `<video>`.
2. **Feature off**: clear `lan_base_url` on PC2, re-run the same flow — panel must behave exactly as before this feature (Drive download/upload, no probe lines in the log).
3. **Fallback**: restore `lan_base_url`, then on PC1 `sudo ufw deny from 192.168.1.0/24 to any port 8000 proto tcp` (temporarily reorder/remove the allow rule), re-run — panel must log the probe failure and complete the whole job via Drive. Restore the allow rule afterwards.

- [ ] **Step 6: Full regression** — `pytest backend/tests -q` and frontend `npx tsc --noEmit` + e2e suite. Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add pixi.toml premiere-extension/README.md
git commit -m "feat(lan): bind backend to LAN, document CEP LAN settings"
```

---

## Self-Review Notes

- Spec coverage: ping/manifest/files/outputs endpoints (T1–T3), relay + ensure-upsert (T4, T6), local-first readiness/preview/copyright/upload-source (T5–T7), CEP settings/engine/selection (T8–T10), deployment + 3-scenario E2E (T11). Milestone 0 already passed (spec).
- The spec's "version skew → Drive fallback" is implemented in `probe()` (api_version check) + `runTransferTask` fallback path.
- The spec's "uvicorn --reload only watches .py" assumption is re-verified implicitly in T11 Step 5 scenario 1 (backend must not restart when outputs land).
- Deliberately NOT done (YAGNI): manifest caching, LAN transfer for `ATR_*` proxies, token on non-LAN routes, upload resume (retry restarts the file — acceptable at 100MB/87Mbps ≈ 10s per retry).
