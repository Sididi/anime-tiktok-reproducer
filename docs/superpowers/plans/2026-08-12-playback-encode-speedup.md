# Playback Encode Speedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut full playback-prepare wall-clock ~4–5× by adding a full-GPU transcode rung (NVDEC → scale_cuda → NVENC p1) to the clip encode ladder and raising encode parallelism.

**Architecture:** `MatchPlaybackService._encode_clip_sync` gains one new rung between the existing stream-copy fast path and the CPU-decode NVENC path, gated by a one-time cached capability probe. Worker defaults rise (global 4→8, per-episode 1→4). Output profiles, qp values, and `ENCODE_PROFILE_VERSION` are untouched, so existing clip caches stay valid and the x16 fast-watch experience is unchanged.

**Tech Stack:** Python 3 / FastAPI backend, ffmpeg 8.1 (system, `ATR_FFMPEG_BINARY=/usr/bin/ffmpeg` — verified to have `hevc_cuvid`, `h264_cuvid`, `scale_cuda`, `h264_nvenc`), pytest via pixi `dev` env.

**Spec:** `docs/superpowers/specs/2026-08-12-playback-encode-speedup-design.md`

## Global Constraints

- Do **not** bump `ENCODE_PROFILE_VERSION` (`"v4|source_h264_only"` stays).
- Do **not** change `_PROFILE_MAP` (resolutions, fps, crf/qp values stay identical).
- Ladder order: stream copy → full-GPU (new) → NVENC with CPU decode → libx264 ultrafast. Every rung failure appends to `error_details` and falls through; a clip fails only when all rungs fail.
- Full-GPU rung uses `-preset p1`; existing NVENC rung keeps `-preset p5` (unchanged).
- Tests run via the pixi dev env: `cd backend && pixi run -e dev pytest <file> -v`. Never run two pytest invocations concurrently (project rule). The default pixi env lacks pytest — always use `-e dev`.
- The backend test suite has ~17 pre-existing, attributed failures (2026-07-24 baseline). Only the files named in this plan must pass.

## File Structure

- Modify: `backend/app/services/match_playback_service.py` — probe (Task 1), command builder (Task 2), ladder integration (Task 3).
- Modify: `backend/app/config.py` — worker defaults (Task 4).
- Create: `backend/tests/test_match_playback_gpu_ladder.py` — all new unit tests (follows the monkeypatch patterns of `backend/tests/test_match_playback_clip_store.py`).

---

### Task 1: Full-GPU capability probe

**Files:**
- Modify: `backend/app/services/match_playback_service.py` (class attrs near line 121-122; new method next to `_is_nvenc_available_sync`, which ends at line 631)
- Create: `backend/tests/test_match_playback_gpu_ladder.py`

