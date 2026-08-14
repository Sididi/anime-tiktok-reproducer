# Thumbnail Selection v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the shipped thumbnail feature to 11 scene-anchored candidates extracted as clean frames from source episodes (local → Drive range-fetch → output fallback), composed into 1080×1920 blurred-extend covers, delivered as images to every image-capable platform, with a progressive modal and a floating download-progress card.

**Architecture:** `ThumbnailService` gains dual-coordinate candidates (output timestamp + source episode coordinate from `matches.json`), a three-step extraction ladder, PIL blurred-extend composition, and a background progressive builder with `meta.json` state. Upload side resolves the chosen candidate's composed cover, hosts it on Drive for VPS-published platforms (IG `cover_url`, PFM `thumbnail_url`), and keeps timestamps as fallbacks. Spec: `docs/superpowers/specs/2026-08-15-thumbnail-selection-v2-design.md` (note: the spec says source shift `0.05 × speed_ratio`; the correct mapping is `0.05 / speed_ratio` because `speed_ratio = output_duration / source_duration` — this plan is authoritative).

**Tech Stack:** FastAPI + Pydantic, OpenCV (`AnimeMatcherService`), PIL, ffmpeg via `subprocess_runner.run_command` (HTTP range-reads), Google Drive API, React 19 + zustand + framer-motion, httpx (VPS).

## Global Constraints

- All user-facing strings French. New labels: "Scène N · début/milieu/fin", "Dernière scène · fin", "Avant-dernière scène · fin", "Avant-avant-dernière scène · fin"; fallback badge "aperçu sortie".
- Every thumbnail/cover failure is non-fatal; degradation ladder: clean local → clean Drive → output frame → (platform) timestamp → nothing, each step logged.
- Cover composition: 1080×1920 JPEG quality 90; blurred-extend (foreground full-width centered, background self-fill gaussian-blurred and darkened).
- Backend tests: repo root `pixi run -e dev pytest backend/tests/<file> -v`; NEVER two pytest runs concurrently. Server tests: `cd server && .venv/bin/python -m pytest tests/<file> -v`. Frontend: `cd frontend && npx tsc -b`.
- Default candidate remains index 0 (Scène 1 · début); frontend default-selection rule: candidate 0 when ready, else lowest-index ready tile, until the user clicks.
- VPS changes (Task 8) require a VPS redeploy — say so in the final report.
- Commit after every task.

---

### Task 1: Candidate model v2 — scene-anchored set with dual coordinates

**Files:**
- Modify: `backend/app/services/thumbnail_service.py` (`ThumbnailCandidate` ~line 25, `compute_candidates` ~line 67)
- Test: `backend/tests/test_thumbnail_service.py` (extend)

**Interfaces:**
- Consumes: `Transcription` (`backend/app/models/transcription.py`), `MatchList`/`SceneMatch` (`backend/app/models/match.py:27-57`: `scene_index, episode: str, start_time, end_time, speed_ratio` — times in the SOURCE episode).
- Produces (used by Tasks 2-4, 7): `ThumbnailCandidate` frozen dataclass with fields `index: int`, `label: str`, `timestamp_seconds: float` (output timeline), `scene_index: int`, `position: str` ("start"|"mid"|"end"), `episode: str | None`, `source_timestamp_seconds: float | None`; property `timestamp_ms`. `ThumbnailService.compute_candidates(transcription, matches: MatchList | None) -> list[ThumbnailCandidate]`.

