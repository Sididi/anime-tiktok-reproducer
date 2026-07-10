# Instant Upload Duration Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the upload "Checking" phase near-instant by reading the final video's duration from Drive metadata instead of downloading it, and feed the duration-choice modal previews from a single shared background download.

**Architecture:** `UploadPhaseService._check_platform_duration` stops copying/downloading the video; it probes a LAN-local file in place, else asks Drive for `videoMediaMetadata.durationMillis`, else falls back to a blocking download into a new shared per-project source cache. When a check returns `needed=true`, a background thread warms that cache. Two new routes (`upload-source-status`, `upload-source-preview`) replace the per-platform preview routes; both duration modals poll status and play the cached original with `playbackRate` (unchanged mechanism). The upload job reuses the cached file instead of a third Drive download.

**Tech Stack:** FastAPI, googleapiclient (Drive v3), threading, pytest (backend, run via pixi); React + TypeScript (frontend, vite/tsc).

**Spec:** `docs/superpowers/specs/2026-07-10-instant-duration-check-design.md`

## Global Constraints

- Check response shape is unchanged: `{needed, duration_seconds, speed_factor, sped_up_available}` (plus neutral variant).
- `sped_up.mp4` handling at upload time is untouched.
- Shared cache max age: 7200 s (2 h), same as existing prep caches.
- Preview endpoint returns HTTP 202 while the download is in flight.
- French UI copy in modals: loading = `Préparation de l'aperçu...`, failure = `Aperçu indisponible`.
- Backend tests run from `backend/`: `cd backend && pixi run pytest tests/<file> -v`.
- Frontend check: `cd frontend && npm run build` (tsc -b && vite build; use fnm if node missing).

---

### Task 1: Drive video-duration metadata helper

**Files:**
- Modify: `backend/app/services/google_drive_service.py` (add method near `download_file`, ~line 876)
- Test: `backend/tests/test_drive_video_duration.py` (create)

**Interfaces:**
- Consumes: existing `GoogleDriveService._client()`.
- Produces: `GoogleDriveService.get_video_duration_seconds(file_id: str) -> float | None` — duration in seconds from Drive metadata; `None` when metadata is absent or the API call fails.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_drive_video_duration.py
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.google_drive_service import GoogleDriveService


class _FakeDrive:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def files(self):
        return self

    def get(self, **kwargs):
        assert kwargs["fields"] == "videoMediaMetadata(durationMillis)"
        assert kwargs["supportsAllDrives"] is True
        if self._error:
            raise self._error
        return SimpleNamespace(execute=lambda: self._response)


def _patch_client(monkeypatch, drive):
    monkeypatch.setattr(
        GoogleDriveService, "_client", classmethod(lambda cls: drive)
    )


def test_duration_from_metadata(monkeypatch):
    _patch_client(
        monkeypatch,
        _FakeDrive({"videoMediaMetadata": {"durationMillis": "95500"}}),
    )
    assert GoogleDriveService.get_video_duration_seconds("f1") == 95.5


def test_missing_metadata_returns_none(monkeypatch):
    _patch_client(monkeypatch, _FakeDrive({}))
    assert GoogleDriveService.get_video_duration_seconds("f1") is None


def test_unparsable_duration_returns_none(monkeypatch):
    _patch_client(
        monkeypatch,
        _FakeDrive({"videoMediaMetadata": {"durationMillis": "abc"}}),
    )
    assert GoogleDriveService.get_video_duration_seconds("f1") is None