**Interfaces:**
- Consumes: existing `_is_nvenc_available_sync()`, `rewrite_media_command`, `get_media_subprocess_env`.
- Produces: `MatchPlaybackService._is_full_gpu_available_sync() -> bool` (classmethod, result cached in class attrs `_full_gpu_checked: bool` / `_full_gpu_available: bool`). Task 3 gates the new rung on this.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_match_playback_gpu_ladder.py`:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.match_playback_service import MatchPlaybackService, _ClipPlan


@pytest.fixture(autouse=True)
def reset_capability_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class-level probe caches must not leak between tests."""
    monkeypatch.setattr(MatchPlaybackService, "_nvenc_checked", False)
    monkeypatch.setattr(MatchPlaybackService, "_nvenc_available", False)
    monkeypatch.setattr(MatchPlaybackService, "_full_gpu_checked", False)
    monkeypatch.setattr(MatchPlaybackService, "_full_gpu_available", False)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capability_fake_run(*, encoders: str, decoders: str, filters: str, calls: list):
    """Return a fake subprocess.run serving ffmpeg capability listings."""

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-encoders" in cmd:
            return _FakeCompleted(stdout=encoders)
        if "-decoders" in cmd:
            return _FakeCompleted(stdout=decoders)
        if "-filters" in cmd:
            return _FakeCompleted(stdout=filters)
        raise AssertionError(f"unexpected command: {cmd}")

    return _fake_run


def test_full_gpu_probe_true_when_all_capabilities_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264_cuvid ... hevc_cuvid ...",
            filters="... scale_cuda ...",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is True
    first_call_count = len(calls)

    # Second call must be served from the cache: no new subprocess calls.
    assert MatchPlaybackService._is_full_gpu_available_sync() is True
    assert len(calls) == first_call_count


def test_full_gpu_probe_false_without_scale_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264_cuvid ... hevc_cuvid ...",
            filters="... scale ... (no cuda resizer)",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def test_full_gpu_probe_false_without_cuvid_decoders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264 ... hevc ...",
            filters="... scale_cuda ...",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def test_full_gpu_probe_false_when_nvenc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: False)
    )

    def _explode(cmd, **kwargs):
        raise AssertionError("no capability listing should run when nvenc is absent")

    monkeypatch.setattr("app.services.match_playback_service.subprocess.run", _explode)
    assert MatchPlaybackService._is_full_gpu_available_sync() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pixi run -e dev pytest tests/test_match_playback_gpu_ladder.py -v`
Expected: the three probe tests FAIL with `AttributeError: ... has no attribute '_full_gpu_checked'` (fixture) — confirming the method/attrs don't exist yet.

- [ ] **Step 3: Implement the probe**

In `backend/app/services/match_playback_service.py`, next to the existing class attrs (`_nvenc_checked = False` / `_nvenc_available = False`, lines 121-122), add:

```python
    _full_gpu_checked = False
    _full_gpu_available = False
```

Directly below `_is_nvenc_available_sync` (after line 631), add:

```python
    @classmethod
    def _run_capability_listing_sync(cls, flag: str) -> str | None:
        """Return stdout of `ffmpeg -hide_banner <flag>`, or None on failure."""
        cmd = rewrite_media_command(["ffmpeg", "-hide_banner", flag])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=get_media_subprocess_env(cmd),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    @classmethod
    def _is_full_gpu_available_sync(cls) -> bool:
        """True when ffmpeg can run the NVDEC -> scale_cuda -> NVENC pipeline."""
        if cls._full_gpu_checked:
            return cls._full_gpu_available

        cls._full_gpu_checked = True
        cls._full_gpu_available = False

        if not cls._is_nvenc_available_sync():
            return False

        decoders = cls._run_capability_listing_sync("-decoders")
        if (
            decoders is None
            or "hevc_cuvid" not in decoders
            or "h264_cuvid" not in decoders
        ):
            return False

        filters = cls._run_capability_listing_sync("-filters")
        if filters is None or "scale_cuda" not in filters:
            return False

        cls._full_gpu_available = True
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pixi run -e dev pytest tests/test_match_playback_gpu_ladder.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/match_playback_service.py backend/tests/test_match_playback_gpu_ladder.py
git commit -m "feat(playback): cached capability probe for full-GPU transcode pipeline"
```

---

### Task 2: Full-GPU command builder

**Files:**
- Modify: `backend/app/services/match_playback_service.py` (new builder next to `_build_nvenc_command_sync`, lines 728-773)
- Test: `backend/tests/test_match_playback_gpu_ladder.py`