- [ ] **Step 1: Write the failing tests** (replace the existing `compute_candidates` tests in `backend/tests/test_thumbnail_service.py`; keep the file's other tests)

```python
from app.models.match import MatchList, SceneMatch


def _matches(specs: list[tuple[int, str, float, float, float]]) -> MatchList:
    return MatchList(matches=[
        SceneMatch(
            scene_index=i, episode=ep, start_time=s, end_time=e,
            confidence=1.0, speed_ratio=r,
        )
        for i, ep, s, e, r in specs
    ])


def test_eleven_candidates_on_long_video():
    bounds = [(float(i), float(i) + 2.0) for i in range(9)]  # 9 scenes of 2s
    tr = _transcription(bounds)
    cands = ThumbnailService.compute_candidates(tr, None)
    assert len(cands) == 11
    assert [(c.scene_index, c.position) for c in cands] == [
        (0, "start"), (0, "mid"), (0, "end"),
        (1, "start"), (2, "start"), (3, "start"), (4, "start"), (5, "start"),
        (8, "end"), (7, "end"), (6, "end"),
    ]
    assert cands[0].label == "Scène 1 · début"
    assert cands[3].label == "Scène 2 · début"
    assert cands[8].label == "Dernière scène · fin"
    assert cands[9].label == "Avant-dernière scène · fin"
    assert cands[10].label == "Avant-avant-dernière scène · fin"
    assert [c.index for c in cands] == list(range(11))


def test_dedupe_on_short_videos():
    # 1 scene: only the scene-1 triple survives (last-scene ends dedupe onto (0,"end"))
    tr1 = _transcription([(0.0, 4.0)])
    assert [(c.scene_index, c.position) for c in ThumbnailService.compute_candidates(tr1, None)] == [
        (0, "start"), (0, "mid"), (0, "end"),
    ]
    # 4 scenes: starts of 1-4, ends of scenes 4,3,2
    tr4 = _transcription([(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0)])
    pairs = [(c.scene_index, c.position) for c in ThumbnailService.compute_candidates(tr4, None)]
    assert pairs == [
        (0, "start"), (0, "mid"), (0, "end"),
        (1, "start"), (2, "start"), (3, "start"),
        (3, "end"), (2, "end"), (1, "end"),
    ]


def test_source_coordinates_mapped_with_speed_ratio():
    tr = _transcription([(0.0, 2.0), (2.0, 4.0)])
    # scene 0: source 100..104 at speed_ratio 0.5 (output 2s from source 4s)
    matches = _matches([(0, "ep1.mkv", 100.0, 104.0, 0.5), (1, "ep2.mkv", 50.0, 52.0, 1.0)])
    cands = ThumbnailService.compute_candidates(tr, matches)
    by_key = {(c.scene_index, c.position): c for c in cands}
    shift_src = 0.05 / 0.5  # output shift / speed_ratio = 0.1s in source time
    assert by_key[(0, "start")].episode == "ep1.mkv"
    assert by_key[(0, "start")].source_timestamp_seconds == pytest.approx(100.0 + shift_src)
    assert by_key[(0, "mid")].source_timestamp_seconds == pytest.approx(102.0)
    assert by_key[(0, "end")].source_timestamp_seconds == pytest.approx(104.0 - shift_src)
    assert by_key[(1, "start")].source_timestamp_seconds == pytest.approx(50.05)
    # output timestamps unchanged from v1 math
    assert by_key[(0, "start")].timestamp_seconds == pytest.approx(0.05)


def test_source_shift_clamps_to_source_mid_and_bad_ratio_defaults():
    tr = _transcription([(0.0, 2.0)])
    # tiny source span: start+shift and end-shift clamp to source mid
    matches = _matches([(0, "ep.mkv", 10.0, 10.06, 1.0)])
    cands = ThumbnailService.compute_candidates(tr, matches)
    by_key = {(c.scene_index, c.position): c for c in cands}
    assert by_key[(0, "start")].source_timestamp_seconds == pytest.approx(10.03)
    assert by_key[(0, "end")].source_timestamp_seconds == pytest.approx(10.03)
    # zero/negative speed_ratio treated as 1.0
    matches2 = _matches([(0, "ep.mkv", 10.0, 20.0, 0.0)])
    c2 = {(c.scene_index, c.position): c for c in ThumbnailService.compute_candidates(tr, matches2)}
    assert c2[(0, "start")].source_timestamp_seconds == pytest.approx(10.05)


def test_scene_without_match_or_empty_episode_has_no_source():
    tr = _transcription([(0.0, 2.0), (2.0, 4.0)])
    matches = _matches([(0, "", 0.0, 4.0, 1.0)])  # scene 0 empty episode, scene 1 missing
    cands = ThumbnailService.compute_candidates(tr, matches)
    for c in cands:
        assert c.episode is None
        assert c.source_timestamp_seconds is None
```

Update any pre-existing test that calls `compute_candidates(tr)` with one argument to pass `(tr, None)`, and any that asserts the v1 5-candidate shape to match the new rules (a 3-scene video now yields `(0,start)(0,mid)(0,end)(1,start)(2,start)(2,end)(1,end)` = 7).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: new tests FAIL (TypeError: compute_candidates takes 2 positional arguments / missing fields)

- [ ] **Step 3: Implement**

Replace `ThumbnailCandidate` and `compute_candidates`:

```python
@dataclass(frozen=True)
class ThumbnailCandidate:
    index: int
    label: str
    timestamp_seconds: float
    scene_index: int
    position: str  # "start" | "mid" | "end"
    episode: str | None = None
    source_timestamp_seconds: float | None = None

    @property
    def timestamp_ms(self) -> int:
        return int(round(self.timestamp_seconds * 1000))
```

```python
    _LAST_END_LABELS = (
        "Dernière scène · fin",
        "Avant-dernière scène · fin",
        "Avant-avant-dernière scène · fin",
    )

    @classmethod
    def compute_candidates(
        cls,
        transcription: Transcription,
        matches: "MatchList | None",
    ) -> list[ThumbnailCandidate]:
        scenes = [s for s in transcription.scenes if s.end_time > s.start_time]
        if not scenes:
            return []
        shift = cls._SHIFT_SECONDS

        def output_ts(scene, position: str) -> float:
            mid = (scene.start_time + scene.end_time) / 2
            if position == "start":
                return min(scene.start_time + shift, mid)
            if position == "end":
                return max(scene.end_time - shift, mid)
            return mid

        match_by_scene: dict[int, "SceneMatch"] = {}
        if matches is not None:
            for match in matches.matches:
                if match.episode:
                    match_by_scene[match.scene_index] = match

        def source_coord(scene, position: str) -> tuple[str | None, float | None]:
            match = match_by_scene.get(scene.scene_index)
            if match is None or match.end_time <= match.start_time:
                return None, None
            ratio = match.speed_ratio if match.speed_ratio and match.speed_ratio > 0 else 1.0
            src_shift = shift / ratio
            src_mid = (match.start_time + match.end_time) / 2
            if position == "start":
                return match.episode, min(match.start_time + src_shift, src_mid)
            if position == "end":
                return match.episode, max(match.end_time - src_shift, src_mid)
            return match.episode, src_mid

        spots: list[tuple[object, str, str]] = [
            (scenes[0], "start", "Scène 1 · début"),
            (scenes[0], "mid", "Scène 1 · milieu"),
            (scenes[0], "end", "Scène 1 · fin"),
        ]
        for ordinal, scene in enumerate(scenes[1:6], start=2):
            spots.append((scene, "start", f"Scène {ordinal} · début"))
        for offset, label in enumerate(cls._LAST_END_LABELS):
            pos = len(scenes) - 1 - offset
            if pos < 0:
                break
            spots.append((scenes[pos], "end", label))

        candidates: list[ThumbnailCandidate] = []
        seen: set[tuple[int, str]] = set()
        for scene, position, label in spots:
            key = (scene.scene_index, position)
            if key in seen:
                continue
            seen.add(key)
            episode, src_ts = source_coord(scene, position)
            candidates.append(ThumbnailCandidate(
                index=len(candidates),
                label=label,
                timestamp_seconds=round(output_ts(scene, position), 3),
                scene_index=scene.scene_index,
                position=position,
                episode=episode,
                source_timestamp_seconds=round(src_ts, 3) if src_ts is not None else None,
            ))
        return candidates
```

Import `MatchList`, `SceneMatch` from `..models.match` (TYPE_CHECKING import is fine; runtime import is simpler and cheap). Update the single production caller (`build_candidates_payload`) to pass `ProjectService.load_matches(project_id)` — that wiring is completed in Task 4; for this task just change the call to `compute_candidates(transcription, ProjectService.load_matches(project_id))` (import `ProjectService` from `.project_service`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/thumbnail_service.py backend/tests/test_thumbnail_service.py
git commit -m "feat: scene-anchored 11-candidate thumbnail set with source coordinates"
```

---

### Task 2: Local clean-frame extraction + blurred-extend composition

**Files:**
- Modify: `backend/app/services/thumbnail_service.py`
- Test: `backend/tests/test_thumbnail_service.py` (extend)

**Interfaces:**
- Consumes: `AnimeLibraryService.resolve_episode_path(episode_name, *, library_type=None) -> Path | None` (`backend/app/services/anime_library.py:880-916` — handles absolute paths, library-relative names, manifest stems), `AnimeMatcherService.extract_frame`, PIL.
- Produces (used by Tasks 3-4, 7):
  - `ThumbnailService.compose_vertical_cover(image: Image.Image) -> Image.Image` — 1080×1920 blurred-extend; near-9:16 inputs are just resized.
  - `ThumbnailService._resolve_local_episode(episode: str, library_type) -> Path | None`
  - `ThumbnailService._extract_local_clean_frame(candidate: ThumbnailCandidate, library_type) -> Image.Image | None`

- [ ] **Step 1: Write the failing tests**

```python
def test_compose_vertical_cover_landscape_blurred_extend():
    frame = Image.new("RGB", (1920, 1080), (200, 30, 30))
    # paint a bright column so we can verify the foreground band placement
    for x in range(900, 1020):
        for y in range(0, 1080, 7):
            frame.putpixel((x, y), (0, 255, 0))
    cover = ThumbnailService.compose_vertical_cover(frame)
    assert cover.size == (1080, 1920)
    # foreground band: full-width 16:9 => height 607/608, vertically centered
    import numpy as np
    arr = np.asarray(cover)
    center_band = arr[930:990, :, :]
    top_band = arr[0:200, :, :]
    # background is darkened: top band strictly darker on average than center band
    assert float(top_band.mean()) < float(center_band.mean())


def test_compose_vertical_cover_portrait_passthrough_resize():
    frame = Image.new("RGB", (540, 960), (10, 10, 200))
    cover = ThumbnailService.compose_vertical_cover(frame)
    assert cover.size == (1080, 1920)


def test_extract_local_clean_frame_resolves_and_composes(monkeypatch, tmp_path):
    episode = tmp_path / "ep1.mkv"
    episode.write_bytes(b"\x00")
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeLibraryService.resolve_episode_path",
        classmethod(lambda cls, name, manifest=None, library_type=None: episode),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frame",
        classmethod(lambda cls, path, ts: Image.new("RGB", (1920, 1080), (5, 5, 5))),
    )
    cand = ThumbnailCandidate(
        index=0, label="Scène 1 · début", timestamp_seconds=0.05,
        scene_index=0, position="start",
        episode="ep1.mkv", source_timestamp_seconds=100.05,
    )
    frame = ThumbnailService._extract_local_clean_frame(cand, None)
    assert frame is not None and frame.size == (1920, 1080)


def test_extract_local_clean_frame_none_without_source_or_file(monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeLibraryService.resolve_episode_path",
        classmethod(lambda cls, name, manifest=None, library_type=None: None),
    )
    no_source = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
    )
    assert ThumbnailService._extract_local_clean_frame(no_source, None) is None
    with_source = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="gone.mkv", source_timestamp_seconds=1.0,
    )
    assert ThumbnailService._extract_local_clean_frame(with_source, None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: new tests FAIL (missing methods)

- [ ] **Step 3: Implement**

```python
    _COVER_SIZE = (1080, 1920)

    @classmethod
    def compose_vertical_cover(cls, image: Image.Image) -> Image.Image:
        """1080×1920 blurred-extend: frame full-width centered over a blurred,
        darkened self-fill background (the rendered videos' look, minus text)."""
        from PIL import ImageEnhance, ImageFilter, ImageOps  # local: PIL submodules

        target_w, target_h = cls._COVER_SIZE
        src = image.convert("RGB")
        if src.height >= src.width:  # already portrait-ish: plain fit
            return ImageOps.fit(src, cls._COVER_SIZE, Image.LANCZOS)
        background = ImageOps.fit(src, cls._COVER_SIZE, Image.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=40))
        background = ImageEnhance.Brightness(background).enhance(0.45)
        fg_h = int(round(target_w * src.height / src.width))
        foreground = src.resize((target_w, fg_h), Image.LANCZOS)
        background.paste(foreground, (0, (target_h - fg_h) // 2))
        return background

    @classmethod
    def _resolve_local_episode(cls, episode: str, library_type) -> Path | None:
        try:
            resolved = AnimeLibraryService.resolve_episode_path(
                episode, library_type=library_type
            )
        except Exception:
            logger.warning("Episode resolution failed for %r", episode, exc_info=True)
            return None
        if resolved is not None and resolved.exists():
            return resolved
        return None

    @classmethod
    def _extract_local_clean_frame(
        cls, candidate: ThumbnailCandidate, library_type
    ) -> Image.Image | None:
        if not candidate.episode or candidate.source_timestamp_seconds is None:
            return None
        path = cls._resolve_local_episode(candidate.episode, library_type)
        if path is None:
            return None
        try:
            return AnimeMatcherService.extract_frame(
                path, candidate.source_timestamp_seconds
            )
        except Exception:
            logger.warning(
                "Local clean-frame extraction failed: %s t=%.3f",
                path, candidate.source_timestamp_seconds, exc_info=True,
            )
            return None
```

Import `AnimeLibraryService` from `.anime_library` at module top (it is heavy-ish but already imported transitively in the backend app; if importing at module scope creates a cycle, import inside `_resolve_local_episode`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/thumbnail_service.py backend/tests/test_thumbnail_service.py
git commit -m "feat: local clean-frame extraction and blurred-extend cover composition"
```

---

### Task 3: Drive `sources/` single-frame range-fetch

**Files:**
- Modify: `backend/app/services/thumbnail_service.py`
- Modify: `backend/app/services/google_drive_service.py` (one small helper)
- Test: `backend/tests/test_thumbnail_service.py` (extend)

**Interfaces:**
- Consumes: `GoogleDriveService.credentials().token` (`google_drive_service.py:150-152`), `GoogleDriveService.list_children_named(folder_id, filename)` (`:573-583`), `Project.drive_folder_id` (project model), `run_command` (`backend/app/utils/subprocess_runner.py:83` — resolves `"ffmpeg"` internally, sanitized env), `CommandResult` (`returncode` field — verify exact attribute names in subprocess_runner.py before coding).
- Produces (used by Task 4):
  - `GoogleDriveService.find_subfolder(parent_id: str, name: str) -> str | None` — folder-mimeType exact-name query (mirror `ensure_subfolder`'s query at `:745-758` WITHOUT the create step).
  - `ThumbnailService._extract_drive_clean_frame(candidate, drive_folder_id: str) -> Image.Image | None` — resolves `sources/<basename>` file id, ffmpeg range-fetches one frame to a temp JPEG, returns PIL image.

- [ ] **Step 1: Write the failing tests**

```python
def test_drive_clean_frame_builds_range_fetch_command(monkeypatch, tmp_path):
    from app.services.thumbnail_service import ThumbnailService

    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.find_subfolder",
        classmethod(lambda cls, parent, name: "srcfolder" if name == "sources" else None),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.list_children_named",
        classmethod(lambda cls, folder_id, filename, drive=None: [{"id": "fid123"}]),
    )

    class _Creds:
        token = "tok-abc"

    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.credentials",
        classmethod(lambda cls: _Creds()),
    )
    captured: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # produce the output file the command would have written
        out = Path(cmd[-1])
        Image.new("RGB", (1920, 1080), (1, 2, 3)).save(out, "JPEG")
        return _Result()

    monkeypatch.setattr("app.services.thumbnail_service.run_command", fake_run)
    cand = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="/library/Anime/ep1.mkv", source_timestamp_seconds=63.25,
    )
    frame = ThumbnailService._extract_drive_clean_frame(cand, "folder-1")
    assert frame is not None and frame.size == (1920, 1080)
    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "63.250"
    assert any("fid123" in part and "alt=media" in part for part in cmd)
    assert any("Bearer tok-abc" in part for part in cmd)
    assert "-frames:v" in cmd
    # episode ref reduced to basename for the Drive lookup
    # (list_children_named stub above only matches; assert via captured lookup is implicit)


