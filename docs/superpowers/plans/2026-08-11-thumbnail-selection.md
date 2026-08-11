# Thumbnail Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a thumbnail-selection modal to the upload flow (5 scene-frame candidates) and propagate the chosen frame to TikTok (PFM `thumbnail_timestamp_ms`), Instagram (`thumb_offset`), YouTube (`thumbnails.set`), and Facebook immediate (`/{video_id}/thumbnails`).

**Architecture:** The selection primitive is a **timestamp (ms) in the final output video**. A new backend `ThumbnailService` computes 5 candidate timestamps from `output/transcription_timing.json` and extracts preview JPEGs from the existing `upload_source` cache. The chosen timestamp flows through `UploadProjectRequest → enqueue_upload → execute_upload`, then per platform: timestamp for TikTok/Instagram, server-extracted JPEG for YouTube/Facebook. Spec: `docs/superpowers/specs/2026-08-11-thumbnail-selection-design.md`.

**Tech Stack:** FastAPI + Pydantic (backend `backend/`, VPS `server/`), OpenCV via `AnimeMatcherService.extract_frames` (PTS-accurate), React 19 + framer-motion + Tailwind (frontend), httpx (PFM), googleapiclient (YouTube), requests (Facebook Graph).

## Global Constraints

- All user-facing UI copy is **French** (match existing modals: "Vérification de la durée Facebook...", etc.).
- Platform thumbnail failures are **never fatal** to an upload — warnings go into the platform result `detail`.
- Backend tests: run from repo root with `pixi run -e dev pytest backend/tests/<file> -v`. NEVER run two pytest invocations concurrently. The dev-env full suite has ~17 known pre-existing failures (all attributed); only files you touch must be clean.
- Server (VPS) tests: `cd server && .venv/bin/python -m pytest tests/<file> -v`.
- Frontend verification: `cd frontend && npx tsc -b` (type-check; there are no frontend unit tests).
- The candidate shift is `3/60 s = 0.05 s` (3 frames at the 60 fps TikTok timeline).
- New backend cache dir: `settings.cache_dir / "upload_thumbs" / <project_id> / <version>/`.
- Commit after every task (small, descriptive commits).

---

### Task 1: ThumbnailService — candidates + frame extraction

**Files:**
- Create: `backend/app/services/thumbnail_service.py`
- Test: `backend/tests/test_thumbnail_service.py`

**Interfaces:**
- Consumes: `Transcription` model (`backend/app/models/transcription.py`), `ExportService.get_output_dir(project_id)` (`backend/app/services/export_service.py:213`), `AnimeMatcherService.extract_frames(video_path, timestamps) -> list[Image|None]` and `.extract_frame(video_path, timestamp) -> Image|None` (`backend/app/services/anime_matcher.py:415,448`), `settings.cache_dir`.
- Produces (used by Tasks 2, 6):
  - `ThumbnailCandidate` frozen dataclass: `index: int`, `label: str`, `timestamp_seconds: float`, property `timestamp_ms: int`.
  - `ThumbnailService.load_final_timeline(project_id: str) -> Transcription | None`
  - `ThumbnailService.compute_candidates(transcription: Transcription) -> list[ThumbnailCandidate]`
  - `ThumbnailService.build_candidates_payload(project_id: str, video_path: Path) -> dict[str, Any]`
  - `ThumbnailService.cached_frame_path(project_id: str, index: int) -> Path | None`
  - `ThumbnailService.extract_frame_image(video_path: Path, timestamp_seconds: float, dest_path: Path) -> Path | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for thumbnail candidate computation and caching."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

from app.models.transcription import SceneTranscription, Transcription
from app.services.thumbnail_service import ThumbnailCandidate, ThumbnailService


def _transcription(bounds: list[tuple[float, float]]) -> Transcription:
    return Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=i, text="", start_time=start, end_time=end
            )
            for i, (start, end) in enumerate(bounds)
        ],
    )


def test_five_candidates_with_shift_and_mid():
    tr = _transcription([(0.0, 4.0), (4.0, 7.0), (7.0, 11.0)])
    cands = ThumbnailService.compute_candidates(tr)
    assert [c.index for c in cands] == [0, 1, 2, 3, 4]
    assert cands[0].timestamp_seconds == pytest.approx(0.05)   # scene 1 start + shift
    assert cands[1].timestamp_seconds == pytest.approx(2.0)    # scene 1 mid, no shift
    assert cands[2].timestamp_seconds == pytest.approx(3.95)   # scene 1 end - shift
    assert cands[3].timestamp_seconds == pytest.approx(4.05)   # scene 2 start + shift
    assert cands[4].timestamp_seconds == pytest.approx(7.05)   # scene 3 start + shift
    assert cands[0].label == "Scène 1 · début"
    assert cands[1].label == "Scène 1 · milieu"
    assert cands[2].label == "Scène 1 · fin"
    assert cands[3].label == "Scène 2 · début"
    assert cands[4].label == "Scène 3 · début"


def test_timestamp_ms_rounds():
    c = ThumbnailCandidate(index=0, label="x", timestamp_seconds=1.2345)
    assert c.timestamp_ms == 1234  # int(round(1.2345 * 1000)) == 1234 (banker's-free)


def test_fewer_scenes_yield_fewer_candidates():
    tr = _transcription([(0.0, 4.0)])
    cands = ThumbnailService.compute_candidates(tr)
    assert len(cands) == 3
    tr2 = _transcription([(0.0, 4.0), (4.0, 7.0)])
    assert len(ThumbnailService.compute_candidates(tr2)) == 4


def test_tiny_scene_shift_clamped_to_mid():
    # Scene shorter than 2×shift: start+shift and end-shift both clamp to mid.
    tr = _transcription([(0.0, 0.06)])
    cands = ThumbnailService.compute_candidates(tr)
    mid = 0.03
    assert cands[0].timestamp_seconds == pytest.approx(mid)
    assert cands[2].timestamp_seconds == pytest.approx(mid)


def test_empty_or_degenerate_scenes_skipped():
    assert ThumbnailService.compute_candidates(_transcription([])) == []
    # zero-length scene is ignored entirely
    assert ThumbnailService.compute_candidates(_transcription([(2.0, 2.0)])) == []


def test_load_final_timeline_reads_output_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path
    )
    out_dir = tmp_path / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "t",'
        ' "words": [], "start_time": 0.0, "end_time": 3.0, "is_raw": false}]}'
    )
    tr = ThumbnailService.load_final_timeline("p1")
    assert tr is not None
    assert tr.scenes[0].end_time == 3.0
    assert ThumbnailService.load_final_timeline("missing") is None


@pytest.fixture
def fake_frames(monkeypatch):
    def _extract_frames(video_path, timestamps):
        return [Image.new("RGB", (4, 4), (255, 0, 0)) for _ in timestamps]

    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, video_path, timestamps: _extract_frames(video_path, timestamps)),
    )


def test_build_candidates_payload_caches_jpegs(tmp_path, monkeypatch, fake_frames):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = tmp_path / "projects" / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "",'
        ' "words": [], "start_time": 0.0, "end_time": 4.0, "is_raw": false}]}'
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)

    payload = ThumbnailService.build_candidates_payload("p1", video)
    assert payload["state"] == "ready"
    assert len(payload["candidates"]) == 3
    first = payload["candidates"][0]
    assert first["index"] == 0
    assert first["timestamp_ms"] == 50
    assert first["image_url"].startswith("/project-manager/projects/p1/thumbnail-frame/0")
    frame = ThumbnailService.cached_frame_path("p1", 0)
    assert frame is not None and frame.exists()


def test_build_candidates_payload_error_without_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)
    payload = ThumbnailService.build_candidates_payload("p1", video)
    assert payload["state"] == "error"
    assert payload["detail"]


def test_build_candidates_payload_drops_failed_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = tmp_path / "projects" / "p1" / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "transcription_timing.json").write_text(
        '{"language": "fr", "scenes": [{"scene_index": 0, "text": "",'
        ' "words": [], "start_time": 0.0, "end_time": 4.0, "is_raw": false}]}'
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(
            lambda cls, video_path, timestamps: [
                None if i == 1 else Image.new("RGB", (4, 4)) for i in range(len(timestamps))
            ]
        ),
    )
    payload = ThumbnailService.build_candidates_payload("p1", video)
    assert payload["state"] == "ready"
    assert [c["index"] for c in payload["candidates"]] == [0, 2]


def test_extract_frame_image(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frame",
        classmethod(lambda cls, video_path, timestamp: Image.new("RGB", (4, 4))),
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00")
    dest = tmp_path / "thumb.jpg"
    result = ThumbnailService.extract_frame_image(video, 1.5, dest)
    assert result == dest and dest.exists()
    # failure path returns None
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frame",
        classmethod(lambda cls, video_path, timestamp: None),
    )
    assert ThumbnailService.extract_frame_image(video, 1.5, tmp_path / "t2.jpg") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.thumbnail_service'`