def test_api_error_returns_none(monkeypatch):
    _patch_client(monkeypatch, _FakeDrive(error=RuntimeError("boom")))
    assert GoogleDriveService.get_video_duration_seconds("f1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pixi run pytest tests/test_drive_video_duration.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'get_video_duration_seconds'`

- [ ] **Step 3: Implement the method**

In `backend/app/services/google_drive_service.py`, directly above `def download_file` (~line 875):

```python
    @classmethod
    def get_video_duration_seconds(cls, file_id: str) -> float | None:
        """Video duration from Drive metadata, or None when Drive has not
        processed the file yet (callers fall back to download+probe)."""
        try:
            drive = cls._client()
            info = drive.files().get(
                fileId=file_id,
                fields="videoMediaMetadata(durationMillis)",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            logger.warning(
                "Drive video metadata lookup failed: file_id=%s error=%s", file_id, exc
            )
            return None
        metadata = info.get("videoMediaMetadata") or {}
        duration_millis = metadata.get("durationMillis")
        if duration_millis is None:
            return None
        try:
            return float(duration_millis) / 1000.0
        except (TypeError, ValueError):
            return None
```

Note: check the module already has a `logger`; if it doesn't, add `logger = logging.getLogger(__name__)` following the file's existing imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pixi run pytest tests/test_drive_video_duration.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/google_drive_service.py backend/tests/test_drive_video_duration.py
git commit -m "feat(drive): read video duration from Drive metadata"
```

---

### Task 2: Shared source-video cache with background warm

**Files:**
- Modify: `backend/app/services/upload_phase.py` (constants ~line 1199, helpers near `_copyright_audio_dir` ~line 1251, cleanup near `cleanup_stale_copyright_audio` ~line 1298)
- Test: `backend/tests/test_upload_source_cache.py` (create)

**Interfaces:**
- Consumes: `GoogleDriveService.download_file(file_id, destination)`, `UploadReadiness` dataclass, `ProjectService.load`, `cls.compute_readiness`, `cls._cleanup_stale_prep_cache(cache_dir, max_age)`.
- Produces (all on `UploadPhaseService`):
  - `cached_source_video(project_id: str) -> Path | None` — cached final video if fully downloaded.
  - `_ensure_source_video(project_id: str, readiness: UploadReadiness) -> Path` — blocking copy/download, per-project lock, `.part` + atomic rename.
  - `start_source_video_download(project_id: str, readiness: UploadReadiness | None = None) -> dict` — non-blocking warm; returns `{"state": "ready"|"in_progress"|"error", ...}`; loads project/readiness itself when not given (raises `ValueError` if project missing).
  - `source_video_status(project_id: str) -> dict` — `{"state": "ready"|"in_progress"|"error"|"missing"}`, `detail` present on error.
  - `cleanup_stale_source_cache() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_upload_source_cache.py
from __future__ import annotations

import sys
import time
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


@pytest.fixture
def source_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "_SOURCE_CACHE_DIR", tmp_path / "upload_source"
    )
    # isolate cross-test state
    monkeypatch.setattr(UploadPhaseService, "_source_download_errors", {})
    monkeypatch.setattr(UploadPhaseService, "_source_downloads_in_flight", set())
    monkeypatch.setattr(UploadPhaseService, "_source_locks", {})
    return tmp_path / "upload_source"


def test_cached_source_video_none_when_empty(source_cache):
    assert UploadPhaseService.cached_source_video("p1") is None


def test_ensure_source_video_copies_local(source_cache, tmp_path):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"local-bytes")
    readiness = _readiness(
        local_video_path=str(video), local_video_name="output.mp4"
    )
    result = UploadPhaseService._ensure_source_video("p1", readiness)
    assert result.read_bytes() == b"local-bytes"
    assert result.name == "output.mp4"
    assert UploadPhaseService.cached_source_video("p1") == result