def test_drive_clean_frame_none_when_missing(monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.find_subfolder",
        classmethod(lambda cls, parent, name: None),
    )
    cand = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="ep1.mkv", source_timestamp_seconds=1.0,
    )
    assert ThumbnailService._extract_drive_clean_frame(cand, "folder-1") is None


def test_drive_clean_frame_none_on_ffmpeg_failure(monkeypatch):
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.find_subfolder",
        classmethod(lambda cls, parent, name: "srcfolder"),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.list_children_named",
        classmethod(lambda cls, folder_id, filename, drive=None: [{"id": "fid123"}]),
    )

    class _Creds:
        token = "tok"

    monkeypatch.setattr(
        "app.services.thumbnail_service.GoogleDriveService.credentials",
        classmethod(lambda cls: _Creds()),
    )

    class _Fail:
        returncode = 1

    monkeypatch.setattr(
        "app.services.thumbnail_service.run_command", lambda cmd, **kw: _Fail()
    )
    cand = ThumbnailCandidate(
        index=0, label="x", timestamp_seconds=0.05, scene_index=0, position="start",
        episode="ep1.mkv", source_timestamp_seconds=1.0,
    )
    assert ThumbnailService._extract_drive_clean_frame(cand, "folder-1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: new tests FAIL (missing methods)

- [ ] **Step 3: Implement**

`google_drive_service.py` — add next to `ensure_subfolder` (~line 745), reusing its query shape minus creation:

```python
    @classmethod
    def find_subfolder(cls, parent_id: str, name: str, *, drive=None) -> str | None:
        """Return the id of an existing subfolder by exact name, else None."""
        drive = drive or cls._client()
        safe_name = name.replace("'", "\\'")
        response = drive.files().list(
            q=(
                f"'{parent_id}' in parents and trashed = false "
                f"and mimeType = 'application/vnd.google-apps.folder' "
                f"and name = '{safe_name}'"
            ),
            fields="files(id)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = response.get("files") or []
        return str(files[0]["id"]) if files else None
```

(Copy the exact `q`/kwargs conventions from `ensure_subfolder` — if it escapes names or passes drive-listing kwargs differently, mirror it.)

`thumbnail_service.py`:

```python
    _DRIVE_FETCH_TIMEOUT_SECONDS = 90

    @classmethod
    def _extract_drive_clean_frame(
        cls, candidate: ThumbnailCandidate, drive_folder_id: str
    ) -> Image.Image | None:
        """Single-frame ffmpeg range-fetch from the project's Drive sources/ bundle.

        ffmpeg seeks over HTTPS with Range requests: it reads the mp4 index
        then only the GOP around the target — a few MB, never the full file.
        """
        if not candidate.episode or candidate.source_timestamp_seconds is None:
            return None
        try:
            sources_id = GoogleDriveService.find_subfolder(drive_folder_id, "sources")
            if not sources_id:
                return None
            basename = Path(candidate.episode).name
            entries = GoogleDriveService.list_children_named(sources_id, basename)
            if not entries:
                return None
            file_id = str(entries[0]["id"])
            token = GoogleDriveService.credentials().token
            url = (
                "https://www.googleapis.com/drive/v3/files/"
                f"{file_id}?alt=media&supportsAllDrives=true"
            )
            with tempfile.TemporaryDirectory(prefix="atr-thumb-drive-") as tmp:
                out = Path(tmp) / "frame.jpg"
                cmd = [
                    "ffmpeg", "-y", "-v", "error",
                    "-headers", f"Authorization: Bearer {token}\r\n",
                    "-ss", f"{candidate.source_timestamp_seconds:.3f}",
                    "-i", url,
                    "-frames:v", "1", "-q:v", "2",
                    str(out),
                ]
                result = run_command(
                    cmd, timeout_seconds=cls._DRIVE_FETCH_TIMEOUT_SECONDS
                )
                if getattr(result, "returncode", 1) != 0 or not out.exists():
                    logger.warning(
                        "Drive frame range-fetch failed: episode=%s t=%.3f rc=%s",
                        basename, candidate.source_timestamp_seconds,
                        getattr(result, "returncode", "?"),
                    )
                    return None
                with Image.open(out) as img:
                    return img.convert("RGB").copy()
        except Exception:
            logger.warning(
                "Drive clean-frame extraction failed for %s", candidate.episode,
                exc_info=True,
            )
            return None
```

Imports: `tempfile`, `run_command` from `..utils.subprocess_runner`, `GoogleDriveService` from `.google_drive_service`. Verify `CommandResult`'s success attribute name in `subprocess_runner.py` (`returncode` assumed) and adapt.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/thumbnail_service.py backend/app/services/google_drive_service.py backend/tests/test_thumbnail_service.py
git commit -m "feat: single-frame Drive range-fetch for source episodes"
```

---

### Task 4: Progressive builder + candidates payload v2 + route

**Files:**
- Modify: `backend/app/services/thumbnail_service.py` (rewrite `build_candidates_payload` into a background progressive builder)
- Modify: `backend/app/api/routes/project_manager.py` (`thumbnail_candidates` route ~line 230)
- Test: `backend/tests/test_thumbnail_service.py`, `backend/tests/test_thumbnail_routes.py`

**Interfaces:**
- Consumes: Tasks 1-3 outputs; `ProjectService.load(project_id)` (for `library_type`, `drive_folder_id`), `ProjectService.load_matches`, `UploadPhaseService.cached_source_video` / `start_source_video_download`.
- Produces (used by Tasks 5, 7):
  - `ThumbnailService.candidates_status(project_id: str) -> dict` — snapshot from `meta.json`; shape `{"state": "in_progress"|"partial"|"ready"|"error", "version": str, "pending": int, "detail"?: str, "candidates": [{"index", "label", "timestamp_ms", "source": "clean"|"output"|"pending", "image_url"?: str}]}`.
  - `ThumbnailService.start_candidates_build(project_id: str) -> dict` — kicks the background builder when needed, returns current snapshot.
  - Cache layout: `upload_thumbs/<project>/<version>/cand_<i>.jpg` (composed 1080×1920 covers) + `meta.json` `{"version", "candidates": [{"index", "label", "timestamp_ms", "scene_index", "position", "source", "output_version"?}]}`. Version = `f"{tt_mtime_ns}-{matches_mtime_ns or 0}"` of `transcription_timing.json` / `matches.json`.
  - `cached_frame_path(project_id, index)` unchanged signature (serves composed covers now).

- [ ] **Step 1: Write the failing tests** (service level; monkeypatch the three extractors)

```python
def _v2_project(tmp_path, monkeypatch, scenes=2, with_matches=True):
    projects = tmp_path / "projects"
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects)
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    out_dir = projects / "p1" / "output"
    out_dir.mkdir(parents=True)
    scene_json = ",".join(
        f'{{"scene_index": {i}, "text": "", "words": [], "start_time": {i * 2}.0,'
        f' "end_time": {i * 2 + 2}.0, "is_raw": false}}'
        for i in range(scenes)
    )
    (out_dir / "transcription_timing.json").write_text(
        f'{{"language": "fr", "scenes": [{scene_json}]}}'
    )
    if with_matches:
        import json as _json
        from app.models.match import MatchList, SceneMatch
        ml = MatchList(matches=[
            SceneMatch(scene_index=i, episode=f"ep{i}.mkv", start_time=10.0 * i,
                       end_time=10.0 * i + 4.0, confidence=1.0, speed_ratio=1.0)
            for i in range(scenes)
        ])
        (projects / "p1" / "matches.json").write_text(ml.model_dump_json())
    (projects / "p1" / "project.json").write_text(
        '{"id": "p1", "url": "https://example.com/v", "drive_folder_id": "folder-1"}'
    )
    return projects