- [ ] **Step 3: Implement the service**

```python
"""Thumbnail candidate computation and frame extraction for upload covers.

Candidates are timestamps in the FINAL rendered video, computed from the
authoritative playback timeline (output/transcription_timing.json). Preview
JPEGs are extracted from the shared upload_source cache in one decode pass.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from ..models.transcription import Transcription
from .anime_matcher import AnimeMatcherService
from .export_service import ExportService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailCandidate:
    index: int
    label: str
    timestamp_seconds: float

    @property
    def timestamp_ms(self) -> int:
        return int(round(self.timestamp_seconds * 1000))


class ThumbnailService:
    # 3 frames at the 60fps TikTok timeline — absorbs off-by-a-frame scene cuts.
    _SHIFT_SECONDS = 3.0 / 60.0
    _JPEG_QUALITY = 90
    _THUMBS_CACHE_DIR = settings.cache_dir / "upload_thumbs"

    @classmethod
    def load_final_timeline(cls, project_id: str) -> Transcription | None:
        path = ExportService.get_output_dir(project_id) / "transcription_timing.json"
        if not path.exists():
            return None
        try:
            return Transcription.model_validate(json.loads(path.read_text()))
        except Exception:
            logger.warning(
                "Unreadable transcription_timing.json for project %s", project_id,
                exc_info=True,
            )
            return None

    @classmethod
    def compute_candidates(cls, transcription: Transcription) -> list[ThumbnailCandidate]:
        scenes = [s for s in transcription.scenes if s.end_time > s.start_time]
        if not scenes:
            return []
        shift = cls._SHIFT_SECONDS
        first = scenes[0]
        mid = (first.start_time + first.end_time) / 2
        spots: list[tuple[str, float]] = [
            ("Scène 1 · début", min(first.start_time + shift, mid)),
            ("Scène 1 · milieu", mid),
            ("Scène 1 · fin", max(first.end_time - shift, mid)),
        ]
        for ordinal, scene in enumerate(scenes[1:3], start=2):
            scene_mid = (scene.start_time + scene.end_time) / 2
            spots.append(
                (f"Scène {ordinal} · début", min(scene.start_time + shift, scene_mid))
            )
        return [
            ThumbnailCandidate(index=i, label=label, timestamp_seconds=round(ts, 3))
            for i, (label, ts) in enumerate(spots)
        ]

    @classmethod
    def _project_thumbs_dir(cls, project_id: str) -> Path:
        return cls._THUMBS_CACHE_DIR / project_id

    @classmethod
    def build_candidates_payload(
        cls, project_id: str, video_path: Path
    ) -> dict[str, Any]:
        """Compute candidates, extract+cache JPEGs, return the API payload."""
        transcription = cls.load_final_timeline(project_id)
        if transcription is None:
            return {
                "state": "error",
                "detail": "Timeline finale introuvable (transcription_timing.json)",
            }
        candidates = cls.compute_candidates(transcription)
        if not candidates:
            return {
                "state": "error",
                "detail": "Aucune scène exploitable dans la timeline finale",
            }

        stat = video_path.stat()
        version = f"{stat.st_mtime_ns}-{stat.st_size}"
        cache_dir = cls._project_thumbs_dir(project_id) / version
        if not all(
            (cache_dir / f"cand_{c.index}.jpg").exists() for c in candidates
        ):
            # Source video changed (or first call): rebuild from scratch.
            shutil.rmtree(cls._project_thumbs_dir(project_id), ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            images = AnimeMatcherService.extract_frames(
                video_path, [c.timestamp_seconds for c in candidates]
            )
            for candidate, image in zip(candidates, images):
                if image is None:
                    logger.warning(
                        "Thumbnail frame extraction failed: project=%s index=%d t=%.3f",
                        project_id, candidate.index, candidate.timestamp_seconds,
                    )
                    continue
                image.convert("RGB").save(
                    cache_dir / f"cand_{candidate.index}.jpg",
                    "JPEG",
                    quality=cls._JPEG_QUALITY,
                )

        payload = [
            {
                "index": c.index,
                "label": c.label,
                "timestamp_ms": c.timestamp_ms,
                "image_url": (
                    f"/project-manager/projects/{project_id}"
                    f"/thumbnail-frame/{c.index}?v={version}"
                ),
            }
            for c in candidates
            if (cache_dir / f"cand_{c.index}.jpg").exists()
        ]
        if not payload:
            return {
                "state": "error",
                "detail": "Extraction des miniatures impossible depuis la vidéo finale",
            }
        return {"state": "ready", "version": version, "candidates": payload}

    @classmethod
    def cached_frame_path(cls, project_id: str, index: int) -> Path | None:
        base = cls._project_thumbs_dir(project_id)
        if not base.exists():
            return None
        for version_dir in sorted(base.iterdir(), reverse=True):
            candidate = version_dir / f"cand_{index}.jpg"
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def extract_frame_image(
        cls, video_path: Path, timestamp_seconds: float, dest_path: Path
    ) -> Path | None:
        """Single-frame JPEG for image-native platforms (YouTube/Facebook)."""
        try:
            image = AnimeMatcherService.extract_frame(video_path, timestamp_seconds)
            if image is None:
                return None
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGB").save(dest_path, "JPEG", quality=cls._JPEG_QUALITY)
            return dest_path
        except Exception:
            logger.warning(
                "Thumbnail image extraction failed: %s t=%.3f",
                video_path, timestamp_seconds, exc_info=True,
            )
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/thumbnail_service.py backend/tests/test_thumbnail_service.py
git commit -m "feat: thumbnail candidate computation and frame extraction service"
```

---

### Task 2: Thumbnail API routes

