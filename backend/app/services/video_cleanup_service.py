"""Pure-mode video cleanup: burned-in subtitle/watermark removal.

Pipeline (all zone rects are user-drawn, normalized frame coords):

1. Detection pass — sequential decode; per frame, a white-text score inside
   each subtitle rect (luma/saturation candidate mask gated by outline
   contrast). Hysteresis + temporal smoothing turn scores into presence
   spans. Watermark zones are active for the whole video.
2. Inpainting pass — second sequential decode; for every span (chunked to
   bound GPU memory) the crop region around the rect is fed to ProPainter
   (see propainter_adapter) with clean lead-in/out context frames where the
   span edges allow it. Results are cached on disk (`cleanup/spans/*.npz`),
   which also makes a re-run resume for free.
3. Assembly pass — third sequential decode; span results are composited back
   (feathered mask) and raw frames are piped to an ffmpeg encoder (NVENC
   with libx264 fallback); audio is stream-copied from the original file.

The full job swaps ``project.video_path`` to ``tiktok_clean.mp4`` on
completion; ``project.original_video_path`` keeps the raw download.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable

import cv2
import numpy as np

from ..config import settings
from ..library_types import LibraryType
from ..models import ProjectPhase
from ..models.cleanup import CleanupState, CleanupZone
from ..utils.media_binaries import get_media_subprocess_env, rewrite_media_command
from ..utils.video_color import ensure_bt709_tags
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Tunables (centralized; calibrate on real subtitle styles with the debug dump)
# ---------------------------------------------------------------------------

# White-text candidate mask: bright + unsaturated.
TEXT_LUMA_MIN = 190
TEXT_SATURATION_MAX = 60
# Outline gate: morphological gradient threshold + how far a bright pixel may
# sit from a strong edge and still count as text.
TEXT_GRADIENT_MIN = 40
TEXT_GRADIENT_REACH = 2  # dilation iterations of the gradient mask
# Presence hysteresis, as fraction of the rect area.
TEXT_SCORE_ON = 0.003
TEXT_SCORE_OFF = 0.001
# Temporal smoothing (frames).
TEXT_GAP_CLOSE_FRAMES = 4
TEXT_ISLAND_DROP_FRAMES = 2
TEXT_SPAN_PAD_FRAMES = 3

# Crop geometry.
CROP_MARGIN_FRACTION = 0.15
CROP_MARGIN_MIN_PX = 24
CROP_ALIGN = 16
# Model input cap (long side) before downscale, per the 8 GB VRAM budget.
MODEL_MAX_LONG_SIDE = 640

# Span → clip chunking (frames). Context frames are clean references at span
# edges; mid-span chunk boundaries overlap with masks kept active.
CLIP_CONTEXT_FRAMES = 10
CLIP_MAX_FRAMES = 120
CLIP_CHUNK_OVERLAP = 10

# Inpaint mask dilation inside the rect (pixels ~ iterations).
MASK_DILATE_ITERATIONS = 9
# Feather (Gaussian sigma in px) when compositing back.
COMPOSITE_FEATHER_SIGMA = 1.5

# OOM ladder for ProPainter subvideo length.
SUBVIDEO_LADDER = (80, 40, 24)
# Extra downscale steps if the ladder alone is not enough.
OOM_DOWNSCALE_FACTOR = 0.75

PREVIEW_LEAD_SECONDS = 0.5
PREVIEW_DURATION_SECONDS = 4.0


@dataclass
class _ZonePlan:
    zone: CleanupZone
    # Pixel rect in frame coords.
    rect: tuple[int, int, int, int]  # x, y, w, h
    # Crop region (aligned, includes margin).
    crop: tuple[int, int, int, int]  # x, y, w, h
    # Active frame spans [(start, end_exclusive), ...]
    spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class _Clip:
    zone_plan: _ZonePlan
    # Frame window fed to the model (includes context/overlap).
    frame_start: int
    frame_end: int  # exclusive
    # Sub-window (absolute frames) whose result is composited.
    out_start: int
    out_end: int  # exclusive
    # Absolute frame range where the mask is active.
    mask_start: int
    mask_end: int  # exclusive

    @property
    def cache_name(self) -> str:
        z = self.zone_plan.zone
        return f"{z.kind}_{z.id}_{self.out_start:06d}_{self.out_end:06d}.npz"


class CleanupCancelled(Exception):
    pass


_cancel_events: dict[str, threading.Event] = {}
_running_tasks: dict[str, asyncio.Task] = {}


class VideoCleanupService:
    # -- paths ------------------------------------------------------------

    @classmethod
    def get_cleanup_dir(cls, project_id: str) -> Path:
        return settings.projects_dir / project_id / "cleanup"

    @classmethod
    def get_clean_video_path(cls, project_id: str) -> Path:
        return settings.projects_dir / project_id / "tiktok_clean.mp4"

    # -- state ------------------------------------------------------------

    @classmethod
    def _require_pure_project(cls, project_id: str):
        project = ProjectService.load(project_id)
        if project is None:
            raise ValueError("Project not found")
        if project.library_type != LibraryType.PURE:
            raise ValueError("Cleanup is only available for Pure projects")
        return project

    @classmethod
    def get_state(cls, project_id: str) -> CleanupState:
        project = cls._require_pure_project(project_id)
        return project.cleanup or CleanupState()

    @classmethod
    def save_zones(cls, project_id: str, zones: list[CleanupZone]) -> CleanupState:
        project = cls._require_pure_project(project_id)
        subtitle_zones = [z for z in zones if z.kind == "subtitle"]
        if len(subtitle_zones) > 1:
            raise ValueError("Only one subtitle zone is supported")
        state = project.cleanup or CleanupState()
        if state.status == "running":
            raise ValueError("Cleanup is running; cancel it before editing zones")
        state.zones = zones
        state.updated_at = datetime.now()
        project.cleanup = state
        ProjectService.save(project)
        return state

    @classmethod
    def _update_state(cls, project_id: str, **updates) -> CleanupState:
        project = ProjectService.load(project_id)
        if project is None:
            raise ValueError("Project not found")
        state = project.cleanup or CleanupState()
        for key, value in updates.items():
            setattr(state, key, value)
        state.updated_at = datetime.now()
        project.cleanup = state
        ProjectService.save(project)
        return state

    # -- job orchestration -------------------------------------------------

    @classmethod
    async def start_full_cleanup(cls, project_id: str) -> None:
        project = cls._require_pure_project(project_id)
        source = project.original_video_path or project.video_path
        if not source or not Path(source).exists():
            raise ValueError("Project video not found")
        state = project.cleanup or CleanupState()
        if not state.zones:
            raise ValueError("Draw at least one cleanup zone first")
        if state.status == "running":
            raise ValueError("Cleanup already running")

        cls._update_state(
            project_id,
            status="running",
            progress=0.0,
            message="Queued (waiting for a heavy-processing slot)…",
            error=None,
        )
        cancel_event = threading.Event()
        _cancel_events[project_id] = cancel_event

        async def _job() -> None:
            from .indexation_queue import indexation_queue

            try:
                # The inpainting models need the whole 8 GB card, like
                # fast-mode matching: reserve the full heavy budget.
                async with indexation_queue.heavy_slot(
                    "cleanup", slots=indexation_queue.MAX_CONCURRENT
                ):
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        cls._run_full_cleanup_sync,
                        project_id,
                        Path(source),
                        list(state.zones),
                        cancel_event,
                    )
            except CleanupCancelled:
                cls._update_state(
                    project_id,
                    status="idle",
                    progress=0.0,
                    message="Cleanup cancelled.",
                    error=None,
                )
            except Exception as exc:
                logger.exception("Cleanup failed for project %s", project_id)
                cls._update_state(
                    project_id,
                    status="error",
                    message=None,
                    error=str(exc),
                )
            finally:
                _cancel_events.pop(project_id, None)
                _running_tasks.pop(project_id, None)
                from .propainter_adapter import ProPainterEngine

                ProPainterEngine.unload()

        _running_tasks[project_id] = asyncio.create_task(_job())

    @classmethod
    async def cancel(cls, project_id: str) -> None:
        event = _cancel_events.get(project_id)
        if event is not None:
            event.set()

    @classmethod
    async def stream_state(cls, project_id: str) -> AsyncIterator[CleanupState]:
        """Poll-based SSE stream; ends when the job reaches a terminal state."""
        last_payload = None
        while True:
            state = cls.get_state(project_id)
            payload = state.model_dump_json()
            if payload != last_payload:
                last_payload = payload
                yield state
            if state.status in ("complete", "error", "idle"):
                return
            await asyncio.sleep(0.5)

    # -- preview -----------------------------------------------------------

    @classmethod
    async def render_preview(cls, project_id: str, *, timestamp: float) -> dict:
        project = cls._require_pure_project(project_id)
        source = project.original_video_path or project.video_path
        if not source or not Path(source).exists():
            raise ValueError("Project video not found")
        state = project.cleanup or CleanupState()
        if not state.zones:
            raise ValueError("Draw at least one cleanup zone first")

        from .indexation_queue import indexation_queue

        async with indexation_queue.heavy_slot(
            "cleanup_preview", slots=indexation_queue.MAX_CONCURRENT
        ):
            await asyncio.get_running_loop().run_in_executor(
                None,
                cls._render_preview_sync,
                project_id,
                Path(source),
                list(state.zones),
                timestamp,
            )
        return {
            "before_url": f"/api/projects/{project_id}/cleanup/preview/before",
            "after_url": f"/api/projects/{project_id}/cleanup/preview/after",
        }

    @classmethod
    def preview_path(cls, project_id: str, which: str) -> Path:
        return cls.get_cleanup_dir(project_id) / f"preview_{which}.mp4"

    # -- geometry ----------------------------------------------------------

    @staticmethod
    def _zone_rect_px(zone: CleanupZone, width: int, height: int) -> tuple[int, int, int, int]:
        x = int(round(zone.x * width))
        y = int(round(zone.y * height))
        w = max(1, int(round(zone.w * width)))
        h = max(1, int(round(zone.h * height)))
        x = min(max(0, x), width - 1)
        y = min(max(0, y), height - 1)
        w = min(w, width - x)
        h = min(h, height - y)
        return x, y, w, h

    @staticmethod
    def _crop_region(
        rect: tuple[int, int, int, int], width: int, height: int
    ) -> tuple[int, int, int, int]:
        x, y, w, h = rect
        margin_x = max(CROP_MARGIN_MIN_PX, int(w * CROP_MARGIN_FRACTION))
        margin_y = max(CROP_MARGIN_MIN_PX, int(h * CROP_MARGIN_FRACTION))
        cx0 = max(0, x - margin_x)
        cy0 = max(0, y - margin_y)
        cx1 = min(width, x + w + margin_x)
        cy1 = min(height, y + h + margin_y)
        # Align origin down and size up to CROP_ALIGN, clamped to the frame.
        cx0 = (cx0 // CROP_ALIGN) * CROP_ALIGN
        cy0 = (cy0 // CROP_ALIGN) * CROP_ALIGN
        cw = min(width - cx0, -(-(cx1 - cx0) // CROP_ALIGN) * CROP_ALIGN)
        ch = min(height - cy0, -(-(cy1 - cy0) // CROP_ALIGN) * CROP_ALIGN)
        # Keep /8 alignment even when clamped at the frame edge.
        cw -= cw % 8
        ch -= ch % 8
        return cx0, cy0, cw, ch

    # -- text detection ----------------------------------------------------

    @classmethod
    def _text_mask(cls, crop_bgr: np.ndarray) -> np.ndarray:
        """Candidate white-text pixels inside a (rect-sized) BGR crop."""
        as_int = crop_bgr.astype(np.int16)
        cmax = as_int.max(axis=2)
        cmin = as_int.min(axis=2)
        bright = cmax >= TEXT_LUMA_MIN
        unsaturated = (cmax - cmin) <= TEXT_SATURATION_MAX
        candidate = bright & unsaturated
        if not candidate.any():
            return np.zeros(crop_bgr.shape[:2], dtype=bool)
        gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        kernel = np.ones((3, 3), np.uint8)
        gradient = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, kernel)
        edges = (gradient >= TEXT_GRADIENT_MIN).astype(np.uint8)
        near_edge = cv2.dilate(edges, kernel, iterations=TEXT_GRADIENT_REACH) > 0
        return candidate & near_edge

    @classmethod
    def _scores_to_spans(cls, scores: list[float], total_frames: int) -> list[tuple[int, int]]:
        """Hysteresis + smoothing over per-frame text scores."""
        present = np.zeros(len(scores), dtype=bool)
        on = False
        for i, score in enumerate(scores):
            if not on and score >= TEXT_SCORE_ON:
                on = True
            elif on and score <= TEXT_SCORE_OFF:
                on = False
            present[i] = on

        # Close short gaps.
        spans = cls._bool_to_spans(present)
        merged: list[tuple[int, int]] = []
        for span in spans:
            if merged and span[0] - merged[-1][1] <= TEXT_GAP_CLOSE_FRAMES:
                merged[-1] = (merged[-1][0], span[1])
            else:
                merged.append(span)
        # Drop tiny islands, pad the survivors.
        result = []
        for start, end in merged:
            if end - start <= TEXT_ISLAND_DROP_FRAMES:
                continue
            start = max(0, start - TEXT_SPAN_PAD_FRAMES)
            end = min(total_frames, end + TEXT_SPAN_PAD_FRAMES)
            if result and start <= result[-1][1]:
                result[-1] = (result[-1][0], end)
            else:
                result.append((start, end))
        return result

    @staticmethod
    def _bool_to_spans(values: np.ndarray) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start = None
        for i, value in enumerate(values):
            if value and start is None:
                start = i
            elif not value and start is not None:
                spans.append((start, i))
                start = None
        if start is not None:
            spans.append((start, len(values)))
        return spans

    # -- planning ----------------------------------------------------------

    @classmethod
    def _plan_zones(
        cls,
        video_path: Path,
        zones: list[CleanupZone],
        cancel_event: threading.Event | None,
        progress_cb: Callable[[float, str], None] | None,
        *,
        frame_range: tuple[int, int] | None = None,
        score_dump: list | None = None,
    ) -> tuple[list[_ZonePlan], int, float, int, int]:
        """Detection pass. Returns (plans, total_frames, fps, width, height)."""
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"Cannot open video: {video_path}")
        try:
            fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            declared_total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

            plans = [
                _ZonePlan(
                    zone=zone,
                    rect=cls._zone_rect_px(zone, width, height),
                    crop=cls._crop_region(
                        cls._zone_rect_px(zone, width, height), width, height
                    ),
                )
                for zone in zones
            ]
            subtitle_plans = [p for p in plans if p.zone.kind == "subtitle"]

            range_start, range_end = frame_range or (0, declared_total or 1 << 31)
            scores: dict[int, list[float]] = {id(p): [] for p in subtitle_plans}

            frame_index = 0
            observed_end = range_start
            if range_start > 0:
                capture.set(cv2.CAP_PROP_POS_FRAMES, range_start)
                frame_index = range_start
            while frame_index < range_end:
                ok, frame = capture.read()
                if not ok:
                    break
                for plan in subtitle_plans:
                    x, y, w, h = plan.rect
                    mask = cls._text_mask(frame[y : y + h, x : x + w])
                    score = float(mask.sum()) / float(w * h)
                    scores[id(plan)].append(score)
                    if score_dump is not None:
                        score_dump.append((frame_index, plan.zone.id, score))
                frame_index += 1
                observed_end = frame_index
                if cancel_event is not None and cancel_event.is_set():
                    raise CleanupCancelled()
                if progress_cb and declared_total and frame_index % 120 == 0:
                    progress_cb(
                        min(1.0, (frame_index - range_start) / max(1, (min(range_end, declared_total) - range_start))),
                        f"Analyzing subtitles… frame {frame_index}",
                    )

            total_frames = observed_end
            for plan in plans:
                if plan.zone.kind == "watermark":
                    plan.spans = [(range_start, total_frames)]
                else:
                    plan.spans = [
                        (range_start + s, range_start + e)
                        for s, e in cls._scores_to_spans(
                            scores[id(plan)], total_frames - range_start
                        )
                    ]
            return plans, total_frames, fps, width, height
        finally:
            capture.release()

    @classmethod
    def _spans_to_clips(cls, plan: _ZonePlan, total_frames: int) -> list[_Clip]:
        """Chunk spans into model-sized clips.

        Span-edge context frames are clean (mask inactive) references; a
        mid-span chunk boundary instead overlaps the neighbouring chunk with
        the mask kept active.
        """
        clips: list[_Clip] = []
        for span_start, span_end in plan.spans:
            lead = max(0, span_start - CLIP_CONTEXT_FRAMES)
            tail = min(total_frames, span_end + CLIP_CONTEXT_FRAMES)
            if tail - lead <= CLIP_MAX_FRAMES:
                clips.append(
                    _Clip(
                        zone_plan=plan,
                        frame_start=lead,
                        frame_end=tail,
                        out_start=span_start,
                        out_end=span_end,
                        mask_start=span_start,
                        mask_end=span_end,
                    )
                )
                continue
            # Chunk the active region.
            chunk = CLIP_MAX_FRAMES - 2 * CLIP_CHUNK_OVERLAP
            position = span_start
            while position < span_end:
                out_end = min(span_end, position + chunk)
                window_start = max(lead, position - CLIP_CHUNK_OVERLAP)
                window_end = min(tail, out_end + CLIP_CHUNK_OVERLAP)
                clips.append(
                    _Clip(
                        zone_plan=plan,
                        frame_start=window_start,
                        frame_end=window_end,
                        out_start=position,
                        out_end=out_end,
                        mask_start=span_start,
                        mask_end=span_end,
                    )
                )
                position = out_end
        return clips

    # -- inpainting --------------------------------------------------------

    @classmethod
    def _collect_clip_frames(
        cls,
        capture: cv2.VideoCapture,
        clip: _Clip,
        current_pos: int,
    ) -> tuple[np.ndarray, int]:
        """Sequentially read the clip's crop frames. Returns (frames, new_pos)."""
        cx, cy, cw, ch = clip.zone_plan.crop
        if current_pos != clip.frame_start:
            capture.set(cv2.CAP_PROP_POS_FRAMES, clip.frame_start)
            current_pos = clip.frame_start
        frames = []
        for _ in range(clip.frame_end - clip.frame_start):
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame[cy : cy + ch, cx : cx + cw].copy())
            current_pos += 1
        if not frames:
            raise RuntimeError("Failed to read clip frames")
        return np.stack(frames), current_pos

    @classmethod
    def _build_clip_masks(
        cls, clip: _Clip, crop_frames_bgr: np.ndarray
    ) -> np.ndarray:
        """Per-frame uint8 masks (255 = inpaint) in crop coords."""
        plan = clip.zone_plan
        cx, cy, cw, ch = plan.crop
        x, y, w, h = plan.rect
        rx, ry = x - cx, y - cy

        length = crop_frames_bgr.shape[0]
        masks = np.zeros((length, ch, cw), dtype=np.uint8)

        if plan.zone.kind == "watermark":
            rect_mask = np.zeros((ch, cw), dtype=np.uint8)
            rect_mask[ry : ry + h, rx : rx + w] = 255
        else:
            if os.environ.get("PURE_CLEANUP_FULL_RECT_MASK"):
                rect_mask = np.zeros((ch, cw), dtype=np.uint8)
                rect_mask[ry : ry + h, rx : rx + w] = 255
            else:
                # Static-per-span mask: union of per-frame text masks over the
                # ACTIVE frames of this clip, dilated, clipped to the rect.
                union = np.zeros((h, w), dtype=bool)
                for i in range(length):
                    absolute = clip.frame_start + i
                    if clip.mask_start <= absolute < clip.mask_end:
                        crop = crop_frames_bgr[i]
                        union |= cls._text_mask(crop[ry : ry + h, rx : rx + w])
                if not union.any():
                    # Detection said text is here; fall back to the full rect.
                    union[:] = True
                kernel = np.ones((3, 3), np.uint8)
                dilated = cv2.dilate(
                    union.astype(np.uint8), kernel,
                    iterations=MASK_DILATE_ITERATIONS,
                )
                rect_mask = np.zeros((ch, cw), dtype=np.uint8)
                rect_mask[ry : ry + h, rx : rx + w] = dilated * 255

        for i in range(length):
            absolute = clip.frame_start + i
            if clip.mask_start <= absolute < clip.mask_end:
                masks[i] = rect_mask
        return masks

    @classmethod
    def _inpaint_clip_with_ladder(
        cls,
        frames_bgr: np.ndarray,
        masks: np.ndarray,
        *,
        status_cb: Callable[[str], None] | None = None,
    ) -> np.ndarray:
        """Run ProPainter with the OOM ladder. Input/output BGR uint8."""
        import torch

        from .propainter_adapter import ProPainterEngine

        length, ch, cw = frames_bgr.shape[:3]

        scale = 1.0
        long_side = max(cw, ch)
        if long_side > MODEL_MAX_LONG_SIDE:
            scale = MODEL_MAX_LONG_SIDE / long_side

        attempts: list[tuple[int, float]] = []
        for subvideo in SUBVIDEO_LADDER:
            attempts.append((subvideo, scale))
        attempts.append((SUBVIDEO_LADDER[-1], scale * OOM_DOWNSCALE_FACTOR))
        attempts.append(
            (SUBVIDEO_LADDER[-1], scale * OOM_DOWNSCALE_FACTOR * OOM_DOWNSCALE_FACTOR)
        )

        last_error: Exception | None = None
        for attempt_index, (subvideo, attempt_scale) in enumerate(attempts):
            try:
                if attempt_scale < 1.0:
                    mw = int(cw * attempt_scale) // 8 * 8
                    mh = int(ch * attempt_scale) // 8 * 8
                    mw, mh = max(64, mw), max(64, mh)
                    model_frames = np.stack(
                        [
                            cv2.resize(f, (mw, mh), interpolation=cv2.INTER_AREA)
                            for f in frames_bgr
                        ]
                    )
                    model_masks = np.stack(
                        [
                            cv2.resize(
                                m, (mw, mh), interpolation=cv2.INTER_NEAREST
                            )
                            for m in masks
                        ]
                    )
                else:
                    model_frames, model_masks = frames_bgr, masks

                rgb = model_frames[..., ::-1]
                result_rgb = ProPainterEngine.inpaint_clip(
                    np.ascontiguousarray(rgb),
                    model_masks,
                    subvideo_length=subvideo,
                )
                result_bgr = result_rgb[..., ::-1]

                if attempt_scale < 1.0:
                    # Upscale the repaired crop back; the composite step only
                    # uses it inside the (original-resolution) mask.
                    result_bgr = np.stack(
                        [
                            cv2.resize(
                                f, (cw, ch), interpolation=cv2.INTER_CUBIC
                            )
                            for f in result_bgr
                        ]
                    )
                return np.ascontiguousarray(result_bgr)
            except torch.cuda.OutOfMemoryError as exc:  # type: ignore[attr-defined]
                last_error = exc
                torch.cuda.empty_cache()
                message = (
                    f"GPU out of memory — retrying (step {attempt_index + 2}/"
                    f"{len(attempts)}: subvideo={subvideo}, scale={attempt_scale:.2f})"
                )
                logger.warning(message)
                if status_cb:
                    status_cb(message)
        raise RuntimeError(
            f"ProPainter ran out of GPU memory at every ladder step: {last_error}"
        )

    @classmethod
    def _composite_alpha(cls, masks: np.ndarray) -> np.ndarray:
        """Feathered float alpha [T, H, W, 1] from uint8 masks."""
        alphas = []
        for mask in masks:
            alpha = (mask.astype(np.float32)) / 255.0
            alpha = cv2.GaussianBlur(alpha, (0, 0), COMPOSITE_FEATHER_SIGMA)
            alphas.append(alpha)
        return np.stack(alphas)[..., None]

    # -- full job ----------------------------------------------------------

    @classmethod
    def _run_full_cleanup_sync(
        cls,
        project_id: str,
        video_path: Path,
        zones: list[CleanupZone],
        cancel_event: threading.Event,
    ) -> None:
        started = time.monotonic()
        cleanup_dir = cls.get_cleanup_dir(project_id)
        spans_dir = cleanup_dir / "spans"
        spans_dir.mkdir(parents=True, exist_ok=True)

        def set_progress(progress: float, message: str) -> None:
            cls._update_state(
                project_id, progress=progress, message=message, status="running"
            )

        # Phase 1: detection.
        set_progress(0.01, "Analyzing subtitle presence…")
        plans, total_frames, fps, width, height = cls._plan_zones(
            video_path,
            zones,
            cancel_event,
            lambda p, m: set_progress(0.01 + 0.14 * p, m),
        )

        clips: list[_Clip] = []
        for plan in plans:
            clips.extend(cls._spans_to_clips(plan, total_frames))
        clips.sort(key=lambda c: c.frame_start)

        active_frames = sum(c.out_end - c.out_start for c in clips)
        logger.info(
            "Cleanup %s: %d zones, %d clips, %d/%d active frames",
            project_id,
            len(plans),
            len(clips),
            active_frames,
            total_frames,
        )

        # Phase 2: inpainting (with per-clip disk cache → resume for free).
        from .propainter_adapter import ProPainterEngine

        if clips:
            ProPainterEngine.load(
                progress_cb=lambda m: set_progress(0.15, m)
            )
        capture = cv2.VideoCapture(str(video_path))
        try:
            position = 0
            for clip_index, clip in enumerate(clips):
                if cancel_event.is_set():
                    raise CleanupCancelled()
                cache_path = spans_dir / clip.cache_name
                progress = 0.15 + 0.65 * (clip_index / max(1, len(clips)))
                zone_label = clip.zone_plan.zone.kind
                if cache_path.exists():
                    set_progress(
                        progress,
                        f"Clip {clip_index + 1}/{len(clips)} ({zone_label}) cached — skipping",
                    )
                    continue
                set_progress(
                    progress,
                    f"Inpainting clip {clip_index + 1}/{len(clips)} ({zone_label}, "
                    f"frames {clip.out_start}-{clip.out_end})…",
                )
                frames_bgr, position = cls._collect_clip_frames(
                    capture, clip, position
                )
                masks = cls._build_clip_masks(clip, frames_bgr)
                result = cls._inpaint_clip_with_ladder(
                    frames_bgr,
                    masks,
                    status_cb=lambda m: set_progress(progress, m),
                )
                # Persist only the composited output window.
                rel_start = clip.out_start - clip.frame_start
                rel_end = clip.out_end - clip.frame_start
                np.savez_compressed(
                    cache_path,
                    frames=result[rel_start:rel_end],
                    masks=masks[rel_start:rel_end],
                )
        finally:
            capture.release()

        ProPainterEngine.unload()

        # Phase 3: assembly.
        if cancel_event.is_set():
            raise CleanupCancelled()
        set_progress(0.82, "Re-encoding cleaned video…")
        clean_path = cls.get_clean_video_path(project_id)
        cls._assemble_video_sync(
            video_path,
            clean_path,
            clips,
            spans_dir,
            fps,
            width,
            height,
            total_frames,
            cancel_event,
            lambda p, m: set_progress(0.82 + 0.17 * p, m),
        )

        # Swap the project video to the cleaned file.
        project = ProjectService.load(project_id)
        if project is None:
            raise RuntimeError("Project not found")
        if not project.original_video_path:
            project.original_video_path = project.video_path
        project.video_path = str(clean_path)
        state = project.cleanup or CleanupState()
        state.status = "complete"
        state.progress = 1.0
        state.message = (
            f"Cleanup complete in {time.monotonic() - started:.0f}s "
            f"({active_frames} frames repaired)."
        )
        state.error = None
        state.cleaned_video_path = str(clean_path)
        state.updated_at = datetime.now()
        project.cleanup = state
        ProjectService.save(project)

        # The browser preview proxy is keyed by source path; the swap to
        # tiktok_clean.mp4 makes the old proxy stale and the new one is
        # generated on demand by the preview routes — nothing to do here.

    # -- assembly ----------------------------------------------------------

    @classmethod
    def _assemble_video_sync(
        cls,
        source_path: Path,
        output_path: Path,
        clips: list[_Clip],
        spans_dir: Path,
        fps: float,
        width: int,
        height: int,
        total_frames: int,
        cancel_event: threading.Event | None,
        progress_cb: Callable[[float, str], None] | None,
    ) -> None:
        """Composite cached clip results and encode (NVENC, CPU fallback)."""
        tmp_path = output_path.with_name(output_path.name + ".tmp.mp4")
        try:
            cls._stream_composite(
                source_path, tmp_path, clips, spans_dir, fps, width, height,
                total_frames, cancel_event, progress_cb, try_nvenc=True,
            )
        except CleanupCancelled:
            raise
        except RuntimeError as exc:
            logger.warning("NVENC assembly failed (%s); retrying on CPU", exc)
            tmp_path.unlink(missing_ok=True)
            cls._stream_composite(
                source_path, tmp_path, clips, spans_dir, fps, width, height,
                total_frames, cancel_event, progress_cb, try_nvenc=False,
            )

        ensure_bt709_tags(tmp_path)
        tmp_path.replace(output_path)

    @classmethod
    def _stream_composite(
        cls,
        source_path: Path,
        tmp_path: Path,
        clips: list[_Clip],
        spans_dir: Path,
        fps: float,
        width: int,
        height: int,
        total_frames: int,
        cancel_event: threading.Event | None,
        progress_cb: Callable[[float, str], None] | None,
        *,
        try_nvenc: bool,
    ) -> None:
        """One sequential decode → composite → raw-pipe-to-ffmpeg pass."""
        pending = sorted(clips, key=lambda c: c.out_start)
        active: list[dict] = []
        pending_index = 0

        process = cls._spawn_encoder(
            source_path, tmp_path, fps, width, height,
            try_nvenc=try_nvenc, include_audio=True,
        )

        capture = cv2.VideoCapture(str(source_path))
        try:
            frame_index = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                # Activate clips whose output window starts here.
                while (
                    pending_index < len(pending)
                    and pending[pending_index].out_start <= frame_index
                ):
                    clip = pending[pending_index]
                    pending_index += 1
                    cache_path = spans_dir / clip.cache_name
                    if not cache_path.exists():
                        logger.warning("Missing clip cache %s", cache_path)
                        continue
                    data = np.load(cache_path)
                    active.append(
                        {
                            "clip": clip,
                            "frames": data["frames"],
                            "alpha": cls._composite_alpha(data["masks"]),
                        }
                    )
                # Composite active clips.
                for entry in list(active):
                    clip = entry["clip"]
                    if frame_index >= clip.out_end:
                        active.remove(entry)
                        continue
                    if frame_index < clip.out_start:
                        continue
                    i = frame_index - clip.out_start
                    cx, cy, cw, ch = clip.zone_plan.crop
                    region = frame[cy : cy + ch, cx : cx + cw].astype(np.float32)
                    repaired = entry["frames"][i].astype(np.float32)
                    alpha = entry["alpha"][i]
                    blended = alpha * repaired + (1.0 - alpha) * region
                    frame[cy : cy + ch, cx : cx + cw] = np.clip(
                        blended, 0, 255
                    ).astype(np.uint8)

                try:
                    process.stdin.write(frame.tobytes())
                except BrokenPipeError as exc:
                    stderr = b""
                    try:
                        _, stderr = process.communicate(timeout=10)
                    except Exception:
                        pass
                    raise RuntimeError(
                        "Encoder terminated early: "
                        + stderr.decode("utf-8", "replace")[-800:]
                    ) from exc
                frame_index += 1
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    raise CleanupCancelled()
                if progress_cb and frame_index % 240 == 0:
                    progress_cb(
                        frame_index / max(1, total_frames),
                        f"Encoding… frame {frame_index}/{total_frames}",
                    )
        finally:
            capture.release()
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass

        return_code = process.wait(timeout=1200)
        if return_code != 0:
            stderr = process.stderr.read() if process.stderr else b""
            raise RuntimeError(
                "Cleanup encoding failed: "
                + stderr.decode("utf-8", "replace")[-800:]
            )

    @staticmethod
    def _spawn_encoder(
        source_path: Path,
        output_path: Path,
        fps: float,
        width: int,
        height: int,
        *,
        try_nvenc: bool,
        include_audio: bool = False,
    ) -> subprocess.Popen:
        if try_nvenc:
            video_args = [
                "-c:v", "h264_nvenc",
                "-preset", "p5",
                "-rc", "constqp",
                "-qp", "19",
            ]
        else:
            video_args = ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            # Raw composited frames on stdin.
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", f"{fps:.6f}",
            "-i", "pipe:0",
        ]
        if include_audio:
            # Original file only for its (stream-copied) audio.
            cmd += [
                "-i", str(source_path),
                "-map", "0:v:0",
                "-map", "1:a:0?",
                "-c:a", "copy",
            ]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        cmd += [
            *video_args,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        cmd = rewrite_media_command(cmd)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=get_media_subprocess_env(cmd),
        )

    # -- preview -----------------------------------------------------------

    @classmethod
    def _render_preview_sync(
        cls,
        project_id: str,
        video_path: Path,
        zones: list[CleanupZone],
        timestamp: float,
    ) -> None:
        cleanup_dir = cls.get_cleanup_dir(project_id)
        cleanup_dir.mkdir(parents=True, exist_ok=True)

        probe = cv2.VideoCapture(str(video_path))
        if not probe.isOpened():
            raise RuntimeError("Cannot open video for preview")
        fps = probe.get(cv2.CAP_PROP_FPS) or 30.0
        declared_total = int(probe.get(cv2.CAP_PROP_FRAME_COUNT))
        probe.release()

        start_frame = max(0, int((timestamp - PREVIEW_LEAD_SECONDS) * fps))
        end_frame = min(
            declared_total or (1 << 31),
            start_frame + int((PREVIEW_LEAD_SECONDS + PREVIEW_DURATION_SECONDS) * fps),
        )
        if end_frame - start_frame < 8:
            raise ValueError("Preview window too short — pick an earlier timestamp")

        plans, total_frames, fps, width, height = cls._plan_zones(
            video_path,
            zones,
            None,
            None,
            frame_range=(start_frame, end_frame),
        )

        clips: list[_Clip] = []
        for plan in plans:
            # Clamp context to the preview window: everything is in memory.
            for clip in cls._spans_to_clips(plan, total_frames):
                clip.frame_start = max(clip.frame_start, start_frame)
                clips.append(clip)
        clips.sort(key=lambda c: c.frame_start)

        from .propainter_adapter import ProPainterEngine

        ProPainterEngine.load()

        capture = cv2.VideoCapture(str(video_path))
        results: list[tuple[_Clip, np.ndarray, np.ndarray]] = []
        try:
            position = -1
            for clip in clips:
                frames_bgr, position = cls._collect_clip_frames(
                    capture, clip, position if position >= 0 else -1
                )
                masks = cls._build_clip_masks(clip, frames_bgr)
                repaired = cls._inpaint_clip_with_ladder(frames_bgr, masks)
                rel_start = clip.out_start - clip.frame_start
                rel_end = clip.out_end - clip.frame_start
                results.append(
                    (
                        clip,
                        repaired[rel_start:rel_end],
                        cls._composite_alpha(masks[rel_start:rel_end]),
                    )
                )

            # Write before/after windows.
            for which in ("before", "after"):
                writer_path = cls.preview_path(project_id, which)
                process = cls._spawn_encoder(
                    video_path, writer_path.with_suffix(".tmp.mp4"),
                    fps, width, height, try_nvenc=False,
                )
                capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
                for frame_index in range(start_frame, end_frame):
                    ok, frame = capture.read()
                    if not ok:
                        break
                    if which == "after":
                        for clip, repaired, alpha in results:
                            if not (clip.out_start <= frame_index < clip.out_end):
                                continue
                            i = frame_index - clip.out_start
                            cx, cy, cw, ch = clip.zone_plan.crop
                            region = frame[cy : cy + ch, cx : cx + cw].astype(
                                np.float32
                            )
                            frame[cy : cy + ch, cx : cx + cw] = np.clip(
                                alpha[i] * repaired[i].astype(np.float32)
                                + (1.0 - alpha[i]) * region,
                                0,
                                255,
                            ).astype(np.uint8)
                    process.stdin.write(frame.tobytes())
                process.stdin.close()
                if process.wait(timeout=300) != 0:
                    stderr = process.stderr.read() if process.stderr else b""
                    raise RuntimeError(
                        "Preview encoding failed: "
                        + stderr.decode("utf-8", "replace")[-500:]
                    )
                writer_path.with_suffix(".tmp.mp4").replace(writer_path)
        finally:
            capture.release()