def test_progressive_build_clean_then_pending_fallback(tmp_path, monkeypatch):
    _v2_project(tmp_path, monkeypatch, scenes=2)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: Image.new("RGB", (1920, 1080), (9, 9, 9))
                    if cand.scene_index == 0 else None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    # output video NOT cached yet
    monkeypatch.setattr(
        "app.services.thumbnail_service.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: None),
    )
    ThumbnailService._run_candidates_build("p1")  # synchronous internal for tests
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "partial"
    by_index = {c["index"]: c for c in snap["candidates"]}
    # scene-0 candidates clean, scene-1 candidates pending (no local, no drive, no output)
    assert by_index[0]["source"] == "clean"
    assert by_index[0]["image_url"].startswith("/project-manager/projects/p1/thumbnail-frame/0")
    scene1_pending = [c for c in snap["candidates"] if c["source"] == "pending"]
    assert snap["pending"] == len(scene1_pending) > 0


def test_progressive_build_completes_with_output_fallback(tmp_path, monkeypatch):
    _v2_project(tmp_path, monkeypatch, scenes=2)
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, cand, lt: Image.new("RGB", (1920, 1080), (9, 9, 9))
                    if cand.scene_index == 0 else None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, cand, folder: None),
    )
    video = tmp_path / "output.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(
        "app.services.thumbnail_service.UploadPhaseService.cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    monkeypatch.setattr(
        "app.services.thumbnail_service.AnimeMatcherService.extract_frames",
        classmethod(lambda cls, path, ts: [Image.new("RGB", (1080, 1920)) for _ in ts]),
    )
    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "ready"
    assert snap["pending"] == 0
    sources = {c["index"]: c["source"] for c in snap["candidates"]}
    assert "output" in sources.values() and "clean" in sources.values()
    # every candidate has a served composed cover
    for c in snap["candidates"]:
        p = ThumbnailService.cached_frame_path("p1", c["index"])
        assert p is not None and p.exists()
        with Image.open(p) as img:
            assert img.size == (1080, 1920)