def test_ensure_source_video_downloads_from_drive(source_cache, monkeypatch):
    import app.services.upload_phase as up

    def fake_download(cls, file_id, destination):
        assert file_id == "d1"
        assert destination.name.endswith(".part")
        destination.write_bytes(b"drive-bytes")

    monkeypatch.setattr(
        up.GoogleDriveService, "download_file", classmethod(fake_download)
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    result = UploadPhaseService._ensure_source_video("p1", readiness)
    assert result.read_bytes() == b"drive-bytes"
    assert result.name == "final.mp4"
    # no leftover partial file
    assert list(result.parent.glob("*.part")) == []


def test_ensure_source_video_reuses_cache(source_cache, monkeypatch):
    import app.services.upload_phase as up

    calls = []
    monkeypatch.setattr(
        up.GoogleDriveService,
        "download_file",
        classmethod(lambda cls, fid, dest: calls.append(fid) or dest.write_bytes(b"x")),
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    UploadPhaseService._ensure_source_video("p1", readiness)
    UploadPhaseService._ensure_source_video("p1", readiness)
    assert calls == ["d1"]


def test_partial_download_is_not_ready(source_cache):
    partial_dir = source_cache / "p1"
    partial_dir.mkdir(parents=True)
    (partial_dir / "final.mp4.part").write_bytes(b"incomplete")
    assert UploadPhaseService.cached_source_video("p1") is None
    assert UploadPhaseService.source_video_status("p1")["state"] == "missing"


def test_status_ready_when_cached(source_cache):
    cache_dir = source_cache / "p1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "final.mp4").write_bytes(b"x")
    assert UploadPhaseService.source_video_status("p1")["state"] == "ready"


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_start_download_background_success(source_cache, monkeypatch):
    import app.services.upload_phase as up

    monkeypatch.setattr(
        up.GoogleDriveService,
        "download_file",
        classmethod(lambda cls, fid, dest: dest.write_bytes(b"bg")),
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    status = UploadPhaseService.start_source_video_download("p1", readiness)
    assert status["state"] in ("in_progress", "ready")
    assert _wait_until(
        lambda: UploadPhaseService.source_video_status("p1")["state"] == "ready"
    )


def test_start_download_background_error(source_cache, monkeypatch):
    import app.services.upload_phase as up

    def boom(cls, fid, dest):
        raise RuntimeError("drive down")

    monkeypatch.setattr(up.GoogleDriveService, "download_file", classmethod(boom))
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    UploadPhaseService.start_source_video_download("p1", readiness)
    assert _wait_until(
        lambda: UploadPhaseService.source_video_status("p1")["state"] == "error"
    )
    assert "drive down" in UploadPhaseService.source_video_status("p1")["detail"]


def test_start_download_short_circuits_when_ready(source_cache):
    cache_dir = source_cache / "p1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "final.mp4").write_bytes(b"x")
    status = UploadPhaseService.start_source_video_download("p1", _readiness())
    assert status["state"] == "ready"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pixi run pytest tests/test_upload_source_cache.py -v`
Expected: FAIL — `AttributeError` on `_SOURCE_CACHE_DIR` / missing methods

- [ ] **Step 3: Implement the cache layer**

In `backend/app/services/upload_phase.py`:

a) Ensure `import threading` is present at the top of the file (add if missing; `shutil`, `Path` already exist).

b) Next to the prep-cache constants (~line 1209, after `_COPYRIGHT_AUDIO_MAX_AGE_SECONDS`):

```python
    _SOURCE_CACHE_DIR = settings.cache_dir / "upload_source"
    _SOURCE_CACHE_MAX_AGE_SECONDS = 7200  # 2 hours

    # Shared final-video preview cache bookkeeping (guarded by _source_download_guard)
    _source_download_guard = threading.Lock()
    _source_downloads_in_flight: set[str] = set()
    _source_download_errors: dict[str, str] = {}
    _source_locks: dict[str, threading.Lock] = {}
```

c) After `_copyright_audio_dir` (~line 1255):

```python
    @classmethod
    def _source_cache_dir(cls, project_id: str) -> Path:
        return cls._SOURCE_CACHE_DIR / project_id

    @classmethod
    def _source_lock(cls, project_id: str) -> threading.Lock:
        with cls._source_download_guard:
            return cls._source_locks.setdefault(project_id, threading.Lock())

    @classmethod
    def cached_source_video(cls, project_id: str) -> Path | None:
        cache_dir = cls._source_cache_dir(project_id)
        if not cache_dir.exists():
            return None
        for f in sorted(cache_dir.iterdir()):
            if f.is_file() and f.suffix.lower() == ".mp4":
                return f
        return None

    @classmethod
    def _ensure_source_video(
        cls, project_id: str, readiness: UploadReadiness
    ) -> Path:
        """Blocking: return the cached final video, materializing it if needed."""
        with cls._source_lock(project_id):
            cached = cls.cached_source_video(project_id)
            if cached is not None:
                return cached

            video_name = (
                readiness.drive_video_name
                or readiness.local_video_name
                or "final_video.mp4"
            )
            cache_dir = cls._source_cache_dir(project_id)
            cache_dir.mkdir(parents=True, exist_ok=True)
            destination = cache_dir / video_name
            partial = cache_dir / f"{video_name}.part"

            try:
                if readiness.local_video_path and Path(readiness.local_video_path).exists():
                    shutil.copy2(readiness.local_video_path, partial)
                elif readiness.drive_video_id:
                    GoogleDriveService.download_file(readiness.drive_video_id, partial)
                else:
                    raise ValueError(
                        "Final video unavailable: not present locally and no Drive copy"
                    )
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
            return destination

    @classmethod
    def start_source_video_download(
        cls, project_id: str, readiness: UploadReadiness | None = None
    ) -> dict[str, Any]:
        """Warm the shared source-video cache in the background."""
        status = cls.source_video_status(project_id)
        if status["state"] in ("ready", "in_progress"):
            return status

        if readiness is None:
            project = ProjectService.load(project_id)
            if not project:
                raise ValueError("Project not found")
            readiness = cls.compute_readiness(project)

        with cls._source_download_guard:
            if project_id in cls._source_downloads_in_flight:
                return {"state": "in_progress"}
            cls._source_downloads_in_flight.add(project_id)
            cls._source_download_errors.pop(project_id, None)

        def _worker() -> None:
            try:
                cls._ensure_source_video(project_id, readiness)
            except Exception as exc:
                logger.warning(
                    "Source video download failed: project_id=%s error=%s",
                    project_id,
                    exc,
                )
                with cls._source_download_guard:
                    cls._source_download_errors[project_id] = str(exc)
            finally:
                with cls._source_download_guard:
                    cls._source_downloads_in_flight.discard(project_id)

        threading.Thread(
            target=_worker, name=f"source-video-{project_id}", daemon=True
        ).start()
        return {"state": "in_progress"}

    @classmethod
    def source_video_status(cls, project_id: str) -> dict[str, Any]:
        if cls.cached_source_video(project_id) is not None:
            return {"state": "ready"}
        with cls._source_download_guard:
            if project_id in cls._source_downloads_in_flight:
                return {"state": "in_progress"}
            error = cls._source_download_errors.get(project_id)
        if error:
            return {"state": "error", "detail": error}
        return {"state": "missing"}
```

d) Next to `cleanup_stale_copyright_audio` (~line 1308):

```python
    @classmethod
    def cleanup_stale_source_cache(cls) -> None:
        cls._cleanup_stale_prep_cache(
            cls._SOURCE_CACHE_DIR, cls._SOURCE_CACHE_MAX_AGE_SECONDS
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pixi run pytest tests/test_upload_source_cache.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/upload_phase.py backend/tests/test_upload_source_cache.py
git commit -m "feat(upload): shared source-video cache with background download"
```

---

### Task 3: Metadata-first duration check

**Files:**
- Modify: `backend/app/services/upload_phase.py` — `_check_platform_duration` (~line 1346), `check_facebook_duration` (~line 1417), `check_youtube_duration` (~line 1436)
- Test: `backend/tests/test_platform_duration_check.py` (create)

**Interfaces:**
- Consumes: Task 1 `GoogleDriveService.get_video_duration_seconds`, Task 2 `_ensure_source_video` / `start_source_video_download` / `cleanup_stale_source_cache`.
- Produces: `_check_platform_duration(project_id, account_id, *, cleanup_stale, is_enabled, probe_media, max_duration, max_speed) -> dict` — **drops the now-dead `platform_label`, `prep_dir`, `transcode_to_limit` parameters**; response shape unchanged. New helper `_resolve_final_video_duration(project_id, readiness, probe_media) -> float`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_platform_duration_check.py
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import app.services.upload_phase as up
from app.services.upload_phase import UploadPhaseService, UploadReadiness


def _readiness(**overrides):
    base = dict(
        status="green", metadata_exists=True, drive_video_count=0,
        drive_video_id="d1", drive_video_name="final.mp4",
        drive_video_web_url=None, reasons=[], drive_folder_id="folder-1",
        drive_folder_url=None, local_video_path=None, local_video_name=None,
    )
    base.update(overrides)
    return UploadReadiness(**base)


@pytest.fixture
def check_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "_SOURCE_CACHE_DIR", tmp_path / "upload_source"
    )
    monkeypatch.setattr(UploadPhaseService, "_source_download_errors", {})
    monkeypatch.setattr(UploadPhaseService, "_source_downloads_in_flight", set())
    monkeypatch.setattr(UploadPhaseService, "_source_locks", {})
    monkeypatch.setattr(
        up.ProjectService, "load",
        classmethod(lambda cls, pid: SimpleNamespace(id=pid)),
    )
    started = []
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: started.append(pid) or {"state": "in_progress"}),
    )
    return started


def _run_check(monkeypatch, readiness, *, probe_media=None, max_duration=90.0, max_speed=1.4):
    monkeypatch.setattr(
        UploadPhaseService, "compute_readiness",
        classmethod(lambda cls, project: readiness),
    )
    return UploadPhaseService._check_platform_duration(
        "p1", None,
        cleanup_stale=lambda: None,
        is_enabled=lambda account_id: True,
        probe_media=probe_media or (lambda **kw: (None, "no probe expected")),
        max_duration=max_duration,
        max_speed=max_speed,
    )


def test_under_limit_via_drive_metadata_no_download(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: 80.0),
    )
    result = _run_check(monkeypatch, _readiness())
    assert result == {
        "needed": False, "duration_seconds": 80.0,
        "speed_factor": 1.0, "sped_up_available": False,
    }
    assert check_env == []  # no background download for short videos


def test_over_limit_via_drive_metadata_triggers_background_download(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: 117.0),
    )
    result = _run_check(monkeypatch, _readiness())
    assert result["needed"] is True
    assert result["duration_seconds"] == 117.0
    assert result["speed_factor"] == 1.3
    assert result["sped_up_available"] is True
    assert check_env == ["p1"]


def test_local_video_probed_in_place(check_env, tmp_path, monkeypatch):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"v")
    probed = []

    def probe(video_path):
        probed.append(video_path)
        return SimpleNamespace(duration_seconds=200.0), None

    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: pytest.fail("should not hit Drive")),
    )
    readiness = _readiness(
        local_video_path=str(video), local_video_name="output.mp4"
    )
    result = _run_check(monkeypatch, readiness, probe_media=probe, max_duration=180.0)
    assert probed == [video]
    assert result["needed"] is True


def test_missing_metadata_falls_back_to_download_probe(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: None),
    )
    ensured = []

    def fake_ensure(cls, project_id, readiness):
        ensured.append(project_id)
        path = cls._source_cache_dir(project_id) / "final.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(
        UploadPhaseService, "_ensure_source_video", classmethod(fake_ensure)
    )

    def probe(video_path):
        return SimpleNamespace(duration_seconds=100.0), None

    result = _run_check(monkeypatch, _readiness(), probe_media=probe)
    assert ensured == ["p1"]
    assert result["needed"] is True
    assert result["duration_seconds"] == 100.0


def test_unprobeable_fallback_raises(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService, "_ensure_source_video",
        classmethod(lambda cls, pid, r: Path("/nonexistent/final.mp4")),
    )
    with pytest.raises(ValueError, match="Unable to probe video duration"):
        _run_check(monkeypatch, _readiness(), probe_media=lambda **kw: (None, "bad file"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pixi run pytest tests/test_platform_duration_check.py -v`
Expected: FAIL — `TypeError: _check_platform_duration() got an unexpected keyword argument` (old signature requires `platform_label`, `prep_dir`, `transcode_to_limit`)

- [ ] **Step 3: Rework the check**

Replace `_check_platform_duration` (currently ~lines 1345–1414 of `backend/app/services/upload_phase.py`) with:

```python
    @classmethod
    def _resolve_final_video_duration(
        cls,
        project_id: str,
        readiness: UploadReadiness,
        probe_media: Callable[..., Any],
    ) -> float:
        """Duration of the final video without downloading it when possible."""
        if readiness.local_video_path:
            local = Path(readiness.local_video_path)
            if local.exists():
                probe, probe_error = probe_media(video_path=local)
                if (
                    not probe_error
                    and probe is not None
                    and probe.duration_seconds is not None
                ):
                    return probe.duration_seconds

        if readiness.drive_video_id:
            duration = GoogleDriveService.get_video_duration_seconds(
                readiness.drive_video_id
            )
            if duration is not None:
                return duration

        # Drive has not exposed video metadata yet: single blocking download
        # into the shared cache (also feeds the preview modals and the upload).
        source_path = cls._ensure_source_video(project_id, readiness)
        probe, probe_error = probe_media(video_path=source_path)
        if probe_error or probe is None or probe.duration_seconds is None:
            raise ValueError(
                f"Unable to probe video duration: {probe_error or 'unknown'}"
            )
        return probe.duration_seconds

    @classmethod
    def _check_platform_duration(
        cls,
        project_id: str,
        account_id: str | None,
        *,
        cleanup_stale: Callable[[], None],
        is_enabled: Callable[[str | None], bool],
        probe_media: Callable[..., Any],
        max_duration: float,
        max_speed: float,
    ) -> dict[str, Any]:
        cleanup_stale()
        cls.cleanup_stale_source_cache()

        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        if not is_enabled(account_id):
            return cls._neutral_duration_check_result()

        readiness = cls.compute_readiness(project)
        if readiness.status != "green" or not (
            readiness.drive_video_id or readiness.local_video_path
        ):
            raise ValueError(
                f"Project is not ready for upload: {', '.join(readiness.reasons)}"
            )

        duration_seconds = cls._resolve_final_video_duration(
            project_id, readiness, probe_media
        )

        if duration_seconds <= max_duration + 0.01:
            return {
                "needed": False,
                "duration_seconds": round(duration_seconds, 2),
                "speed_factor": 1.0,
                "sped_up_available": False,
            }

        speed_factor = duration_seconds / max_duration
        sped_up_available = speed_factor <= max_speed + 1e-6

        # A choice modal will open: warm the shared preview cache now so the
        # previews are ready as soon as possible.  Never blocks the check.
        cls.start_source_video_download(project_id, readiness)

        return {
            "needed": True,
            "duration_seconds": round(duration_seconds, 2),
            "speed_factor": round(speed_factor, 4),
            "sped_up_available": sped_up_available,
        }
```

Update both callers (drop the removed kwargs):

```python
    @classmethod
    def check_facebook_duration(
        cls,
        project_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return cls._check_platform_duration(
            project_id,
            account_id,
            cleanup_stale=cls.cleanup_stale_facebook_prep,
            is_enabled=cls._facebook_upload_enabled,
            probe_media=SocialUploadService._probe_facebook_media,
            max_duration=SocialUploadService._FACEBOOK_MAX_DURATION_SECONDS,
            max_speed=SocialUploadService._FACEBOOK_MAX_SPEED_FACTOR,
        )

    @classmethod
    def check_youtube_duration(
        cls,
        project_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return cls._check_platform_duration(
            project_id,
            account_id,
            cleanup_stale=cls.cleanup_stale_youtube_prep,
            is_enabled=cls._youtube_upload_enabled,
            probe_media=SocialUploadService._probe_youtube_media,
            max_duration=SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS,
            max_speed=SocialUploadService._YOUTUBE_MAX_SPEED_FACTOR,
        )
```

- [ ] **Step 4: Run the new tests and the full backend suite**

Run: `cd backend && pixi run pytest tests/test_platform_duration_check.py -v`
Expected: 5 passed

Run: `cd backend && pixi run pytest`
Expected: all pass (catches any other caller of the old signature)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/upload_phase.py backend/tests/test_platform_duration_check.py
git commit -m "feat(upload): duration check via Drive metadata, no blocking download"
```

---

### Task 4: Preview/status routes

**Files:**
- Modify: `backend/app/api/routes/project_manager.py` — fix `facebook_duration_check` docstring (~line 142), replace `facebook_preview_video` (~line 160) and `youtube_preview_video` (~line 210) with two new routes
- Test: `backend/tests/test_upload_source_routes.py` (create)

**Interfaces:**
- Consumes: Task 2 `start_source_video_download`, `source_video_status`, `cached_source_video`.
- Produces:
  - `GET /api/project-manager/projects/{project_id}/upload-source-status` → `{"state": "ready"|"in_progress"|"error", "detail?": str}` (self-healing: warms the cache when missing).
  - `GET /api/project-manager/projects/{project_id}/upload-source-preview` → 200 `video/mp4` when cached, 202 JSON while in flight, 404 otherwise.
  - The per-platform `facebook-preview`/`youtube-preview` routes are **removed**.

- [ ] **Step 1: Confirm nothing else consumes the old preview routes**

Run: `grep -rn "facebook-preview\|youtube-preview" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.js" --include="*.jsx" . | grep -v node_modules | grep -v docs/`
Expected: only `backend/app/api/routes/project_manager.py` and `frontend/src/api/client.ts` (updated in Task 6). If anything else shows up, stop and reassess.

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/test_upload_source_routes.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.upload_phase import UploadPhaseService


@pytest.fixture
def client(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c


def test_status_warms_cache_and_reports(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: {"state": "in_progress"}),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-status")
    assert resp.status_code == 200
    assert resp.json() == {"state": "in_progress"}


def test_status_404_when_project_missing(client, monkeypatch):
    def raise_missing(cls, pid, readiness=None):
        raise ValueError("Project not found")

    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(raise_missing),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-status")
    assert resp.status_code == 404


def test_preview_202_while_in_flight(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video",
        classmethod(lambda cls, pid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService, "source_video_status",
        classmethod(lambda cls, pid: {"state": "in_progress"}),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-preview")
    assert resp.status_code == 202


def test_preview_404_when_absent(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video",
        classmethod(lambda cls, pid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService, "source_video_status",
        classmethod(lambda cls, pid: {"state": "missing"}),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-preview")
    assert resp.status_code == 404


def test_preview_serves_cached_file(client, monkeypatch, tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"mp4-bytes")
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-preview")
    assert resp.status_code == 200
    assert resp.content == b"mp4-bytes"
    assert resp.headers["content-type"] == "video/mp4"


def test_old_platform_preview_routes_removed(client):
    for url in (
        "/api/project-manager/projects/p1/facebook-preview/original",
        "/api/project-manager/projects/p1/youtube-preview/original",
    ):
        assert client.get(url).status_code == 404
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && pixi run pytest tests/test_upload_source_routes.py -v`
Expected: new-route tests FAIL with 404; `test_old_platform_preview_routes_removed` fails on the old routes still existing (they 404 differently only after removal — expect assertion failures on the *new* endpoints first)

- [ ] **Step 4: Implement route changes**

In `backend/app/api/routes/project_manager.py`:

a) Fix the stale docstring of `facebook_duration_check` (~line 142):

```python
    """Check if the project video exceeds Facebook's 90s Reel limit.

    Uses Drive metadata / local probe only — no video download. When a
    choice is needed, the shared preview cache is warmed in the background.
    """
```

b) Delete `facebook_preview_video` (lines ~160–187) and `youtube_preview_video` (lines ~210–235) entirely, and add in their place:

```python
@router.get("/projects/{project_id}/upload-source-status")
async def upload_source_status(project_id: str):
    """State of the shared final-video preview cache; warms it when missing."""
    try:
        return await asyncio.to_thread(
            UploadPhaseService.start_source_video_download, project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/upload-source-preview")
async def upload_source_preview(project_id: str):
    """Serve the cached final video used by the duration-choice modals."""
    video_path = UploadPhaseService.cached_source_video(project_id)
    if video_path is not None and video_path.exists():
        return FileResponse(
            path=video_path,
            media_type="video/mp4",
            filename=video_path.name,
        )
    status = UploadPhaseService.source_video_status(project_id)
    if status["state"] == "in_progress":
        return JSONResponse(status_code=202, content=status)
    raise HTTPException(status_code=404, detail=status.get("detail") or "Preview not cached")
```

c) Ensure imports: the module already imports `FileResponse` and `asyncio`; add `JSONResponse` to the existing `fastapi.responses` import if absent. Remove the now-unused `Literal` import if nothing else in the file uses it (check with grep before removing).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && pixi run pytest tests/test_upload_source_routes.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/routes/project_manager.py backend/tests/test_upload_source_routes.py
git commit -m "feat(api): shared upload-source preview/status routes, drop per-platform previews"
```

---

### Task 5: Upload job reuses the cached source video

**Files:**
- Modify: `backend/app/services/upload_phase.py` (~lines 867–876, inside the upload job's `tempfile.TemporaryDirectory` block)

**Interfaces:**
- Consumes: Task 2 `cached_source_video(project_id)`.
- Produces: no new interface — behavior change only (third Drive download avoided when the check already cached the file).

- [ ] **Step 1: Apply the edit**

Current code (~lines 867–876):

```python
        with tempfile.TemporaryDirectory(prefix=f"atr-upload-{project_id}-") as tmp_dir:
            local_video = Path(readiness.local_video_path) if readiness.local_video_path else None
            video_name = drive_video_name or (local_video.name if local_video else "final_video.mp4")
            local_video_path = Path(tmp_dir) / video_name
            if local_video is not None and local_video.exists():
                emit_progress(0.30, "download", "Copying final video from local output...")
                shutil.copy2(local_video, local_video_path)
            else:
                emit_progress(0.30, "download", "Downloading final video from Drive...")
                GoogleDriveService.download_file(drive_video_id, local_video_path)
```

Replace the `else` branch:

```python
            if local_video is not None and local_video.exists():
                emit_progress(0.30, "download", "Copying final video from local output...")
                shutil.copy2(local_video, local_video_path)
            else:
                cached_source = cls.cached_source_video(project_id)
                if cached_source is not None and cached_source.exists():
                    emit_progress(0.30, "download", "Copying final video from preview cache...")
                    shutil.copy2(cached_source, local_video_path)
                else:
                    emit_progress(0.30, "download", "Downloading final video from Drive...")
                    GoogleDriveService.download_file(drive_video_id, local_video_path)
```

- [ ] **Step 2: Run the full backend suite**

Run: `cd backend && pixi run pytest`
Expected: all pass (this path has no dedicated unit test — `run_upload` is monolithic; `cached_source_video` behavior is covered by Task 2 tests, and this branch is exercised in the manual E2E in Task 7)

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/upload_phase.py
git commit -m "feat(upload): reuse preview cache instead of re-downloading final video"
```

---

### Task 6: Frontend — polling hook + modals use the shared preview

**Files:**
- Modify: `frontend/src/api/client.ts` (~lines 252–265)
- Create: `frontend/src/hooks/useUploadSourcePreview.ts`
- Modify: `frontend/src/components/project-manager/FacebookDurationModal.tsx`
- Modify: `frontend/src/components/project-manager/YouTubeDurationModal.tsx`

**Interfaces:**
- Consumes: Task 4 routes.
- Produces: `useUploadSourcePreview(projectId: string, active: boolean) => { status: "loading" | "ready" | "error"; url: string }`.

- [ ] **Step 1: Update the API client**

In `frontend/src/api/client.ts`, delete `getFacebookPreviewUrl` (~line 252) and `getYouTubePreviewUrl` (~line 264) and add in their place:

```ts
  getUploadSourcePreviewUrl: (projectId: string) =>
    `${API_BASE}/project-manager/projects/${projectId}/upload-source-preview`,

  getUploadSourceStatus: (projectId: string) =>
    request<{ state: "ready" | "in_progress" | "error" | "missing"; detail?: string }>(
      `/project-manager/projects/${projectId}/upload-source-status`,
    ),
```

- [ ] **Step 2: Create the hook**

```ts
// frontend/src/hooks/useUploadSourcePreview.ts
import { useEffect, useState } from "react";
import { api } from "@/api/client";

export type UploadSourcePreviewStatus = "loading" | "ready" | "error";

/**
 * Polls the backend until the shared final-video preview cache is ready.
 * The backend warms the cache on the first status call, so mounting this
 * hook is enough to trigger the download.
 */
export function useUploadSourcePreview(projectId: string, active: boolean) {
  const [status, setStatus] = useState<UploadSourcePreviewStatus>("loading");

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await api.getUploadSourceStatus(projectId);
        if (cancelled) return;
        if (result.state === "ready") {
          setStatus("ready");
          return;
        }
        if (result.state === "error") {
          setStatus("error");
          return;
        }
      } catch {
        // transient network error: keep polling
      }
      if (!cancelled) {
        timer = window.setTimeout(poll, 2000);
      }
    };

    setStatus("loading");
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [projectId, active]);

  return { status, url: api.getUploadSourcePreviewUrl(projectId) };
}
```

- [ ] **Step 3: Update FacebookDurationModal**

In `frontend/src/components/project-manager/FacebookDurationModal.tsx`:

a) Imports: add `Loader2` to the lucide-react import and import the hook:

```tsx
import { Scissors, Zap, X, Ban, Loader2 } from "lucide-react";
import { useUploadSourcePreview } from "@/hooks/useUploadSourcePreview";
```

b) Inside the component, replace `const originalUrl = api.getFacebookPreviewUrl(projectId, "original");` (line 72) with:

```tsx
  const preview = useUploadSourcePreview(projectId, open);
```

(The `api` import can be removed if nothing else in the file uses it — check before removing.)

Note: the hook must be called before the `if (!open) return null;` early return (Rules of Hooks) — move the early return below the hook call if needed; the hook itself is inert while `open` is false.

c) Add a placeholder fragment used by both video slots. Insert above the `card` definition:

```tsx
  const previewPlaceholder = (
    <div className="w-full h-full flex flex-col items-center justify-center gap-2 text-white/70">
      {preview.status === "loading" ? (
        <>
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="text-xs">Préparation de l'aperçu...</span>
        </>
      ) : (
        <span className="text-xs">Aperçu indisponible</span>
      )}
    </div>
  );
```

d) Replace the "cut" video element (lines 117–124) with:

```tsx
            {preview.status === "ready" ? (
              <video
                ref={cutVideoRef}
                src={preview.url}
                className="w-full h-full object-contain"
                controls
                preload="metadata"
                onTimeUpdate={handleCutTimeUpdate}
              />
            ) : (
              previewPlaceholder
            )}
```

e) Replace the sped-up video element (lines 149–156) with:

```tsx
              {preview.status === "ready" ? (
                <video
                  ref={spedUpVideoRef}
                  src={preview.url}
                  className="w-full h-full object-contain"
                  controls
                  preload="metadata"
                  onLoadedMetadata={handleSpedUpLoadedMetadata}
                />
              ) : (
                previewPlaceholder
              )}
```

The badges (`Coupée à 1:30` / `Accélérée x…`) and all buttons stay as they are — choices remain usable while the preview loads.

- [ ] **Step 4: Update YouTubeDurationModal**

Apply the same four edits to `frontend/src/components/project-manager/YouTubeDurationModal.tsx` (it mirrors the Facebook modal: `originalUrl` at line 72 uses `api.getYouTubePreviewUrl(projectId, "original")`; the two `<video>` elements are structured identically). Same hook, same placeholder, same conditional rendering.

- [ ] **Step 5: Typecheck and build**

Run: `cd frontend && npm run build`
Expected: `tsc -b` and vite build succeed with no errors. If `getFacebookPreviewUrl`/`getYouTubePreviewUrl` are referenced anywhere else, tsc will flag it — update those call sites to the new API.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/hooks/useUploadSourcePreview.ts frontend/src/components/project-manager/FacebookDurationModal.tsx frontend/src/components/project-manager/YouTubeDurationModal.tsx
git commit -m "feat(frontend): instant duration modals with polled shared preview"
```

---

### Task 7: Full verification

**Files:** none new.

- [ ] **Step 1: Full backend suite**

Run: `cd backend && pixi run pytest`
Expected: all pass

- [ ] **Step 2: Frontend build**

Run: `cd frontend && npm run build`
Expected: success

- [ ] **Step 3: Manual E2E (requires running app + a Drive-only project > 90 s)**

1. Start backend + frontend, open the project manager, trigger an upload on a long Drive-only project.
2. Expect: "Vérification de la durée Facebook..." resolves in ~1–2 s; the modal opens immediately with `Préparation de l'aperçu...` spinners; both previews start playing after a single background download; the sped-up side plays at `xN` speed.
3. Choose a strategy and confirm the upload job logs `Copying final video from preview cache...` instead of a Drive download.

If no test project is available, note this step as pending user verification in the final report.

- [ ] **Step 4: Final commit if anything was fixed during verification**