**Interfaces:**
- Consumes: `_ClipPlan` (dataclass: `scene_index, track, input_path, start_time, end_time, profile, clip_id, source_key`), `_ClipProfile` (`key, width, height, fps, crf`), `rewrite_media_command`.
- Produces: `MatchPlaybackService._build_full_gpu_command_sync(*, plan: _ClipPlan, profile: _ClipProfile, duration: float, output_path: Path) -> list[str]`. Note: unlike `_build_nvenc_command_sync` it takes **no `vf` argument** — the GPU filter chain differs (fps drop before CUDA scale) and is built internally. Task 3 calls this.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_match_playback_gpu_ladder.py`:

```python
def _make_plan(tmp_path: Path, track: str = "tiktok", profile: str = "tiktok_fast") -> _ClipPlan:
    src = tmp_path / "input.mp4"
    src.write_bytes(b"fake")
    return _ClipPlan(
        scene_index=0,
        track=track,  # type: ignore[arg-type]
        input_path=src,
        start_time=12.5,
        end_time=15.0,
        profile=profile,
        clip_id="clipid0000000000000000000000000000000000",
        source_key=None,
    )


def test_full_gpu_command_shape(tmp_path: Path) -> None:
    plan = _make_plan(tmp_path)
    profile = MatchPlaybackService._PROFILE_MAP["tiktok_fast"]
    out = tmp_path / "out.mp4"
    cmd = MatchPlaybackService._build_full_gpu_command_sync(
        plan=plan, profile=profile, duration=2.5, output_path=out
    )

    joined = " ".join(cmd)
    # Decode on GPU, frames stay on GPU.
    assert "-hwaccel cuda -hwaccel_output_format cuda" in joined
    # Input seek before -i (fast keyframe seek), exact window.
    assert cmd.index("-ss") < cmd.index("-i")
    assert "12.500000" in cmd and "2.500000" in cmd
    # fps drop BEFORE the CUDA scaler; nv12 handles 10-bit sources.
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == (
        "fps=24,scale_cuda=w=540:h=960:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12"
    )
    # Fastest NVENC preset at the profile's constant QP.
    assert "h264_nvenc" in cmd and "p1" in cmd
    assert cmd[cmd.index("-qp") + 1] == "28"
    # No CPU pix_fmt flag: the encoder consumes CUDA frames directly.
    assert "-pix_fmt" not in cmd
    assert "+faststart" in cmd and str(out) in cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pixi run -e dev pytest tests/test_match_playback_gpu_ladder.py::test_full_gpu_command_shape -v`
Expected: FAIL with `AttributeError: ... has no attribute '_build_full_gpu_command_sync'`.

- [ ] **Step 3: Implement the builder**

In `backend/app/services/match_playback_service.py`, directly after `_build_nvenc_command_sync` (line 773), add:

```python
    @classmethod
    def _build_full_gpu_command_sync(
        cls,
        *,
        plan: _ClipPlan,
        profile: _ClipProfile,
        duration: float,
        output_path: Path,
    ) -> list[str]:
        """NVDEC decode -> fps drop -> CUDA scale -> NVENC encode, no CPU frames.

        `fps` runs before `scale_cuda` so dropped frames are never scaled;
        frame selection is identical either way. `format=nv12` converts 10-bit
        (p010) NVDEC output so h264_nvenc can consume it.
        """
        vf = (
            f"fps={profile.fps},"
            f"scale_cuda=w={profile.width}:h={profile.height}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12"
        )
        return rewrite_media_command(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-hwaccel",
                "cuda",
                "-hwaccel_output_format",
                "cuda",
                "-ss",
                f"{plan.start_time:.6f}",
                "-i",
                str(plan.input_path),
                "-t",
                f"{duration:.6f}",
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                vf,
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p1",
                "-rc",
                "constqp",
                "-qp",
                str(profile.crf),
                "-profile:v",
                "high",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pixi run -e dev pytest tests/test_match_playback_gpu_ladder.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/match_playback_service.py backend/tests/test_match_playback_gpu_ladder.py
git commit -m "feat(playback): full-GPU ffmpeg command builder (NVDEC + scale_cuda + NVENC p1)"
```

---

### Task 3: Ladder integration in `_encode_clip_sync`

**Files:**
- Modify: `backend/app/services/match_playback_service.py` (`_encode_clip_sync`, between the stream-copy block ending at line 876 and the `# --- Standard transcode path ---` comment at line 878)
- Test: `backend/tests/test_match_playback_gpu_ladder.py`

**Interfaces:**
- Consumes: `_is_full_gpu_available_sync()` (Task 1), `_build_full_gpu_command_sync(...)` (Task 2), existing `_validate_clip_sync`, `error_details` list, `tmp_path` temp-output pattern.
- Produces: no new public surface — `_encode_clip_sync` behavior: rung order is stream-copy → full-GPU → NVENC(CPU decode) → libx264, each failure appending `error_details` and falling through.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_match_playback_gpu_ladder.py`:

```python
@pytest.fixture
def clip_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "clip_store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        MatchPlaybackService,
        "_clip_store_dir",
        classmethod(lambda cls, project_id: store),
    )
    return store