def test_candidates_status_error_without_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path / "thumbs")
    ThumbnailService._run_candidates_build("p1")
    snap = ThumbnailService.candidates_status("p1")
    assert snap["state"] == "error"
    assert snap["detail"]
```

Route tests (`test_thumbnail_routes.py`): replace the v1 route tests for `thumbnail-candidates` with: the route calls `UploadPhaseService.start_source_video_download` (still warms), then `ThumbnailService.start_candidates_build`, and returns its snapshot verbatim; 404 on `ValueError`. Keep the `thumbnail-frame` tests unchanged.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py backend/tests/test_thumbnail_routes.py -v`
Expected: new tests FAIL

- [ ] **Step 3: Implement**

In `thumbnail_service.py` (replacing `build_candidates_payload`; keep `cached_frame_path` scanning logic):

- `_candidates_version(project_id) -> str | None`: `transcription_timing.json` mtime_ns + `matches.json` mtime_ns (0 when absent): `f"{tt}-{mm}"`; None when the timeline file is missing.
- `meta.json` read/write helpers (`_read_meta`, `_write_meta`) — atomic write (`.tmp` + `replace`), stored beside the JPEGs in the version dir.
- `candidates_status(project_id)`: resolve version; no version → `{"state": "error", "detail": "Timeline finale introuvable (transcription_timing.json)"}`; no meta for current version → `{"state": "in_progress", "candidates": [], "pending": 0}`; else assemble from meta: candidates with `source != "pending"` get `image_url = f"/project-manager/projects/{project_id}/thumbnail-frame/{index}?v={version}"`; `pending = count(source == "pending")`; `state = "ready"` if pending == 0 else `"partial"`; include `"version"`.
- `_run_candidates_build(project_id)` (synchronous; the whole body under `cls._build_lock(project_id)`):
  1. version; None → write nothing (status reports error).
  2. If version dir + meta exist and pending == 0 → return (cache hit).
  3. Fresh build: `shutil.rmtree(project_dir)` only when the current version dir is absent (version changed); create dir.
  4. Load transcription + matches + project (`ProjectService.load` for `library_type`, `drive_folder_id`); compute candidates; write meta with all `source: "pending"`.
  5. For each candidate with a source coordinate: local extraction → else Drive extraction (only when `project.drive_folder_id`) → on success `compose_vertical_cover(frame).save(cand_<i>.jpg, "JPEG", quality=90)`, meta `source = "clean"`, meta written after EACH candidate (progressive visibility).
  6. For candidates still pending (no source coord, or clean extraction failed): if `UploadPhaseService.cached_source_video(project_id)` exists → `AnimeMatcherService.extract_frames(video, [output timestamps])` in one pass, save each (already 9:16; `compose_vertical_cover` handles resize), meta `source = "output"`. Else leave `"pending"`.
  7. Candidates whose output-fallback also failed while the video exists: drop from meta entirely (mirror v1 dropped-frame behavior).
- `start_candidates_build(project_id)`: if a build for this project is already running (class-level `_builds_in_flight: set[str]` + guard lock, mirroring `UploadPhaseService._source_downloads_in_flight` at `upload_phase.py:1391-1393`) → return `candidates_status`. If status is `ready` or `error` → return it. Else register, spawn `threading.Thread(target=_worker, daemon=True)` running `_run_candidates_build` with try/finally deregistration, return `candidates_status`.
- Import `UploadPhaseService` lazily inside methods (avoid the import cycle: `upload_phase` already imports `thumbnail_service`).

Route (`project_manager.py`):

```python
@router.get("/projects/{project_id}/thumbnail-candidates")
async def thumbnail_candidates(project_id: str):
    """Progressive thumbnail candidates; warms the output cache for fallbacks."""
    try:
        await asyncio.to_thread(
            UploadPhaseService.start_source_video_download, project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        # cache warming is best-effort here; clean tiles don't need it
        logger.warning("Source warm failed for %s", project_id, exc_info=True)
    return await asyncio.to_thread(ThumbnailService.start_candidates_build, project_id)
```

(Add a module logger to `project_manager.py` if absent.) The `thumbnail-frame` route is unchanged.

Also: the periodic re-poll after the output cache lands — the frontend keeps polling; each poll hits `start_candidates_build`, which re-runs the builder when `pending > 0` and the meta isn't complete (step 2's cache-hit check requires pending == 0), so pending fallbacks resolve on the first poll after the download completes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run -e dev pytest backend/tests/test_thumbnail_service.py backend/tests/test_thumbnail_routes.py backend/tests/test_upload_phase_thumbnail.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/thumbnail_service.py backend/app/api/routes/project_manager.py backend/tests/test_thumbnail_service.py backend/tests/test_thumbnail_routes.py
git commit -m "feat: progressive thumbnail candidate builder with clean/output/pending states"
```

---

### Task 5: Frontend — progressive modal, badges, candidate index

**Files:**
- Modify: `frontend/src/types/index.ts` (`ThumbnailCandidate`/`ThumbnailCandidatesResult` ~line 328)
- Modify: `frontend/src/api/client.ts` (`getThumbnailCandidates` ~line 302, `runProjectUpload` ~line 233)
- Modify: `frontend/src/hooks/useThumbnailCandidates.ts`
- Modify: `frontend/src/components/project-manager/ThumbnailSelectionModal.tsx`
- Modify: `frontend/src/components/project-manager/ProjectManagerModal.tsx` (context + enqueue call)

**Interfaces:**
- Consumes: Task 4's snapshot shape.
- Produces: `ThumbnailCandidate` gains `source: "clean" | "output" | "pending"` and `image_url?: string`; `ThumbnailCandidatesResult` gains `pending?: number` and state `"partial"`; `useThumbnailCandidates` returns `{status: "loading" | "partial" | "ready" | "error", candidates, detail}` (terminal on "ready"/"error", keeps polling on "partial"); modal `onChoice(timestampMs: number | null, candidateIndex: number | null)`; `runProjectUpload(..., thumbnailTimestampMs?, thumbnailCandidateIndex?)` posts `thumbnail_candidate_index`.

- [ ] **Step 1: Types + client**

```ts
export interface ThumbnailCandidate {
  index: number;
  label: string;
  timestamp_ms: number;
  source: "clean" | "output" | "pending";
  image_url?: string;
}