**Files:**
- Modify: `backend/app/api/routes/project_manager.py` (add two routes after `upload_source_preview`, ~line 227)
- Test: `backend/tests/test_thumbnail_routes.py`

**Interfaces:**
- Consumes (Task 1): `ThumbnailService.build_candidates_payload`, `ThumbnailService.cached_frame_path`; existing `UploadPhaseService.start_source_video_download(project_id)` and `UploadPhaseService.cached_source_video(project_id)`.
- Produces (used by Task 8):
  - `GET /api/project-manager/projects/{project_id}/thumbnail-candidates` → `{"state": "ready"|"in_progress"|"error"|"missing", "detail"?, "version"?, "candidates"?: [{index, label, timestamp_ms, image_url}]}`
  - `GET /api/project-manager/projects/{project_id}/thumbnail-frame/{index}?v=...` → JPEG `FileResponse` or 404.

- [ ] **Step 1: Write the failing tests** (mirror `backend/tests/test_upload_source_routes.py`'s `client` fixture exactly)

```python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.thumbnail_service import ThumbnailService
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


def test_candidates_report_in_progress_while_warming(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: {"state": "in_progress"}),
    )
    resp = client.get("/api/project-manager/projects/p1/thumbnail-candidates")
    assert resp.status_code == 200
    assert resp.json()["state"] == "in_progress"


def test_candidates_404_when_project_missing(client, monkeypatch):
    def raise_missing(cls, pid, readiness=None):
        raise ValueError("Project not found")

    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download", classmethod(raise_missing)
    )
    resp = client.get("/api/project-manager/projects/p1/thumbnail-candidates")
    assert resp.status_code == 404


def test_candidates_ready_delegates_to_service(client, monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00")
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: {"state": "ready"}),
    )
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video", classmethod(lambda cls, pid: video)
    )
    monkeypatch.setattr(
        ThumbnailService, "build_candidates_payload",
        classmethod(lambda cls, pid, vp: {
            "state": "ready",
            "version": "1-1",
            "candidates": [{
                "index": 0, "label": "Scène 1 · début", "timestamp_ms": 50,
                "image_url": "/project-manager/projects/p1/thumbnail-frame/0?v=1-1",
            }],
        }),
    )
    resp = client.get("/api/project-manager/projects/p1/thumbnail-candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["candidates"][0]["timestamp_ms"] == 50


def test_frame_served_and_404(client, monkeypatch, tmp_path):
    jpg = tmp_path / "cand_0.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(
        ThumbnailService, "cached_frame_path",
        classmethod(lambda cls, pid, index: jpg if index == 0 else None),
    )
    ok = client.get("/api/project-manager/projects/p1/thumbnail-frame/0")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"
    missing = client.get("/api/project-manager/projects/p1/thumbnail-frame/3")
    assert missing.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_routes.py -v`
Expected: FAIL with 404s (routes don't exist yet)

- [ ] **Step 3: Add the routes**

In `backend/app/api/routes/project_manager.py`, import `ThumbnailService` (`from ...services.thumbnail_service import ThumbnailService`) and add after `upload_source_preview`:

```python
@router.get("/projects/{project_id}/thumbnail-candidates")
async def thumbnail_candidates(project_id: str):
    """Thumbnail candidates for the upload cover; warms the source cache."""
    try:
        status = await asyncio.to_thread(
            UploadPhaseService.start_source_video_download, project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if status.get("state") != "ready":
        return {"state": status.get("state"), "detail": status.get("detail")}
    video_path = UploadPhaseService.cached_source_video(project_id)
    if video_path is None or not video_path.exists():
        return {"state": "missing"}
    return await asyncio.to_thread(
        ThumbnailService.build_candidates_payload, project_id, video_path
    )


@router.get("/projects/{project_id}/thumbnail-frame/{index}")
async def thumbnail_frame(project_id: str, index: int, v: str | None = None):
    """Serve a cached thumbnail candidate JPEG."""
    path = ThumbnailService.cached_frame_path(project_id, index)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail frame not cached")
    return FileResponse(
        path=path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )
```

(`Path` from `pathlib` is not currently imported in this file — the routes above don't need it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_routes.py backend/tests/test_upload_source_routes.py -v`
Expected: all PASS (including the pre-existing upload-source route tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/project_manager.py backend/tests/test_thumbnail_routes.py
git commit -m "feat: thumbnail candidates and frame API routes"
```

---

### Task 3: Request plumbing — `thumbnail_timestamp_ms` end to end (backend)

**Files:**
- Modify: `backend/app/api/routes/project_manager.py` (`UploadProjectRequest` ~line 24, `run_upload_phase` ~line 64)
- Modify: `backend/app/services/project_upload_service.py` (`UploadRequestSpec` ~line 104, `enqueue_upload` ~line 186, `execute_upload` call ~line 378)
- Modify: `backend/app/services/upload_phase.py` (`execute_upload` signature ~line 820)
- Test: `backend/tests/test_thumbnail_routes.py` (extend)

**Interfaces:**
- Produces: `execute_upload(..., thumbnail_timestamp_ms: int | None = None)` — consumed by Tasks 4–7. Field name is `thumbnail_timestamp_ms` at every layer.

- [ ] **Step 1: Write the failing test** (append to `backend/tests/test_thumbnail_routes.py`)

```python
def test_upload_route_forwards_thumbnail_timestamp(client, monkeypatch):
    captured: dict = {}

    async def fake_enqueue(self, **kwargs):
        captured.update(kwargs)
        from app.services.project_upload_service import ProjectUploadJob  # noqa: PLC0415
        return ProjectUploadJob(project_id=kwargs["project_id"])

    monkeypatch.setattr(
        "app.services.project_upload_service.ProjectUploadService.enqueue_upload",
        fake_enqueue,
    )
    resp = client.post(
        "/api/project-manager/projects/p1/upload",
        json={"account_id": "acc", "thumbnail_timestamp_ms": 2350},
    )
    assert resp.status_code == 200
    assert captured["thumbnail_timestamp_ms"] == 2350
```

Note: verify `ProjectUploadJob` is importable from `app.services.project_upload_service` (it is referenced there); if it lives in a models module, import it from its actual home.

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_routes.py -v`
Expected: new test FAILS (`KeyError: 'thumbnail_timestamp_ms'` or TypeError)

- [ ] **Step 3: Thread the field through the three layers**

`project_manager.py`:

```python
class UploadProjectRequest(BaseModel):
    account_id: str | None = None
    platforms: list[Literal["youtube", "facebook", "instagram"]] | None = None
    facebook_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    instagram_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    youtube_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    copyright_audio_path: str | None = None
    thumbnail_timestamp_ms: int | None = None
```

and in `run_upload_phase`, pass `thumbnail_timestamp_ms=req.thumbnail_timestamp_ms` to `enqueue_upload`.

`project_upload_service.py` — add `thumbnail_timestamp_ms: int | None = None` to the `UploadRequestSpec` dataclass, to `enqueue_upload`'s keyword args, store it in the `UploadRequestSpec(...)` construction, and pass `thumbnail_timestamp_ms=request.thumbnail_timestamp_ms` in the `execute_upload` call at ~line 378.

`upload_phase.py` — add to `execute_upload`'s signature after `copyright_audio_path`:

```python
        copyright_audio_path: str | None = None,
        thumbnail_timestamp_ms: int | None = None,
```

(The value is consumed in Tasks 4–7; for now it is accepted and unused.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_routes.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/project_manager.py backend/app/services/project_upload_service.py backend/app/services/upload_phase.py backend/tests/test_thumbnail_routes.py
git commit -m "feat: thread thumbnail_timestamp_ms through the upload request pipeline"
```

---

### Task 4: TikTok propagation — backend payload + VPS + PFM body

**Files:**
- Modify: `backend/app/services/upload_phase.py` (`_build_tiktok_payload` ~line 674 and its call site ~line 931)
- Modify: `backend/tests/test_upload_phase_tiktok.py`
- Modify: `server/app/api/internal.py` (`TikTokPayload` ~line 38)
- Modify: `server/app/services/post_for_me_publisher.py` (`create_tiktok_post` ~line 324, `publish_to_tiktok` ~line 512)
- Modify: `server/app/services/reminder_scheduler.py` (`create_tiktok_post` call ~line 321)
- Test: `server/tests/test_post_for_me_publisher.py`, `server/tests/test_reminder_scheduler.py`

**Interfaces:**
- Consumes: `thumbnail_timestamp_ms` from Task 3.
- Produces: PFM `POST /social-posts` body's media item becomes `{"url": ..., "thumbnail_timestamp_ms": <int>}` when set; VPS `TikTokPayload.thumbnail_timestamp_ms: int | None`.

- [ ] **Step 1: Write the failing backend tests** (in `backend/tests/test_upload_phase_tiktok.py`)

Update `test_build_tiktok_payload_full`'s expected dict — it must NOT gain the key when the argument is omitted — and add:

```python
def test_build_tiktok_payload_includes_thumbnail_timestamp():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_123"))
    payload = UploadPhaseService._build_tiktok_payload(
        account, "desc", thumbnail_timestamp_ms=2350
    )
    assert payload["thumbnail_timestamp_ms"] == 2350


def test_build_tiktok_payload_omits_thumbnail_when_none():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_123"))
    payload = UploadPhaseService._build_tiktok_payload(account, "desc")
    assert "thumbnail_timestamp_ms" not in payload
```

- [ ] **Step 2: Run backend tests to verify the new ones fail**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_tiktok.py -v`
Expected: 2 new tests FAIL (TypeError: unexpected keyword argument)

- [ ] **Step 3: Implement the backend side**

`_build_tiktok_payload`:

```python
    @classmethod
    def _build_tiktok_payload(
        cls,
        account: AccountConfig | None,
        tiktok_description: str,
        thumbnail_timestamp_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Payload for the VPS server's Post for Me publish (see server TikTokPayload)."""
        if account is None or account.tiktok is None:
            return None
        tiktok = account.tiktok
        if not tiktok.post_for_me_account_id:
            return None
        payload: dict[str, Any] = {
            "social_account_id": tiktok.post_for_me_account_id,
            "post_for_me_platform": tiktok.post_for_me_platform,
            "caption": tiktok_description,
            "privacy_status": tiktok.privacy_status,
            "allow_comment": tiktok.allow_comment,
            "allow_duet": tiktok.allow_duet,
            "allow_stitch": tiktok.allow_stitch,
        }
        if thumbnail_timestamp_ms is not None:
            payload["thumbnail_timestamp_ms"] = int(thumbnail_timestamp_ms)
        return payload
```

Call site (~line 931):

```python
        tiktok_payload = cls._build_tiktok_payload(
            account, metadata.tiktok.description, thumbnail_timestamp_ms
        )
```

- [ ] **Step 4: Run backend tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_tiktok.py -v`
Expected: all PASS

- [ ] **Step 5: Write the failing server tests**

In `server/tests/test_post_for_me_publisher.py` (uses the existing `fake` fixture):

```python
async def test_create_post_includes_thumbnail_timestamp(fake, tmp_path):
    state = TikTokPublishState(media_url="https://media.example/abc.mp4",
                               stage="media_uploaded")
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        thumbnail_timestamp_ms=2350, publish_state=state,
    )
    assert result.success is True
    assert fake.created_posts[0]["media"] == [
        {"url": "https://media.example/abc.mp4", "thumbnail_timestamp_ms": 2350}
    ]


async def test_create_post_omits_thumbnail_when_none(fake, tmp_path):
    state = TikTokPublishState(media_url="https://media.example/abc.mp4",
                               stage="media_uploaded")
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        publish_state=state,
    )
    assert result.success is True
    assert fake.created_posts[0]["media"] == [{"url": "https://media.example/abc.mp4"}]
```

In `server/tests/test_reminder_scheduler.py`, add a test copying the structure of `test_dispatch_tiktok_happy_path` (same fixtures / `_patch_phases` / `_tiktok_job` / `wait_for_inflight` helpers):

```python
async def test_dispatch_tiktok_passes_thumbnail_timestamp(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job()
    job.tiktok_payload["thumbnail_timestamp_ms"] = 2350
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert calls["create"][0]["thumbnail_timestamp_ms"] == 2350
```

- [ ] **Step 6: Run server tests to verify the new ones fail**

Run: `cd server && .venv/bin/python -m pytest tests/test_post_for_me_publisher.py tests/test_reminder_scheduler.py -v`
Expected: new tests FAIL (TypeError / KeyError)

- [ ] **Step 7: Implement the server side**

`server/app/api/internal.py`:

```python
class TikTokPayload(BaseModel):
    social_account_id: str
    caption: str
    post_for_me_platform: Literal["tiktok", "tiktok_business"] = "tiktok"
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
    thumbnail_timestamp_ms: int | None = None
```

`server/app/services/post_for_me_publisher.py` — `create_tiktok_post` gains keyword `thumbnail_timestamp_ms: int | None = None` (place after `allow_stitch`); build the media item conditionally:

```python
    media_item: dict[str, Any] = {"url": media_url}
    if thumbnail_timestamp_ms is not None:
        media_item["thumbnail_timestamp_ms"] = int(thumbnail_timestamp_ms)
    body: dict[str, Any] = {
        "caption": caption,
        "social_accounts": [social_account_id],
        "media": [media_item],
        "platform_configurations": {
            platform_configuration_key: {
                "privacy_status": privacy_status,
                "allow_comment": allow_comment,
                "allow_duet": allow_duet,
                "allow_stitch": allow_stitch,
            }
        },
    }
```

`publish_to_tiktok` gains the same keyword and forwards it in its `create_tiktok_post(...)` call (~line 551).

`server/app/services/reminder_scheduler.py` (~line 321), add to the `create_tiktok_post` call:

```python
                thumbnail_timestamp_ms=payload.get("thumbnail_timestamp_ms"),
```

- [ ] **Step 8: Run server tests to verify they pass**

Run: `cd server && .venv/bin/python -m pytest tests/test_post_for_me_publisher.py tests/test_reminder_scheduler.py tests/test_internal_api.py -v`
Expected: all PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/upload_phase.py backend/tests/test_upload_phase_tiktok.py server/app/api/internal.py server/app/services/post_for_me_publisher.py server/app/services/reminder_scheduler.py server/tests/test_post_for_me_publisher.py server/tests/test_reminder_scheduler.py
git commit -m "feat: TikTok cover via PFM thumbnail_timestamp_ms (backend + VPS)"
```

---

### Task 5: Instagram propagation — `thumb_offset` with speed scaling

**Files:**
- Modify: `backend/app/services/upload_phase.py` (`_prepare_instagram_drive_video` metadata dict ~line 809, `execute_upload` IG wiring ~line 1196, new helper `_instagram_thumb_offset`)
- Test: `backend/tests/test_upload_phase_thumbnail.py` (create)

**Interfaces:**
- Consumes: `thumbnail_timestamp_ms` (Task 3); `LimitedDurationVideoPreparation.speed_factor` / `.transcoded` from `SocialUploadService.prepare_instagram_video_for_drive` (verify the dataclass field names in `backend/app/services/social_upload_service.py` before coding — `original_duration_seconds`, `speed_factor`, `transcoded` are referenced at lines 1428-1430).
- Produces: `UploadPhaseService._instagram_thumb_offset(thumbnail_timestamp_ms: int, speed_factor: str | float | None, max_duration_seconds: float) -> int`; IG VPS payload gains `thumb_offset` (already supported by `server/app/api/internal.py:34` and `instagram_publisher.py` — no server change).

- [ ] **Step 1: Write the failing tests**

```python
"""Instagram thumb_offset scaling for the thumbnail feature."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.upload_phase import UploadPhaseService


def test_thumb_offset_passthrough_no_speed():
    assert UploadPhaseService._instagram_thumb_offset(2350, None, 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "1.0", 90.0) == 2350


def test_thumb_offset_scaled_when_sped_up():
    # 10s into the original maps to 8s in a 1.25x sped-up artifact
    assert UploadPhaseService._instagram_thumb_offset(10_000, "1.25", 90.0) == 8000


def test_thumb_offset_clamped_to_max_duration():
    # cut video: offset can never exceed the prepared duration (minus margin)
    assert UploadPhaseService._instagram_thumb_offset(95_000, None, 90.0) == 89_500


def test_thumb_offset_garbage_speed_treated_as_one():
    assert UploadPhaseService._instagram_thumb_offset(2350, "abc", 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "0.0", 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "-3", 90.0) == 2350


def test_thumb_offset_never_negative():
    assert UploadPhaseService._instagram_thumb_offset(0, None, 90.0) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_instagram_thumb_offset'`

- [ ] **Step 3: Implement**

Helper in `UploadPhaseService` (near `_build_tiktok_payload`):

```python
    @classmethod
    def _instagram_thumb_offset(
        cls,
        thumbnail_timestamp_ms: int,
        speed_factor: str | float | None,
        max_duration_seconds: float,
    ) -> int:
        """Map an original-video timestamp to the prepared IG artifact.

        The IG Drive artifact may be sped up (speed_factor > 1) or cut at
        max_duration_seconds; the Graph API thumb_offset must land inside it.
        """
        try:
            speed = float(speed_factor) if speed_factor is not None else 1.0
        except (TypeError, ValueError):
            speed = 1.0
        if not speed or speed <= 0:
            speed = 1.0
        offset = int(round(thumbnail_timestamp_ms / speed))
        ceiling = max(0, int(max_duration_seconds * 1000) - 500)
        return max(0, min(offset, ceiling))
```

In `_prepare_instagram_drive_video`, extend the success metadata dict (~line 811):

```python
            {
                "instagram_drive_file_id": file_id,
                "instagram_drive_video_url": direct_url,
                "instagram_drive_web_url": web_url,
                "instagram_drive_filename": cls._INSTAGRAM_DRIVE_FILENAME,
                "instagram_speed_factor": (
                    f"{prep.speed_factor}" if prep.transcoded and prep.speed_factor else "1.0"
                ),
            },
```

In `execute_upload`, at the IG prep success branch (~line 1195-1198), after `ig_payload["prepared_video_url"] = ...`:

```python
                    else:
                        ig_payload["prepared_video_url"] = instagram_drive_metadata[
                            "instagram_drive_video_url"
                        ]
                        if thumbnail_timestamp_ms is not None:
                            ig_payload["thumb_offset"] = cls._instagram_thumb_offset(
                                thumbnail_timestamp_ms,
                                instagram_drive_metadata.get("instagram_speed_factor"),
                                instagram_max_duration,
                            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py backend/tests/test_instagram_drive_preparation.py -v`
Expected: all PASS (the second file guards the metadata dict change; if it asserts exact dict equality, update it to include `instagram_speed_factor`)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/upload_phase.py backend/tests/test_upload_phase_thumbnail.py backend/tests/test_instagram_drive_preparation.py
git commit -m "feat: Instagram thumb_offset from thumbnail timestamp with speed scaling"
```

---

### Task 6: YouTube `thumbnails.set` + shared frame extraction in execute_upload

**Files:**
- Modify: `backend/app/services/social_upload_service.py` (`upload_youtube` ~line 672; new helper `_set_youtube_thumbnail`)
- Modify: `backend/app/services/upload_phase.py` (`execute_upload` — extract the JPEG once, pass to the YouTube job lambdas ~lines 1094, 1111)
- Test: `backend/tests/test_upload_phase_thumbnail.py` (extend)

**Interfaces:**
- Consumes: `ThumbnailService.extract_frame_image` (Task 1), `thumbnail_timestamp_ms` (Task 3).
- Produces: `upload_youtube(..., thumbnail_image_path: Path | None = None)`; `SocialUploadService._set_youtube_thumbnail(youtube, video_id: str, image_path: Path, deadline: float | None) -> str | None` (None on success, French warning string on failure). `execute_upload` local `thumbnail_image_path: Path | None` (also consumed by Task 7).

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_upload_phase_thumbnail.py`)

```python
from app.services.social_upload_service import SocialUploadService


class _FakeThumbnails:
    def __init__(self, fail: bool):
        self._fail = fail
        self.calls: list[dict] = []

    def set(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("boom")
        return object()  # request object; execution is monkeypatched


class _FakeYouTube:
    def __init__(self, fail: bool = False):
        self._thumbnails = _FakeThumbnails(fail)

    def thumbnails(self):
        return self._thumbnails


def test_set_youtube_thumbnail_success_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        SocialUploadService, "_execute_google_request",
        classmethod(lambda cls, youtube, request, **kw: {}),
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    yt = _FakeYouTube()
    warning = SocialUploadService._set_youtube_thumbnail(yt, "vid123", image, None)
    assert warning is None
    assert yt.thumbnails().calls[0]["videoId"] == "vid123"


def test_set_youtube_thumbnail_failure_returns_warning(tmp_path, monkeypatch):
    def raise_it(cls, youtube, request, **kw):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(
        SocialUploadService, "_execute_google_request", classmethod(raise_it)
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_youtube_thumbnail(
        _FakeYouTube(), "vid123", image, None
    )
    assert warning is not None
    assert "Miniature YouTube" in warning
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py -v`
Expected: new tests FAIL — no `_set_youtube_thumbnail`

- [ ] **Step 3: Implement**

Helper in `SocialUploadService` (near `upload_youtube`):

```python
    @classmethod
    def _set_youtube_thumbnail(
        cls,
        youtube,
        video_id: str,
        image_path: Path,
        deadline: float | None,
    ) -> str | None:
        """Best-effort custom thumbnail; Shorts honor it only on YPP channels."""
        try:
            request = youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(image_path), mimetype="image/jpeg"),
            )
            cls._execute_google_request(
                youtube,
                request,
                deadline=deadline,
                platform="YouTube",
                operation="thumbnail set",
            )
            return None
        except Exception as exc:
            return f"Miniature YouTube non appliquée: {exc}"
```

Note: `_FakeThumbnails.set` raising means `MediaFileUpload` construction happens first — build the request inside the `try`. The fake raises from `.set(...)` itself, which the `try` also covers.

In `upload_youtube`: add keyword `thumbnail_image_path: Path | None = None` (after `youtube_prep_dir`), and after the captions block (~line 870), before `detail_parts`:

```python
                thumbnail_warning: str | None = None
                if thumbnail_image_path is not None and thumbnail_image_path.exists():
                    thumbnail_warning = cls._set_youtube_thumbnail(
                        youtube, video_id, thumbnail_image_path, deadline
                    )

                detail_parts = []
                if scheduled_at:
                    detail_parts.append(
                        f"Programmé le {cls._format_french_datetime(scheduled_at)}"
                    )
                if thumbnail_warning:
                    detail_parts.append(thumbnail_warning)
```

In `execute_upload` (`upload_phase.py`), import `ThumbnailService` at the top of the file (`from .thumbnail_service import ThumbnailService`) and add after the copyright-audio block (~line 1060, once `local_video_path` is final):

```python
            # Extract the chosen thumbnail frame once for image-native platforms
            # (YouTube, Facebook). Extracted from the ORIGINAL output video, so
            # platform-side cut/sped_up retiming cannot shift the image.
            thumbnail_image_path: Path | None = None
            if thumbnail_timestamp_ms is not None:
                thumbnail_image_path = ThumbnailService.extract_frame_image(
                    local_video_path,
                    thumbnail_timestamp_ms / 1000.0,
                    Path(tmp_dir) / "thumbnail.jpg",
                )
```

Then pass `thumbnail_image_path=thumbnail_image_path` in BOTH `upload_youtube` lambdas (account ~line 1094 and global ~line 1111). Capture it in a local before the lambda like the existing `_yt_strategy` pattern (`_yt_thumbnail = thumbnail_image_path`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/social_upload_service.py backend/app/services/upload_phase.py backend/tests/test_upload_phase_thumbnail.py
git commit -m "feat: YouTube custom thumbnail via thumbnails.set (non-fatal)"
```

---

### Task 7: Facebook thumbnail — immediate path + scheduled note

**Files:**
- Modify: `backend/app/services/social_upload_service.py` (`upload_facebook` ~line 1553; new helper `_set_facebook_video_thumbnail`; scheduled-branch note ~line 1601; immediate-path call ~line 1768)
- Modify: `backend/app/services/upload_phase.py` (pass `thumbnail_image_path` in both `upload_facebook` lambdas ~lines 1129, 1148)
- Test: `backend/tests/test_upload_phase_thumbnail.py` (extend)

**Interfaces:**
- Consumes: `thumbnail_image_path` local from Task 6.
- Produces: `upload_facebook(..., thumbnail_image_path: Path | None = None)`; `SocialUploadService._set_facebook_video_thumbnail(*, session, base: str, video_id: str, token: str, image_path: Path, deadline: float | None) -> str | None` (None on success, French warning on failure).

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_upload_phase_thumbnail.py`)

```python
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.posts: list[dict] = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse(self.status_code, {"error": {"message": "denied"}})


def test_set_facebook_thumbnail_success(tmp_path):
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    session = _FakeSession(200)
    warning = SocialUploadService._set_facebook_video_thumbnail(
        session=session,
        base="https://graph.facebook.com/v25.0",
        video_id="v1",
        token="tok",
        image_path=image,
        deadline=None,
    )
    assert warning is None
    assert session.posts[0]["url"] == "https://graph.facebook.com/v25.0/v1/thumbnails"
    assert session.posts[0]["data"]["is_preferred"] == "true"


def test_set_facebook_thumbnail_failure_returns_warning(tmp_path):
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_facebook_video_thumbnail(
        session=_FakeSession(400),
        base="https://graph.facebook.com/v25.0",
        video_id="v1",
        token="tok",
        image_path=image,
        deadline=None,
    )
    assert warning is not None
    assert "Miniature Facebook" in warning
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py -v`
Expected: new tests FAIL — no `_set_facebook_video_thumbnail`

- [ ] **Step 3: Implement**

Helper in `SocialUploadService`:

```python
    @classmethod
    def _set_facebook_video_thumbnail(
        cls,
        *,
        session,
        base: str,
        video_id: str,
        token: str,
        image_path: Path,
        deadline: float | None,
    ) -> str | None:
        """Best-effort custom thumbnail on the classic /videos upload path."""
        try:
            with open(image_path, "rb") as fh:
                resp = session.post(
                    f"{base}/{video_id}/thumbnails",
                    data={"access_token": token, "is_preferred": "true"},
                    files={"source": ("thumbnail.jpg", fh, "image/jpeg")},
                    timeout=cls._request_timeout_seconds(
                        deadline=deadline,
                        platform="Facebook",
                        operation="thumbnail upload",
                    ),
                )
            if resp.status_code >= 400:
                return f"Miniature Facebook non appliquée: {_extract_graph_error(resp)}"
            return None
        except Exception as exc:
            return f"Miniature Facebook non appliquée: {exc}"
```

`upload_facebook`: add keyword `thumbnail_image_path: Path | None = None` (after `facebook_prep_dir`).

Scheduled branch (~line 1601): keep the delegation, but annotate the result:

```python
        if scheduled_at:
            result = cls._upload_facebook_reel_scheduled(
                ...existing args unchanged...
            )
            if thumbnail_image_path is not None and result.status == "uploaded":
                note = "Miniature personnalisée non supportée pour les Reels programmés"
                result.detail = f"{result.detail}; {note}" if result.detail else note
            return result
```

(`PlatformUploadResult` is a plain mutable dataclass — verify in this file; if frozen, rebuild via `dataclasses.replace(result, detail=...)`.)

Immediate path: just before the final success return (~line 1768), after the caption check:

```python
            thumbnail_warning: str | None = None
            if thumbnail_image_path is not None and thumbnail_image_path.exists():
                with cls._create_upload_session() as session:
                    thumbnail_warning = cls._set_facebook_video_thumbnail(
                        session=session,
                        base=base,
                        video_id=video_id,
                        token=token,
                        image_path=thumbnail_image_path,
                        deadline=deadline,
                    )

            detail_parts = []
            if source_mode == "drive_url":
                detail_parts.append("Uploaded via Drive URL ingestion")
            if thumbnail_warning:
                detail_parts.append(thumbnail_warning)
            return PlatformUploadResult(
                platform="facebook",
                status="uploaded",
                url=f"https://www.facebook.com/{video_id}",
                resource_id=video_id,
                detail="; ".join(detail_parts) if detail_parts else None,
            )
```

In `execute_upload` (`upload_phase.py`), pass `thumbnail_image_path=thumbnail_image_path` in both `upload_facebook` lambdas (capture into `_fb_thumbnail` locals first, matching the `_fb_strategy` pattern).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/social_upload_service.py backend/app/services/upload_phase.py backend/tests/test_upload_phase_thumbnail.py
git commit -m "feat: Facebook custom thumbnail on immediate uploads (non-fatal)"
```

---

### Task 8: Frontend — types, API client, hook, ThumbnailSelectionModal

**Files:**
- Modify: `frontend/src/types/index.ts` (near `FacebookCheckResult`, ~line 277)
- Modify: `frontend/src/api/client.ts` (`runProjectUpload` ~line 233; new endpoints near `getUploadSourceStatus` ~line 291)
- Create: `frontend/src/hooks/useThumbnailCandidates.ts`
- Create: `frontend/src/components/project-manager/ThumbnailSelectionModal.tsx`

**Interfaces:**
- Consumes: Task 2's endpoints.
- Produces (used by Task 9):
  - Types `ThumbnailCandidate { index; label; timestamp_ms; image_url }`, `ThumbnailCandidatesResult { state; detail?; version?; candidates? }`.
  - `api.getThumbnailCandidates(projectId)` (image_url mapped to an absolute URL), `api.runProjectUpload(..., thumbnailTimestampMs?: number | null)`.
  - `useThumbnailCandidates(projectId: string, active: boolean) -> { status: "loading"|"ready"|"error"; candidates: ThumbnailCandidate[]; detail?: string }`.
  - `<ThumbnailSelectionModal open stacked? projectId projectTitle? onChoice={(timestampMs: number | null) => void} />` — X button and error-state button both resolve through `onChoice` (default candidate, or `null`), never a cancel.

- [ ] **Step 1: Add types** (`frontend/src/types/index.ts`)

```ts
export interface ThumbnailCandidate {
  index: number;
  label: string;
  timestamp_ms: number;
  image_url: string;
}

export interface ThumbnailCandidatesResult {
  state: "ready" | "in_progress" | "error" | "missing";
  detail?: string;
  version?: string;
  candidates?: ThumbnailCandidate[];
}
```

- [ ] **Step 2: Extend the API client** (`frontend/src/api/client.ts`)

`runProjectUpload` gains a trailing param and body field:

```ts
  runProjectUpload: (
    projectId: string,
    accountId?: string,
    facebookStrategy?: string,
    instagramStrategy?: string,
    youtubeStrategy?: string,
    copyrightAudioPath?: string,
    thumbnailTimestampMs?: number | null,
  ) =>
    request<import("@/types").ProjectUploadJob>(
      `/project-manager/projects/${projectId}/upload`,
      {
        method: "POST",
        body: JSON.stringify({
          account_id: accountId ?? null,
          facebook_strategy: facebookStrategy ?? null,
          instagram_strategy: instagramStrategy ?? null,
          youtube_strategy: youtubeStrategy ?? null,
          copyright_audio_path: copyrightAudioPath ?? null,
          thumbnail_timestamp_ms: thumbnailTimestampMs ?? null,
        }),
      },
    ),
```

New endpoint (near `getUploadSourceStatus`) — map `image_url` to absolute so components can drop it straight into `<img src>`:

```ts
  getThumbnailCandidates: async (projectId: string) => {
    const result = await request<import("@/types").ThumbnailCandidatesResult>(
      `/project-manager/projects/${projectId}/thumbnail-candidates`,
    );
    if (result.candidates) {
      result.candidates = result.candidates.map((c) => ({
        ...c,
        image_url: `${API_BASE}${c.image_url}`,
      }));
    }
    return result;
  },
```

- [ ] **Step 3: Create the polling hook** (`frontend/src/hooks/useThumbnailCandidates.ts`, modeled on `useUploadSourcePreview.ts`)

```ts
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ThumbnailCandidate } from "@/types";

type ThumbnailCandidatesStatus = "loading" | "ready" | "error";

/**
 * Polls the thumbnail-candidates endpoint until frames are extracted.
 * The endpoint warms the shared upload_source cache on first call, so
 * mounting this hook is enough to trigger the whole pipeline.
 */
export function useThumbnailCandidates(projectId: string, active: boolean) {
  const [status, setStatus] = useState<ThumbnailCandidatesStatus>("loading");
  const [candidates, setCandidates] = useState<ThumbnailCandidate[]>([]);
  const [detail, setDetail] = useState<string>();

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await api.getThumbnailCandidates(projectId);
        if (cancelled) return;
        if (result.state === "ready" && result.candidates?.length) {
          setCandidates(result.candidates);
          setStatus("ready");
          return;
        }
        if (result.state === "error") {
          setDetail(result.detail);
          setStatus("error");
          return;
        }
        // "in_progress" / "missing": keep polling, the backend is warming up.
      } catch {
        // transient network error: keep polling
      }
      if (!cancelled) {
        timer = window.setTimeout(poll, 2000);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [projectId, active]);

  return { status, candidates, detail };
}
```

- [ ] **Step 4: Create the modal** (`frontend/src/components/project-manager/ThumbnailSelectionModal.tsx`, following `FacebookDurationModal.tsx`'s card/stacked/AnimatePresence structure)

```tsx
import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Check, Image as ImageIcon, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui";
import { useThumbnailCandidates } from "@/hooks/useThumbnailCandidates";

interface ThumbnailSelectionModalProps {
  open: boolean;
  projectId: string;
  projectTitle?: string | null;
  /** Resolves the step: a candidate timestamp (ms) or null for "no thumbnail". */
  onChoice: (timestampMs: number | null) => void;
  stacked?: boolean;
}

export function ThumbnailSelectionModal({
  open,
  projectId,
  projectTitle,
  onChoice,
  stacked = false,
}: ThumbnailSelectionModalProps) {
  const { status, candidates, detail } = useThumbnailCandidates(projectId, open);
  const [selectedIndex, setSelectedIndex] = useState(0);

  const selected =
    candidates.find((c) => c.index === selectedIndex) ?? candidates[0];
  const defaultTimestampMs = candidates[0]?.timestamp_ms ?? null;

  // Skip/close always falls back to the default candidate: the upload never
  // blocks on this step (approved design decision).
  const resolveWithDefault = () => onChoice(selected?.timestamp_ms ?? defaultTimestampMs);

  useEffect(() => {
    if (!open || stacked) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        resolveWithDefault();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  });

  if (!open) {
    return null;
  }

  const card = (
    <motion.div
      className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
      style={{ maxWidth: "64rem", width: "100%" }}
      initial={{ scale: 0.95, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      exit={{ scale: 0.95, opacity: 0 }}
      transition={{ duration: 0.2 }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">Choisir la miniature</h3>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1 font-mono">
            {projectTitle || "Projet"} · {projectId}
          </p>
          <p className="text-sm text-[hsl(var(--muted-foreground))] mt-2">
            Appliquée sur TikTok, Instagram, YouTube et Facebook (selon support).
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={resolveWithDefault}
          className="shrink-0"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {status === "loading" && (
        <div className="flex flex-col items-center justify-center gap-2 py-16 text-[hsl(var(--muted-foreground))]">
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="text-xs">Extraction des miniatures...</span>
        </div>
      )}

      {status === "error" && (
        <div className="flex flex-col items-center justify-center gap-3 py-12 text-[hsl(var(--muted-foreground))]">
          <ImageIcon className="h-6 w-6" />
          <span className="text-sm">
            Miniatures indisponibles{detail ? ` : ${detail}` : ""}
          </span>
          <Button size="sm" onClick={() => onChoice(null)}>
            Continuer sans miniature
          </Button>
        </div>
      )}

      {status === "ready" && (
        <>
          <div className="grid grid-cols-5 gap-3">
            {candidates.map((candidate) => (
              <button
                key={candidate.index}
                type="button"
                onClick={() => setSelectedIndex(candidate.index)}
                className={`relative rounded-lg overflow-hidden aspect-9/16 bg-black border-2 transition-colors ${
                  selected?.index === candidate.index
                    ? "border-[hsl(var(--primary))]"
                    : "border-transparent hover:border-[hsl(var(--border))]"
                }`}
              >
                <img
                  src={candidate.image_url}
                  alt={candidate.label}
                  className="w-full h-full object-cover"
                />
                {selected?.index === candidate.index && (
                  <div className="absolute top-1.5 right-1.5 bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] rounded-full p-1">
                    <Check className="h-3 w-3" />
                  </div>
                )}
                <div className="absolute bottom-0 inset-x-0 bg-black/70 text-white text-[10px] px-1.5 py-1 text-center">
                  {candidate.label}
                </div>
              </button>
            ))}
          </div>
          <div className="flex items-center justify-center gap-3 pt-1">
            <Button
              size="sm"
              className="active:scale-95 transition-transform"
              onClick={() => onChoice(selected?.timestamp_ms ?? null)}
            >
              <Check className="h-4 w-4 mr-1.5" />
              Utiliser cette miniature
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="active:scale-95 transition-transform text-[hsl(var(--muted-foreground))]"
              onClick={() => onChoice(null)}
            >
              Continuer sans miniature
            </Button>
          </div>
        </>
      )}
    </motion.div>
  );

  if (stacked) {
    return <div className="w-full max-w-5xl">{card}</div>;
  }

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={resolveWithDefault}
      >
        {card}
      </motion.div>
    </AnimatePresence>
  );
}
```

Note: projects with fewer than 3 scenes return fewer than 5 candidates. Make the grid class conditional: `candidates.length >= 4 ? "grid-cols-5" : "grid-cols-3"` in place of the literal `grid-cols-5`.

- [ ] **Step 5: Type-check**

Run: `cd frontend && npx tsc -b`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/hooks/useThumbnailCandidates.ts frontend/src/components/project-manager/ThumbnailSelectionModal.tsx
git commit -m "feat: thumbnail selection modal, hook, and API client support"
```

---

### Task 9: Wire the modal into the upload session machine

**Files:**
- Modify: `frontend/src/components/project-manager/ProjectManagerModal.tsx` (context ~line 42, status union ~line 51, `isPromptSessionStatus` ~line 99, `uploadButtonLabelForSession` ~line 108, `enqueueUpload` ~line 447, `continueUploadAfterFacebook` ~line 525, YouTube modal `onChoice` ~line 1237, render block ~line 1244)

**Interfaces:**
- Consumes: `ThumbnailSelectionModal`, `api.runProjectUpload` 7th param (Task 8).
- Produces: session status `"awaiting_thumbnail_choice"`; `PendingUploadContext.thumbnailTimestampMs?: number | null`.

- [ ] **Step 1: Extend the state machine types**

```ts
interface PendingUploadContext {
  projectId: string;
  accountId?: string;
  facebookStrategy?: UploadDurationStrategy;
  instagramStrategy?: UploadDurationStrategy;
  youtubeStrategy?: UploadDurationStrategy;
  copyrightAudioPath?: string;
  thumbnailTimestampMs?: number | null;
}

type UploadSessionStatus =
  | "checking_copyright"
  | "awaiting_copyright_music"
  | "awaiting_copyright_warning"
  | "checking_facebook"
  | "awaiting_facebook_choice"
  | "checking_youtube"
  | "awaiting_youtube_choice"
  | "awaiting_thumbnail_choice"
  | "enqueueing";
```

Add `status === "awaiting_thumbnail_choice"` to `isPromptSessionStatus`, and a `case "awaiting_thumbnail_choice":` to the `"Confirm"` group in `uploadButtonLabelForSession`.

- [ ] **Step 2: Add the thumbnail step callback and reroute the two `enqueueUpload` call sites**

New callback (after `enqueueUpload`, before `continueUploadAfterFacebook`):

```ts
  const continueUploadToThumbnail = useCallback(
    (context: PendingUploadContext, token: string) => {
      patchUploadSession(context.projectId, token, {
        context,
        status: "awaiting_thumbnail_choice",
        message: null,
        youtubeResult: undefined,
      });
    },
    [patchUploadSession],
  );
```

In `continueUploadAfterFacebook`, replace `await enqueueUpload(context, token);` (line ~525) with `continueUploadToThumbnail(context, token);` and swap the dependency array entry `enqueueUpload` → `continueUploadToThumbnail`.

In the `YouTubeDurationModal` render block (~line 1237), replace `void enqueueUpload(nextContext, session.token);` with `continueUploadToThumbnail(nextContext, session.token);`.

Update `enqueueUpload` to forward the timestamp:

```ts
        const job = await api.runProjectUpload(
          context.projectId,
          context.accountId,
          context.facebookStrategy,
          context.instagramStrategy,
          context.youtubeStrategy,
          context.copyrightAudioPath,
          context.thumbnailTimestampMs,
        );
```

- [ ] **Step 3: Render the modal**

Import `ThumbnailSelectionModal` at the top, and after the `awaiting_youtube_choice` block (~line 1244):

```tsx
                    if (session.status === "awaiting_thumbnail_choice") {
                      return (
                        <ThumbnailSelectionModal
                          key={`${session.context.projectId}:${session.token}:thumbnail`}
                          open
                          stacked
                          projectId={session.context.projectId}
                          projectTitle={projectTitle}
                          onChoice={(timestampMs) => {
                            void enqueueUpload(
                              {
                                ...session.context,
                                thumbnailTimestampMs: timestampMs,
                              },
                              session.token,
                            );
                          }}
                        />
                      );
                    }
```

- [ ] **Step 4: Type-check and lint**

Run: `cd frontend && npx tsc -b && npx eslint src/components/project-manager/ProjectManagerModal.tsx src/components/project-manager/ThumbnailSelectionModal.tsx src/hooks/useThumbnailCandidates.ts`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/project-manager/ProjectManagerModal.tsx
git commit -m "feat: thumbnail selection step in the upload session flow"
```

---

### Task 10: Full verification sweep

**Files:** none new — verification only.

- [ ] **Step 1: Backend touched-file suite**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py backend/tests/test_thumbnail_routes.py backend/tests/test_upload_phase_thumbnail.py backend/tests/test_upload_phase_tiktok.py backend/tests/test_upload_source_routes.py backend/tests/test_upload_source_cache.py backend/tests/test_instagram_drive_preparation.py backend/tests/test_upload_phase_local_source.py -v`
Expected: all PASS

- [ ] **Step 2: Server suite**

Run: `cd server && .venv/bin/python -m pytest tests/ -v`
Expected: all PASS (this suite has no known pre-existing failures; investigate any red)

- [ ] **Step 3: Frontend build**

Run: `cd frontend && npm run build`
Expected: clean `tsc -b && vite build`

- [ ] **Step 4: Commit any fixes, then summarize**

Report to the owner: feature complete; owner E2E pending (real upload with a TikTok business account to verify the PFM cover, plus YouTube/Facebook/Instagram visual checks). Remind: VPS must be redeployed for the server-side changes to take effect.

---

## Out of scope (per spec)

- Custom (non-frame) cover images or text overlays.
- Facebook scheduled/Reels covers (no API support).
- Per-platform distinct thumbnails.
- Changing thumbnails after publish.