def _classify(cmd: list[str]) -> str:
    if "-hwaccel" in cmd:
        return "full_gpu"
    if "h264_nvenc" in cmd:
        return "nvenc_cpu"
    if "libx264" in cmd:
        return "libx264"
    return "other"


def _ladder_fake_run(outcomes: dict[str, int], attempts: list[str]):
    """Fake subprocess.run: rung name -> returncode; rc 0 writes the output."""

    def _fake_run(cmd, **kwargs):
        rung = _classify(list(cmd))
        attempts.append(rung)
        rc = outcomes[rung]
        if rc == 0:
            Path(cmd[-1]).write_bytes(b"encoded")
        return _FakeCompleted(returncode=rc, stderr=f"{rung} boom" if rc else "")

    return _fake_run


def _patch_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MatchPlaybackService,
        "_validate_clip_sync",
        classmethod(lambda cls, path: 2.5),
    )


def test_ladder_full_gpu_success_stops_ladder(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu"]
    assert (clip_store / f"{plan.clip_id}.mp4").exists()


def test_ladder_falls_through_gpu_then_nvenc_then_libx264(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 1, "nvenc_cpu": 1, "libx264": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu", "nvenc_cpu", "libx264"]


def test_ladder_skips_gpu_rung_when_probe_false(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: False)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"nvenc_cpu": 0}, attempts),
    )

    MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert attempts == ["nvenc_cpu"]