export interface ThumbnailCandidatesResult {
  state: "ready" | "partial" | "in_progress" | "error";
  detail?: string;
  version?: string;
  pending?: number;
  candidates?: ThumbnailCandidate[];
}
```

`getThumbnailCandidates`: map `image_url` to `${API_BASE}${c.image_url}` only when present. `runProjectUpload` gains trailing `thumbnailCandidateIndex?: number | null`, body field `thumbnail_candidate_index: thumbnailCandidateIndex ?? null`.

- [ ] **Step 2: Hook — progressive states**

Rework `useThumbnailCandidates`: on each poll, always publish `candidates` when present; `state === "ready"` with non-empty candidates → status "ready", stop; `"partial"` → status "partial", keep polling (2s); `"error"` → status "error" + detail, stop; `"in_progress"` / network errors → keep polling (status stays "loading" until first candidates arrive).

- [ ] **Step 3: Modal**

- Grid: `grid-cols-6` when `candidates.length > 6`, else the existing `>= 5 ? "grid-cols-5" : "grid-cols-3"` rule (two rows for 7-11).
- Tile states: `pending` → dimmed placeholder with a `Loader2` spinner, not selectable; `clean`/`output` → image tile; `source === "output"` additionally shows a bottom-left badge `aperçu sortie` (small `bg-amber-500/80 text-black text-[9px] px-1 rounded`).
- Selection: `selectedIndex: number | null` starts `null`; derived `selected` = user pick if that tile is ready, else candidate 0 if ready, else lowest-index ready tile. Confirm button (`Utiliser cette miniature`) enabled when `selected` exists — modal is usable in "partial" status.
- Confirm → `onChoice(selected.timestamp_ms, selected.index)`; "Continuer sans miniature" → `onChoice(null, null)`; X / Escape / backdrop → resolve with the derived `selected` (or `(null, null)` when nothing ready) — never a cancel, unchanged contract.
- While status "partial", show a one-line hint under the grid: `Certaines vignettes arrivent encore…` — the floating card (Task 6) carries the byte progress.

- [ ] **Step 4: ProjectManagerModal wiring**

`PendingUploadContext` gains `thumbnailCandidateIndex?: number | null`; the thumbnail render block's `onChoice={(timestampMs, candidateIndex) => ...}` spreads both into the context; `enqueueUpload` passes `context.thumbnailCandidateIndex` as the new `runProjectUpload` argument.

- [ ] **Step 5: Verify**

Run: `cd frontend && npx tsc -b && npx eslint src/components/project-manager/ThumbnailSelectionModal.tsx src/hooks/useThumbnailCandidates.ts src/api/client.ts`
Expected: clean (pre-existing `ProjectManagerModal` lint errors excepted)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/hooks/useThumbnailCandidates.ts frontend/src/components/project-manager/ThumbnailSelectionModal.tsx frontend/src/components/project-manager/ProjectManagerModal.tsx
git commit -m "feat: progressive thumbnail modal with clean/output badges and candidate index"
```

---

### Task 6: Byte progress + floating download card

**Files:**
- Modify: `backend/app/services/upload_phase.py` (`start_source_video_download` ~line 1495, `source_video_status` ~line 1536, `_ensure_source_video` ~line 1460)
- Modify: `backend/app/services/google_drive_service.py` (size helper)
- Create: `frontend/src/stores/downloadProgressStore.ts`
- Create: `frontend/src/components/DownloadProgressCard.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/hooks/useUploadSourcePreview.ts`, `frontend/src/hooks/useThumbnailCandidates.ts`, `frontend/src/api/client.ts` (status type)
- Test: `backend/tests/test_upload_source_cache.py` (extend)

**Interfaces:**
- Produces: `GoogleDriveService.get_file_size(file_id) -> int | None` (`files().get(fileId=..., fields="size", supportsAllDrives=True)`, int cast, None on error). `source_video_status` gains `bytes_done`/`bytes_total` (present only while `in_progress` and known). Frontend store `useDownloadProgressStore` with `report(projectId, {state, bytesDone?, bytesTotal?, title?})` and `clear(projectId)`; `DownloadProgressCard` renders fixed bottom-right (one bar per active project, `bytes_done/bytes_total` percent or indeterminate).

- [ ] **Step 1: Backend failing test** (in `test_upload_source_cache.py`, mirroring its existing fixtures)

```python
def test_source_status_reports_bytes_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(UploadPhaseService, "_SOURCE_CACHE_DIR", tmp_path)
    cache_dir = tmp_path / "p1"
    cache_dir.mkdir()
    (cache_dir / "video.mp4.part").write_bytes(b"\x00" * 1234)
    with UploadPhaseService._source_download_guard:
        UploadPhaseService._source_downloads_in_flight.add("p1")
        UploadPhaseService._source_download_totals["p1"] = 10000
    try:
        status = UploadPhaseService.source_video_status("p1")
    finally:
        with UploadPhaseService._source_download_guard:
            UploadPhaseService._source_downloads_in_flight.discard("p1")
            UploadPhaseService._source_download_totals.pop("p1", None)
    assert status["state"] == "in_progress"
    assert status["bytes_done"] == 1234
    assert status["bytes_total"] == 10000
```

- [ ] **Step 2: Run to verify failure, then implement backend**

- Class attr `_source_download_totals: dict[str, int] = {}` next to `_source_download_errors` (`upload_phase.py:1393`), cleaned up in the same guard blocks.
- In `_ensure_source_video`'s Drive branch, before `download_file`: `total = GoogleDriveService.get_file_size(readiness.drive_video_id)`; store under the guard when not None. Local-copy branch stores nothing (instant).
- `source_video_status`, `in_progress` branch: `bytes_total` from the dict; `bytes_done` from the newest `*.part` file's `stat().st_size` in the project cache dir (0 when absent); include both keys only when in_progress.
- Clear the total in the worker's `finally` alongside `_source_downloads_in_flight.discard`.

Run: `pixi run -e dev pytest backend/tests/test_upload_source_cache.py -v` → PASS.

- [ ] **Step 3: Frontend store + card**

`downloadProgressStore.ts` (mirror `frontend/src/stores/projectStore.ts` shape):

```ts
import { create } from "zustand";

export interface DownloadProgressEntry {
  state: "in_progress" | "done";
  bytesDone?: number;
  bytesTotal?: number;
  title?: string | null;
}

interface DownloadProgressState {
  downloads: Record<string, DownloadProgressEntry>;
  report: (projectId: string, entry: DownloadProgressEntry) => void;
  clear: (projectId: string) => void;
}

export const useDownloadProgressStore = create<DownloadProgressState>((set) => ({
  downloads: {},
  report: (projectId, entry) =>
    set((s) => ({ downloads: { ...s.downloads, [projectId]: entry } })),
  clear: (projectId) =>
    set((s) => {
      const next = { ...s.downloads };
      delete next[projectId];
      return { downloads: next };
    }),
}));
```

`DownloadProgressCard.tsx`: reads the store; renders `null` when no `in_progress` entries; else a fixed `bottom-4 right-4 z-70` card listing each active download — title (or project id), a `h-1.5` rounded bar filled to `bytesTotal ? Math.round((bytesDone / bytesTotal) * 100) : undefined`% (indeterminate pulse when total unknown), label `Téléchargement de la vidéo finale…`. French copy.

Mount in `App.tsx`: wrap the existing `<Routes>` in a fragment and add `<DownloadProgressCard />` as a sibling inside `<BrowserRouter>`.

Feed the store from the two polling hooks: `useUploadSourcePreview` and `useThumbnailCandidates` call `api.getUploadSourceStatus(projectId)`'s extended fields — `useUploadSourcePreview` already polls that endpoint: `report(projectId, {state: "in_progress", bytesDone, bytesTotal})` while in_progress and `clear(projectId)` on ready/error/unmount. `useThumbnailCandidates` polls the candidates endpoint (no bytes), so ALSO poll `getUploadSourceStatus` there while status is "loading"/"partial" — or simpler and preferred: mount-side-effect in the modal is avoided by having `useThumbnailCandidates` internally run the same `getUploadSourceStatus` poll loop for the store only, cleared on unmount. `getUploadSourceStatus`'s return type in `client.ts` gains `bytes_done?: number; bytes_total?: number`.

- [ ] **Step 4: Verify**

Run: `cd frontend && npx tsc -b`
Expected: clean

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/upload_phase.py backend/app/services/google_drive_service.py backend/tests/test_upload_source_cache.py frontend/src/stores/downloadProgressStore.ts frontend/src/components/DownloadProgressCard.tsx frontend/src/App.tsx frontend/src/hooks/useUploadSourcePreview.ts frontend/src/hooks/useThumbnailCandidates.ts frontend/src/api/client.ts
git commit -m "feat: source download byte progress with floating progress card"
```

---

### Task 7: Upload side — candidate index, cover resolution, Drive hosting

**Files:**
- Modify: `backend/app/api/routes/project_manager.py` (`UploadProjectRequest` + pass-through)
- Modify: `backend/app/services/project_upload_service.py` (`UploadRequestSpec`, `enqueue_upload`, `execute_upload` call)
- Modify: `backend/app/services/upload_phase.py` (`execute_upload` extraction block ~line 1100, `_build_tiktok_payload` ~line 674, IG payload block)
- Modify: `backend/app/services/thumbnail_service.py` (cover resolver)
- Test: `backend/tests/test_upload_phase_thumbnail.py`, `backend/tests/test_upload_phase_tiktok.py`, `backend/tests/test_thumbnail_routes.py`

**Interfaces:**
- Produces:
  - `ThumbnailService.cover_image_for(project_id: str, candidate_index: int, output_video_path: Path | None, dest_path: Path) -> Path | None` — copies the cached composed cover; on cache miss re-runs the ladder for that single candidate (local → Drive → output frame when `output_video_path` given), composing to `dest_path`. None when everything fails.
  - `execute_upload(..., thumbnail_candidate_index: int | None = None)`.
  - `UploadPhaseService._attach_tiktok_cover(tiktok_payload: dict | None, cover_drive_url: str | None) -> None` — mutates the payload in place: sets `"thumbnail_url"` ONLY when the url is not None AND `tiktok_payload.get("post_for_me_platform") == "tiktok_business"`; no-op otherwise. `_build_tiktok_payload` keeps its v1 signature.
  - IG payload gains `"cover_url"` when hosting succeeded; `thumb_offset` continues to be set (VPS-side fallback data).
  - Drive hosting: composed cover upserted as `thumbnail_cover.jpg` into the project's Drive folder (`GoogleDriveService.upsert_local_file`), `set_public_read`, `get_direct_download_url` — done ONCE in `execute_upload` when `thumbnail_image_path` exists and (IG payload exists or TikTok platform is business); failure → warning log, `cover_drive_url = None`, timestamps carry the fallback.

- [ ] **Step 1: Failing tests**

`test_upload_phase_tiktok.py`:

```python
def test_attach_tiktok_cover_business_only():
    business = {"post_for_me_platform": "tiktok_business", "thumbnail_timestamp_ms": 500}
    UploadPhaseService._attach_tiktok_cover(business, "https://drive/x.jpg")
    assert business["thumbnail_url"] == "https://drive/x.jpg"
    assert business["thumbnail_timestamp_ms"] == 500

    personal = {"post_for_me_platform": "tiktok", "thumbnail_timestamp_ms": 500}
    UploadPhaseService._attach_tiktok_cover(personal, "https://drive/x.jpg")
    assert "thumbnail_url" not in personal


def test_attach_tiktok_cover_noop_on_none():
    payload = {"post_for_me_platform": "tiktok_business"}
    UploadPhaseService._attach_tiktok_cover(payload, None)
    assert "thumbnail_url" not in payload
    UploadPhaseService._attach_tiktok_cover(None, "https://drive/x.jpg")  # no raise
```

`test_upload_phase_thumbnail.py` — `cover_image_for` cache-hit and ladder-fallback tests (monkeypatch `cached_frame_path` → composed jpg copy; then cache-miss path with `_extract_local_clean_frame` → None, `_extract_drive_clean_frame` → None, output frame via `extract_frame_image`-equivalent). `test_thumbnail_routes.py` — extend `test_upload_route_forwards_thumbnail_timestamp` to also post and assert `thumbnail_candidate_index`.

- [ ] **Step 2: Run to verify failures, then implement**

- Request plumbing: exactly mirror the v1 `thumbnail_timestamp_ms` threading (same three files, same layers) for `thumbnail_candidate_index: int | None`.
- `cover_image_for`: cached path via `cached_frame_path` (covers are already composed) → `shutil.copy2` to `dest_path`; miss → recompute candidates (`load_final_timeline` + `load_matches` + `compute_candidates`), find index, run ladder, compose, save to `dest_path`.
- `execute_upload` extraction block replacement (~line 1100):

```python
            thumbnail_image_path: Path | None = None
            if thumbnail_candidate_index is not None:
                thumbnail_image_path = ThumbnailService.cover_image_for(
                    project_id,
                    thumbnail_candidate_index,
                    local_video_path,
                    Path(tmp_dir) / "thumbnail.jpg",
                )
            if thumbnail_image_path is None and thumbnail_timestamp_ms is not None:
                thumbnail_image_path = ThumbnailService.extract_frame_image(
                    local_video_path,
                    thumbnail_timestamp_ms / 1000.0,
                    Path(tmp_dir) / "thumbnail.jpg",
                )
            thumbnail_extraction_failed = (
                (thumbnail_timestamp_ms is not None or thumbnail_candidate_index is not None)
                and thumbnail_image_path is None
            )
```

- Drive hosting, right after the block above:

```python
            cover_drive_url: str | None = None
            wants_hosted_cover = thumbnail_image_path is not None and (
                ig_payload_base is not None
                or (
                    account is not None
                    and account.tiktok is not None
                    and account.tiktok.post_for_me_platform == "tiktok_business"
                )
            )
            if wants_hosted_cover:
                try:
                    uploaded_cover = GoogleDriveService.upsert_local_file(
                        parent_id=readiness.drive_folder_id,
                        filename="thumbnail_cover.jpg",
                        local_path=thumbnail_image_path,
                        chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
                    )
                    cover_id = str(uploaded_cover.get("id") or "").strip()
                    if cover_id:
                        GoogleDriveService.set_public_read(cover_id)
                        cover_drive_url = GoogleDriveService.get_direct_download_url(cover_id)
                except Exception:
                    logger.warning(
                        "Cover Drive hosting failed for %s; falling back to timestamps",
                        project_id, exc_info=True,
                    )