def test_ladder_gpu_output_failing_validation_falls_through(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 but undecodable GPU output must fall through, not poison the store."""
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )

    def _validate(cls, path: Path) -> float:
        # First validation call (the GPU rung's inline check) rejects the clip;
        # later calls (nvenc output + final gate) accept it.
        if attempts == ["full_gpu"]:
            raise RuntimeError("no decodable video stream")
        return 2.5

    monkeypatch.setattr(
        MatchPlaybackService, "_validate_clip_sync", classmethod(_validate)
    )
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 0, "nvenc_cpu": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu", "nvenc_cpu"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pixi run -e dev pytest tests/test_match_playback_gpu_ladder.py -v -k ladder`
Expected: `test_ladder_full_gpu_success_stops_ladder`, `test_ladder_falls_through_gpu_then_nvenc_then_libx264`, and `test_ladder_gpu_output_failing_validation_falls_through` FAIL (the GPU rung doesn't exist, so attempts start with `nvenc_cpu`). `test_ladder_skips_gpu_rung_when_probe_false` may already pass — that's fine.

- [ ] **Step 3: Implement the GPU rung**

In `_encode_clip_sync` (`backend/app/services/match_playback_service.py`), insert between the stream-copy block (ends line 876, `error_details.append("stream_copy: timeout or ffmpeg not found")`) and the `# --- Standard transcode path ---` comment (line 878):

```python
        # --- Full-GPU path: NVDEC decode + CUDA scale + NVENC encode ---
        if not encoded and cls._is_full_gpu_available_sync():
            gpu_cmd = cls._build_full_gpu_command_sync(
                plan=plan,
                profile=profile,
                duration=duration,
                output_path=tmp_path,
            )
            try:
                result = subprocess.run(
                    gpu_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=cls.FFMPEG_TIMEOUT_SECONDS,
                    env=get_media_subprocess_env(gpu_cmd),
                )
                if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                    try:
                        cls._validate_clip_sync(tmp_path)
                        encoded = True
                    except RuntimeError:
                        tmp_path.unlink(missing_ok=True)
                        error_details.append(
                            "full_gpu: validation failed, falling back to CPU-decode transcode"
                        )
                else:
                    error_details.append(
                        f"full_gpu: {result.stderr.strip() or 'unknown error'}"
                    )
                    tmp_path.unlink(missing_ok=True)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                tmp_path.unlink(missing_ok=True)
                error_details.append("full_gpu: timeout or ffmpeg not found")
```

Notes for the implementer:
- `profile`, `duration`, `tmp_path`, `error_details`, `encoded` are all already in scope at that point (defined lines 828-844).
- The inline `_validate_clip_sync` mirrors the stream-copy rung: an rc=0-but-undecodable GPU output must fall through to the CPU rungs instead of hitting the final validation gate (which raises without fallback).
- The GPU timeout/`FileNotFoundError` is caught here (unlike the existing NVENC rung) so a wedged GPU never kills the clip outright — the CPU rungs still get their chance.

- [ ] **Step 4: Run the full new test file plus the existing clip-store tests**

Run: `cd backend && pixi run -e dev pytest tests/test_match_playback_gpu_ladder.py tests/test_match_playback_clip_store.py -v`
Expected: all PASS (the clip-store file guards the pre-existing empty-container/validation behavior we must not regress).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/match_playback_service.py backend/tests/test_match_playback_gpu_ladder.py
git commit -m "feat(playback): full-GPU rung in clip encode ladder with CPU fallthrough"
```

---

### Task 4: Raise parallelism defaults

**Files:**
- Modify: `backend/app/config.py:51-52`

**Interfaces:**
- Consumes: nothing new.
- Produces: `settings.match_playback_max_workers` default 8, `settings.match_playback_max_workers_per_episode` default 4. Existing clamps (`max 8` at line 324-327, `max 4` at line 384-387) already cover the new defaults — do not touch the validators.

- [ ] **Step 1: Edit the defaults**

In `backend/app/config.py` change lines 51-52 from:

```python
    match_playback_max_workers: int = 4
    match_playback_max_workers_per_episode: int = 1
```

to:

```python
    # 8 = consumer NVENC concurrent-session cap; safe since the full-GPU encode
    # rung leaves the CPU nearly idle. Per-episode 4: same-file concurrent reads
    # measured ~2x throughput on the GPU path (see 2026-08-12 spec).
    match_playback_max_workers: int = 8
    match_playback_max_workers_per_episode: int = 4
```

- [ ] **Step 2: Verify defaults resolve (bypassing .env)**

Run: `cd backend && pixi run -e dev python -c "from app.config import Settings; s = Settings(_env_file=None); print(s.match_playback_max_workers, s.match_playback_max_workers_per_episode)"`
Expected output: `8 4`

Also confirm no `.env` override shadows them: `grep -i "match_playback" /home/sid/Projects/anime-tiktok-reproducer/.env` — expected: no matches (verified 2026-08-12; if a line appears, tell the owner instead of editing `.env`).

- [ ] **Step 3: Commit**

```bash
git add backend/app/config.py
git commit -m "feat(playback): raise encode parallelism defaults (global 8, per-episode 4)"
```

---

### Task 5: Acceptance — timed real-project prepare

**Files:**
- Create: `/tmp/claude-1000/-home-sid-Projects-anime-tiktok-reproducer/1eb9c3e1-740f-4c9e-80f9-7b06a14641e0/scratchpad/time_prepare.py` (scratch only — do NOT commit)

**Interfaces:**
- Consumes: `MatchPlaybackService.prepare_playback(project_id, force=True)` (async generator yielding `PlaybackPrepareProgress`).
- Produces: measured before/after evidence for the final report. No repo changes.

**Preconditions (check, don't assume):**
- The backend server (uvicorn) must NOT be running a prepare for the same project: `MatchPlaybackService` locks are per-process, so a concurrent server-side prepare would race the clip store. Easiest: confirm the backend is stopped (`pgrep -af uvicorn`) or simply don't touch the app during the run.
- GPU roughly idle: `nvidia-smi --query-gpu=memory.used --format=csv,noheader` should show well under ~2000 MiB in use.

- [ ] **Step 1: Write the timing script**

```python
import asyncio
import sys
import time

sys.path.insert(0, "/home/sid/Projects/anime-tiktok-reproducer/backend")

from app.services.match_playback_service import MatchPlaybackService

PROJECT_ID = "ca56e019da2e"  # 176 scenes, 2 episodes — the heaviest recent project


async def main() -> None:
    t0 = time.perf_counter()
    last = None
    async for progress in MatchPlaybackService.prepare_playback(PROJECT_ID, force=True):
        last = progress
    elapsed = time.perf_counter() - t0
    manifest = last.manifest or {}
    stats = manifest.get("clip_store_stats", {})
    scene_status = manifest.get("scene_status", {})
    not_ready = {k: v for k, v in scene_status.items() if v != "ready"}
    print(f"wall: {elapsed:.1f}s")
    print(f"status: {last.status}  ready: {manifest.get('ready')}")
    print(f"encoded: {stats.get('encoded_count')}  reused: {stats.get('reused_count')}")
    print(f"non-ready scenes: {not_ready or 'none'}")


asyncio.run(main())
```

- [ ] **Step 2: Run the AFTER measurement**

Run (from repo root): `pixi run -e dev python "/tmp/claude-1000/-home-sid-Projects-anime-tiktok-reproducer/1eb9c3e1-740f-4c9e-80f9-7b06a14641e0/scratchpad/time_prepare.py"`

Expected:
- `ready: True`, `non-ready scenes: none`, `encoded` ≈ 350 (force re-encodes everything).
- `wall:` in the ~25–45s range (vs ~100–150s estimated baseline). If wall-clock exceeds ~60s, check stderr/manifest for `full_gpu:` fallback messages before concluding — the win depends on the GPU rung actually engaging.
- During the run, optionally confirm GPU engagement: `nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader -l 5` shows non-trivial utilization, or `nvidia-smi` lists several ffmpeg processes.

- [ ] **Step 3: Spot-check one produced clip**

Pick any clip id printed in the manifest (`backend/data/projects/ca56e019da2e/playback_cache_v3/clip_store/`), then:

Run: `ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,pix_fmt,avg_frame_rate -of csv=p=0 <clip.mp4>`
Expected: `h264`, profile-correct dimensions (e.g. `540,960` tiktok / `640,360` source), `yuv420p`(or `nv12`-tagged as yuv420p), profile fps. Visually open one in a browser/player if in doubt.

- [ ] **Step 4: Report**

No commit (scratch script only). Include in the final summary: measured wall-clock, encoded/reused counts, any `full_gpu` fallback occurrences, and the clip spot-check result.

---

## Self-Review (completed)

- **Spec coverage:** ladder rung (Task 3), command shape (Task 2), probe (Task 1), parallelism (Task 4), no-version-bump (global constraint — no task touches `ENCODE_PROFILE_VERSION` or `_PROFILE_MAP`), testing section (Tasks 1-3 unit, Task 5 acceptance). ✔
- **Placeholder scan:** all steps carry runnable code/commands; no TBDs. ✔
- **Type consistency:** `_is_full_gpu_available_sync()` and `_build_full_gpu_command_sync(*, plan, profile, duration, output_path)` used identically in Tasks 1/2/3; `_FakeCompleted`, `_make_plan`, `_classify` defined before use within the single test file. ✔