```

(Match `upsert_local_file`'s real signature at `google_drive_service.py:840` — it may take `drive=` and return dict.) Place this INSIDE the tmp_dir block before the TikTok payload build and job creation — NOTE: v1 builds `tiktok_payload` at ~line 931, BEFORE the tmp_dir block. Move the `_build_tiktok_payload` call down (or rebuild the payload after hosting): the payload is only consumed by `DiscordService.create_job` at ~line 1207, so rebuild `tiktok_payload` just before `_vps_platforms`... `_vps_platforms` also consumes it at ~line 939 (outside tmp_dir). Solution: keep the early build for `_vps_platforms` gating (unchanged), then AFTER hosting, call `cls._attach_tiktok_cover(tiktok_payload, cover_drive_url)` (the helper the Step-1 tests target): it mutates the payload, adding `"thumbnail_url"` only for the `tiktok_business` connector, and no-ops on None payload/url. `_build_tiktok_payload` keeps its v1 signature.
- IG: in the ig_payload branch where `thumb_offset` is set (~line 1233), additionally `ig_payload["cover_url"] = cover_drive_url` when not None (keep `thumb_offset` always).
- YT/FB paths are untouched — they already receive `thumbnail_image_path` (now the composed cover).

- [ ] **Step 3: Run tests**

Run: `pixi run -e dev pytest backend/tests/test_upload_phase_thumbnail.py backend/tests/test_upload_phase_tiktok.py backend/tests/test_thumbnail_routes.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/routes/project_manager.py backend/app/services/project_upload_service.py backend/app/services/upload_phase.py backend/app/services/thumbnail_service.py backend/tests/test_upload_phase_thumbnail.py backend/tests/test_upload_phase_tiktok.py backend/tests/test_thumbnail_routes.py
git commit -m "feat: composed cover resolution, Drive hosting, cover_url/thumbnail_url payloads"
```

---

### Task 8: VPS — Instagram cover_url + TikTok thumbnail_url

**Files:**
- Modify: `server/app/api/internal.py` (`InstagramPayload` ~line 25, `TikTokPayload` ~line 38)
- Modify: `server/app/services/instagram_publisher.py` (`_create_instagram_container` ~line 404, `publish_to_instagram` retry structure ~line 1417-1497)
- Modify: `server/app/services/post_for_me_publisher.py` (`create_tiktok_post` media item ~line 357, `publish_to_tiktok`)
- Modify: `server/app/services/reminder_scheduler.py` (both dispatch call sites: TikTok ~line 331, Instagram ~line 437)
- Test: `server/tests/test_instagram_publisher.py`, `server/tests/test_post_for_me_publisher.py`, `server/tests/test_reminder_scheduler.py`, `server/tests/test_internal_api.py`

**Interfaces:**
- Produces: `InstagramPayload.cover_url: str | None = None`; `TikTokPayload.thumbnail_url: str | None = None`; `_create_instagram_container(..., cover_url: str | None = None)` inserts `create_data["cover_url"]` when set; container-creation failure with a set `cover_url` retries ONCE without it (mirroring `_create_rupload_fallback_container`'s pattern) before the existing fallbacks; `create_tiktok_post(..., thumbnail_url: str | None = None)` → media item `{"url", "thumbnail_url"?, "thumbnail_timestamp_ms"?}`; schedulers pass `payload.get("cover_url")` / `payload.get("thumbnail_url")`.

- [ ] **Step 1: Failing tests**

`test_post_for_me_publisher.py` (existing `fake` fixture):

```python
async def test_create_post_includes_thumbnail_url(fake, tmp_path):
    state = TikTokPublishState(media_url="https://media.example/abc.mp4",
                               stage="media_uploaded")
    result = await create_tiktok_post(
        api_key="key", social_account_id="spc_1", caption="cap",
        post_for_me_platform="tiktok_business",
        thumbnail_url="https://drive.example/cover.jpg",
        thumbnail_timestamp_ms=500, publish_state=state,
    )
    assert result.success is True
    assert fake.created_posts[0]["media"] == [{
        "url": "https://media.example/abc.mp4",
        "thumbnail_timestamp_ms": 500,
        "thumbnail_url": "https://drive.example/cover.jpg",
    }]
```

`test_reminder_scheduler.py` — extend the thumbnail pass-through test (or add a sibling) asserting `calls["create"][0]["thumbnail_url"]` when `job.tiktok_payload["thumbnail_url"]` is set. For Instagram, mirror the file's existing `publish_to_instagram` monkeypatch pattern asserting `cover_url=payload.get("cover_url")` reaches the call.

`test_instagram_publisher.py` — locate the existing container-creation tests (httpx-mock based); add: (a) `cover_url` present in the posted `data` when passed; (b) container creation that fails with HTTP 400 while `cover_url` is set retries once WITHOUT `cover_url` and succeeds (mock returns 400 for requests containing cover_url, 200 otherwise; assert two POSTs). Mirror the file's existing fixture/transport conventions exactly.

- [ ] **Step 2: Run server tests to verify failures**

Run: `cd server && .venv/bin/python -m pytest tests/test_post_for_me_publisher.py tests/test_instagram_publisher.py tests/test_reminder_scheduler.py -v`

- [ ] **Step 3: Implement**

- `internal.py`: add both optional fields (the `model_dump(exclude_none=True)` in `_tiktok_payload` and the `_instagram_payload` builder keep absent-when-None semantics — verify `_instagram_payload` uses `exclude_none` too; if it uses plain `model_dump()`, None values are tolerated downstream via `payload.get`, leave as-is).
- `post_for_me_publisher.py`: `create_tiktok_post` + `publish_to_tiktok` gain `thumbnail_url: str | None = None`; media item adds `"thumbnail_url"` when not None (after the timestamp field).
- `instagram_publisher.py`: `_create_instagram_container` gains `cover_url: str | None = None` → `create_data["cover_url"] = cover_url` when set. In `publish_to_instagram`: thread a `cover_url` parameter from the payload; on the primary container-creation `httpx.HTTPStatusError` where `cover_url` was set, log a warning and retry `_create_instagram_container` once with `cover_url=None` BEFORE falling through to the existing rupload fallback; the rupload fallback itself passes `cover_url=None` (a bad cover URL must never kill the publish).
- `reminder_scheduler.py`: TikTok dispatch adds `thumbnail_url=payload.get("thumbnail_url")`; Instagram dispatch adds `cover_url=payload.get("cover_url")`.

- [ ] **Step 4: Run server tests**

Run: `cd server && .venv/bin/python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/api/internal.py server/app/services/instagram_publisher.py server/app/services/post_for_me_publisher.py server/app/services/reminder_scheduler.py server/tests/test_instagram_publisher.py server/tests/test_post_for_me_publisher.py server/tests/test_reminder_scheduler.py server/tests/test_internal_api.py
git commit -m "feat(vps): Instagram cover_url with retry-without-cover + PFM thumbnail_url"
```

---

### Task 9: Full verification sweep

- [ ] **Step 1:** `pixi run -e dev pytest backend/tests/test_thumbnail_service.py backend/tests/test_thumbnail_routes.py backend/tests/test_upload_phase_thumbnail.py backend/tests/test_upload_phase_tiktok.py backend/tests/test_upload_source_routes.py backend/tests/test_upload_source_cache.py backend/tests/test_instagram_drive_preparation.py backend/tests/test_upload_phase_local_source.py -v` — all PASS.
- [ ] **Step 2:** `cd server && .venv/bin/python -m pytest tests/ -v` — all PASS.
- [ ] **Step 3:** `cd frontend && npm run build` — clean.
- [ ] **Step 4:** Report: feature complete; **VPS redeploy required** (internal.py, both publishers, scheduler); owner E2E = next real upload (check modal progressive tiles + badges + floating progress card, then covers on all five platform paths).

## Out of scope (per spec)

- Custom cover images / text overlays; per-platform distinct choices; post-publish cover changes; non-YPP Shorts feed coverage.
