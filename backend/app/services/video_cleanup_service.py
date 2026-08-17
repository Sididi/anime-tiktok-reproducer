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
from contextlib import suppress
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
# Model input cap (long side) before downscale — VRAM budget AND speed: GPU
# stage cost scales with pixel area, repairs are confined to the mask so the
# upscale-back cost is limited to the strokes (quality A/B'd 2026-08-13).
MODEL_MAX_LONG_SIDE = 512
# Before feeding the model, the crop is tightened to the bounding box of the
# actual mask (+margin): a one-line subtitle doesn't pay for the whole rect.
BBOX_MARGIN_PX = 24
# RAFT returns NaN flows below 128px on either axis (verified empirically);
# every tensor the model sees must respect this floor.
MIN_MODEL_SIDE_PX = 128

# Span → clip chunking (frames). Context frames are clean references at span
# edges; mid-span chunk boundaries overlap with masks kept active. Longer
# clips amortize per-clip fixed costs (python/setup/save).
CLIP_CONTEXT_FRAMES = 10
CLIP_MAX_FRAMES = 240
CLIP_CHUNK_OVERLAP = 10

# Inpaint mask dilation inside the rect (pixels ~ iterations). Must leave
# enough margin past the text's dark outline (~3px, plus 2px of half-res
# quantization) for the eroded+feathered composite alpha: with erode 2 and
# sigma 2.5, original pixels show through up to ~6px inside the mask edge —
# 12px of dilation keeps the outline at >=7px depth (<=2% show-through;
# 9px left visible outline traces on bright backgrounds).
MASK_DILATE_ITERATIONS = 12
# Per-frame subtitle masks: each active frame's mask is the union of the raw
# text masks over a ±window. Tight per-frame holes let the model propagate the
# real background revealed when the text changes (the per-clip union mask made
# every fill a hallucination and the composite a visible band).
TEXT_MASK_WINDOW_FRAMES = 5
# Feather (Gaussian sigma in px) when compositing back; the alpha is eroded
# first so the blend band sits inside the repaired margin.
COMPOSITE_FEATHER_SIGMA = 2.5
COMPOSITE_ALPHA_ERODE_PX = 2
# Optional unsharp on the upscaled fill, applied inside the mask only
# (env PURE_CLEANUP_FILL_UNSHARP overrides; 0 disables).
FILL_UNSHARP_AMOUNT = 0.0
# Bumped whenever the npz clip-cache content semantics change (masks moved
# from per-clip union to per-frame in v2); stale versions are ignored.
CLIP_CACHE_VERSION = 2

# Temporal stride: the model processes every Nth frame; skipped frames get
# their mask region filled by blending the two neighbouring fills. The mask
# is static per span and thin, surrounding real pixels stay exact — only
# stroke interiors can lag (anime content is 8-12 real fps). Watermarks are
# small static logos whose fills vary very slowly → stride harder.
# Stride 3 owner-approved via A/B on real content incl. the highest-motion
# window (2026-08-14) — visually indistinguishable from stride 2.
TEMPORAL_STRIDE = 3
TEMPORAL_STRIDE_SMALL_CROP = 4

# A skipped (strided) frame's fill is blended from the two neighbouring model
# frames; its true text pixels are only guaranteed covered by those frames'
# masks when the window is at least the stride.
assert TEXT_MASK_WINDOW_FRAMES >= TEMPORAL_STRIDE_SMALL_CROP
# Mid-span chunk boundaries only carry CLIP_CHUNK_OVERLAP frames of shared
# context; a wider window would truncate at the seam and masks would differ
# between the two chunks for the same output frame.
assert TEXT_MASK_WINDOW_FRAMES <= CLIP_CHUNK_OVERLAP

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
        return (
            f"{z.kind}_{z.id}_{self.out_start:06d}_{self.out_end:06d}"
            f"_v{CLIP_CACHE_VERSION}.npz"
        )


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
        # RAFT NaN floor: the crop (the largest tensor the model can see)
        # must be at least MIN_MODEL_SIDE_PX per axis.
        if cw < MIN_MODEL_SIDE_PX:
            cx0 = max(0, min(cx0, width - MIN_MODEL_SIDE_PX))
            cw = min(width - cx0, MIN_MODEL_SIDE_PX)
        if ch < MIN_MODEL_SIDE_PX:
            cy0 = max(0, min(cy0, height - MIN_MODEL_SIDE_PX))
            ch = min(height - cy0, MIN_MODEL_SIDE_PX)
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
                    crop = frame[y : y + h, x : x + w]
                    if w >= 128 and h >= 32:
                        # Presence scoring is scale-invariant (fraction of
                        # area); half-res quarters the per-frame CPU cost.
                        crop = cv2.resize(
                            crop, (w // 2, h // 2), interpolation=cv2.INTER_AREA
                        )
                    mask = cls._text_mask(crop)
                    score = float(mask.sum()) / float(mask.shape[0] * mask.shape[1])
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
        # Tiny crops (watermarks) are launch-overhead-bound: double-length
        # clips halve the per-clip fixed costs at negligible VRAM.
        max_frames = (
            CLIP_MAX_FRAMES * 2
            if max(plan.crop[2], plan.crop[3]) < 320
            else CLIP_MAX_FRAMES
        )
        clips: list[_Clip] = []
        for span_start, span_end in plan.spans:
            lead = max(0, span_start - CLIP_CONTEXT_FRAMES)
            tail = min(total_frames, span_end + CLIP_CONTEXT_FRAMES)
            if tail - lead <= max_frames:
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
            chunk = max_frames - 2 * CLIP_CHUNK_OVERLAP
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
    def _per_frame_text_masks(
        cls, clip: _Clip, crop_frames_bgr: np.ndarray
    ) -> list[np.ndarray | None]:
        """Half-resolution raw text mask per frame (None on context frames).

        Mirrors exactly what the decode thread accumulates during the full
        run, so the preview path (which recomputes here) cannot diverge.
        """
        plan = clip.zone_plan
        cx, cy, _, _ = plan.crop
        x, y, w, h = plan.rect
        rx, ry = x - cx, y - cy
        mh, mw = max(1, h // 2), max(1, w // 2)
        out: list[np.ndarray | None] = []
        for i in range(crop_frames_bgr.shape[0]):
            absolute = clip.frame_start + i
            if not (clip.mask_start <= absolute < clip.mask_end):
                out.append(None)
                continue
            rect_crop = crop_frames_bgr[i][ry : ry + h, rx : rx + w]
            if (mh, mw) != (h, w):
                rect_crop = cv2.resize(
                    rect_crop, (mw, mh), interpolation=cv2.INTER_AREA
                )
            out.append(cls._text_mask(rect_crop))
        return out

    @classmethod
    def _build_clip_masks(
        cls,
        clip: _Clip,
        crop_frames_bgr: np.ndarray,
        text_masks: list[np.ndarray | None] | None = None,
    ) -> np.ndarray:
        """Per-frame uint8 masks (255 = inpaint) in crop coords.

        ``text_masks`` is the per-frame half-resolution raw text mask list
        (rect coords, None on context frames) accumulated on the decode
        thread; when None it is recomputed here from the frames (preview
        path). Each active frame's mask is the union of the raw masks over
        a ±TEXT_MASK_WINDOW_FRAMES window, dilated — tight per-frame holes
        instead of the former per-clip union band.
        """
        plan = clip.zone_plan
        cx, cy, cw, ch = plan.crop
        x, y, w, h = plan.rect
        rx, ry = x - cx, y - cy

        length = crop_frames_bgr.shape[0]
        masks = np.zeros((length, ch, cw), dtype=np.uint8)

        def _active(i: int) -> bool:
            absolute = clip.frame_start + i
            return clip.mask_start <= absolute < clip.mask_end

        def _fill_full_rect() -> np.ndarray:
            rect_mask = np.zeros((ch, cw), dtype=np.uint8)
            rect_mask[ry : ry + h, rx : rx + w] = 255
            for i in range(length):
                if _active(i):
                    masks[i] = rect_mask
            return masks

        if plan.zone.kind == "watermark" or os.environ.get(
            "PURE_CLEANUP_FULL_RECT_MASK"
        ):
            return _fill_full_rect()

        if text_masks is None:
            text_masks = cls._per_frame_text_masks(clip, crop_frames_bgr)

        raw = [m for m in text_masks if m is not None]
        if not raw or not any(m.any() for m in raw):
            # Detection said text is here yet no strokes were found on any
            # frame; fall back to the full rect (rare — the warning feeds
            # the verification stats).
            logger.warning(
                "Cleanup clip %s: empty text-mask union, falling back to "
                "the full rect", clip.cache_name,
            )
            return _fill_full_rect()

        dilate_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * MASK_DILATE_ITERATIONS + 1, 2 * MASK_DILATE_ITERATIONS + 1),
        )
        window = TEXT_MASK_WINDOW_FRAMES
        # Sliding ±window union via an incremental count array (2 updates per
        # frame instead of re-OR-ing the whole window).
        count = np.zeros(raw[0].shape, dtype=np.uint16)
        added_hi = -1
        removed_lo = 0
        empty_active = 0
        for i in range(length):
            while added_hi < min(length - 1, i + window):
                added_hi += 1
                m = text_masks[added_hi]
                if m is not None:
                    count += (m > 0).astype(np.uint16)
            while removed_lo < i - window:
                m = text_masks[removed_lo]
                if m is not None:
                    count -= (m > 0).astype(np.uint16)
                removed_lo += 1
            if not _active(i):
                continue
            base = (count > 0).astype(np.uint8)
            if not base.any():
                # Window sees no candidate pixels at all: nothing visible to
                # remove on this frame — leave it untouched (falling back to
                # a wider union would resurrect the band on hysteresis
                # tails).
                empty_active += 1
                continue
            if base.shape != (h, w):
                base = cv2.resize(
                    base, (w, h), interpolation=cv2.INTER_NEAREST
                )
            dilated = cv2.dilate(base, dilate_kernel)
            masks[i, ry : ry + h, rx : rx + w] = dilated * 255
        if empty_active:
            logger.debug(
                "Cleanup clip %s: %d active frame(s) with empty window mask",
                clip.cache_name, empty_active,
            )
        return masks

    @staticmethod
    def _save_clip_atomic(path: Path, frames: np.ndarray, masks: np.ndarray) -> None:
        """Write-then-rename so a concurrent assembly reader never sees a
        partially written npz."""
        tmp = path.with_name(path.name + ".tmp.npz")
        np.savez(tmp, frames=frames, masks=masks)
        os.replace(tmp, path)

    @classmethod
    def _mask_bbox(cls, masks: np.ndarray, height: int, width: int) -> tuple[int, int, int, int]:
        """Aligned bounding box (+margin) of the union mask, in crop coords."""
        union = masks.any(axis=0)
        ys, xs = np.nonzero(union)
        if len(ys) == 0:
            return 0, 0, width, height
        y0 = max(0, int(ys.min()) - BBOX_MARGIN_PX)
        y1 = min(height, int(ys.max()) + 1 + BBOX_MARGIN_PX)
        x0 = max(0, int(xs.min()) - BBOX_MARGIN_PX)
        x1 = min(width, int(xs.max()) + 1 + BBOX_MARGIN_PX)
        # Grow to the minimum model size (conv pyramids need real extent).
        if y1 - y0 < MIN_MODEL_SIDE_PX:
            grow = MIN_MODEL_SIDE_PX - (y1 - y0)
            y0 = max(0, y0 - grow // 2)
            y1 = min(height, y0 + MIN_MODEL_SIDE_PX)
            y0 = max(0, y1 - MIN_MODEL_SIDE_PX)
        if x1 - x0 < MIN_MODEL_SIDE_PX:
            grow = MIN_MODEL_SIDE_PX - (x1 - x0)
            x0 = max(0, x0 - grow // 2)
            x1 = min(width, x0 + MIN_MODEL_SIDE_PX)
            x0 = max(0, x1 - MIN_MODEL_SIDE_PX)
        # Quantize extents UP to 64-px steps (origin aligned to 8): repeated
        # tensor shapes let cuDNN autotune amortize across clips — varying
        # shapes made benchmark-mode retune on every clip (measured).
        y0 = (y0 // 8) * 8
        x0 = (x0 // 8) * 8
        bh = -(-(y1 - y0) // 64) * 64
        bw = -(-(x1 - x0) // 64) * 64
        bh = min(bh, height - y0)
        bw = min(bw, width - x0)
        # Keep /8 alignment when clamped at the crop edge.
        bh -= bh % 8
        bw -= bw % 8
        if bh >= height - 64:
            y0, bh = 0, height
        if bw >= width - 64:
            x0, bw = 0, width
        return x0, y0, max(64, bw), max(64, bh)

    @classmethod
    def _inpaint_clip_with_ladder(
        cls,
        frames_bgr: np.ndarray,
        masks: np.ndarray,
        *,
        status_cb: Callable[[str], None] | None = None,
    ) -> np.ndarray:
        """Run ProPainter with the OOM ladder. Input/output BGR uint8.

        The model only sees the bounding box of the actual mask (+margin):
        GPU cost scales with pixel area, and a one-line subtitle should not
        pay for the full user-drawn rect.
        """
        import torch

        from .propainter_adapter import ProPainterEngine

        full_h, full_w = frames_bgr.shape[1:3]
        bx, by, bw, bh = cls._mask_bbox(masks, full_h, full_w)
        tightened = (bw * bh) < 0.9 * (full_w * full_h)
        if tightened:
            work_frames = np.ascontiguousarray(
                frames_bgr[:, by : by + bh, bx : bx + bw]
            )
            work_masks = np.ascontiguousarray(
                masks[:, by : by + bh, bx : bx + bw]
            )
        else:
            work_frames, work_masks = frames_bgr, masks

        result_work = cls._inpaint_temporal_strided(
            work_frames, work_masks, status_cb=status_cb
        )

        if not tightened:
            return result_work
        result = frames_bgr.copy()
        result[:, by : by + bh, bx : bx + bw] = result_work
        return result

    @classmethod
    def _inpaint_temporal_strided(
        cls,
        frames_bgr: np.ndarray,
        masks: np.ndarray,
        *,
        status_cb: Callable[[str], None] | None = None,
    ) -> np.ndarray:
        """Run the model on every stride-th frame; fill skipped frames' mask
        regions from the neighbouring fills (50/50 blend where both sides
        exist). Small (watermark) crops stride harder — static logos over
        slowly varying fills."""
        length = frames_bgr.shape[0]
        stride = (
            TEMPORAL_STRIDE_SMALL_CROP
            if max(frames_bgr.shape[1:3]) < 320
            else TEMPORAL_STRIDE
        )
        if stride <= 1 or length < 3 * stride:
            return cls._inpaint_ladder_inner(
                frames_bgr, masks, status_cb=status_cb
            )

        keep = sorted(set(range(0, length, stride)) | {length - 1})
        sub_result = cls._inpaint_ladder_inner(
            np.ascontiguousarray(frames_bgr[keep]),
            np.ascontiguousarray(masks[keep]),
            status_cb=status_cb,
        )
        fill_by_index = {orig: sub_result[k] for k, orig in enumerate(keep)}

        result = frames_bgr.copy()
        for orig, fill in fill_by_index.items():
            region = masks[orig] > 0
            result[orig][region] = fill[region]
        # A neighbour's fill outside its OWN mask is that neighbour's
        # original pixels — subtitle strokes included. With per-frame masks
        # the blend must therefore be restricted per pixel to where each
        # neighbour actually repaired; masks are ±window unions with
        # window >= stride, so every true text pixel of a skipped frame is
        # covered by both neighbours. (Static masks: both == region,
        # identical to the former 50/50 blend.)
        for prev_i, next_i in zip(keep, keep[1:]):
            blended = None
            fill_p = fill_by_index[prev_i]
            fill_n = fill_by_index[next_i]
            mask_p = masks[prev_i] > 0
            mask_n = masks[next_i] > 0
            for j in range(prev_i + 1, next_i):
                region = masks[j] > 0
                if not region.any():
                    continue
                both = region & mask_p & mask_n
                only_p = region & mask_p & ~mask_n
                only_n = region & mask_n & ~mask_p
                if both.any():
                    if blended is None:
                        blended = (
                            (fill_p.astype(np.uint16) + fill_n.astype(np.uint16))
                            // 2
                        ).astype(np.uint8)
                    result[j][both] = blended[both]
                if only_p.any():
                    result[j][only_p] = fill_p[only_p]
                if only_n.any():
                    result[j][only_n] = fill_n[only_n]
                # region pixels covered by neither neighbour are window
                # bleed, not text at frame j — the original stays.
        return result

    @staticmethod
    def _fill_is_near_black(
        result_bgr: np.ndarray, masks: np.ndarray, original_bgr: np.ndarray
    ) -> bool:
        """Canary: a mostly-black mid-frame fill means upstream flow/feature
        corruption (e.g. the RAFT sub-128px NaN pathology) — but only when
        the surrounding original content is NOT black itself. With per-frame
        stroke masks a black fill is the correct answer on fades/dark scenes
        (measured: every black fill on the reference project sat in >80%
        black surroundings)."""
        mid = result_bgr.shape[0] // 2
        region = masks[mid] > 0
        mid_region = result_bgr[mid][region]
        if not mid_region.size:
            return False
        if float((mid_region.max(axis=1) < 8).mean()) <= 0.9:
            return False
        surroundings = original_bgr[mid][~region]
        if not surroundings.size:
            return True
        return float((surroundings.max(axis=1) < 8).mean()) < 0.5

    @classmethod
    def _canary_retry_fp32(
        cls,
        frames_bgr: np.ndarray,
        masks: np.ndarray,
        *,
        status_cb: Callable[[str], None] | None = None,
    ) -> np.ndarray | None:
        """One bounded fp32 retry of a clip whose fill tripped the canary.
        Returns None (keep the suspect fill) if the retry fails or trips
        the canary again — never fail the whole job over it."""
        from .propainter_adapter import ProPainterEngine

        try:
            ProPainterEngine.load(fp16=False)
            retry = cls._inpaint_ladder_inner(
                frames_bgr,
                masks,
                status_cb=status_cb,
                allow_canary_retry=False,
                _attempts_override=[
                    (SUBVIDEO_LADDER[-1], 1.0),
                    (SUBVIDEO_LADDER[-1], OOM_DOWNSCALE_FACTOR),
                ],
            )
        except Exception:
            logger.exception("Canary fp32 retry failed; keeping the suspect fill")
            return None
        finally:
            # MUST restore, else every later clip runs fp32.
            ProPainterEngine.load(fp16=True)
        if cls._fill_is_near_black(retry, masks, frames_bgr):
            return None
        return retry

    @classmethod
    def _inpaint_ladder_inner(
        cls,
        frames_bgr: np.ndarray,
        masks: np.ndarray,
        *,
        status_cb: Callable[[str], None] | None = None,
        allow_canary_retry: bool = True,
        _attempts_override: list[tuple[int, float]] | None = None,
    ) -> np.ndarray:
        import torch

        from .propainter_adapter import ProPainterEngine

        length, ch, cw = frames_bgr.shape[:3]

        scale = 1.0
        long_side = max(cw, ch)
        if long_side > MODEL_MAX_LONG_SIDE:
            scale = MODEL_MAX_LONG_SIDE / long_side

        if _attempts_override is not None:
            attempts = list(_attempts_override)
        else:
            attempts = []
            for subvideo in SUBVIDEO_LADDER:
                attempts.append((subvideo, scale))
            attempts.append((SUBVIDEO_LADDER[-1], scale * OOM_DOWNSCALE_FACTOR))
            attempts.append(
                (
                    SUBVIDEO_LADDER[-1],
                    scale * OOM_DOWNSCALE_FACTOR * OOM_DOWNSCALE_FACTOR,
                )
            )

        last_error: Exception | None = None
        for attempt_index, (subvideo, attempt_scale) in enumerate(attempts):
            try:
                if attempt_scale < 1.0:
                    mw = int(cw * attempt_scale) // 8 * 8
                    mh = int(ch * attempt_scale) // 8 * 8
                    # Never below the RAFT NaN floor (crop itself is >= floor).
                    mw = min(cw, max(MIN_MODEL_SIDE_PX, mw))
                    mh = min(ch, max(MIN_MODEL_SIDE_PX, mh))
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
                # Tiny crops (watermarks) are kernel-launch-bound: wider
                # temporal windows halve the launch count at negligible cost.
                # Large crops drop distant reference frames instead — thin
                # strokes fill from immediate neighbours, and refs were ~30%
                # of the transformer tokens.
                model_long_side = max(model_frames.shape[1:3])
                result_rgb = ProPainterEngine.inpaint_clip(
                    np.ascontiguousarray(rgb),
                    model_masks,
                    subvideo_length=subvideo,
                    neighbor_length=20 if model_long_side < 320 else 10,
                    ref_stride=20 if model_long_side < 320 else 80,
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
                    unsharp = FILL_UNSHARP_AMOUNT
                    env_unsharp = os.environ.get("PURE_CLEANUP_FILL_UNSHARP")
                    if env_unsharp:
                        try:
                            unsharp = float(env_unsharp)
                        except ValueError:
                            logger.warning(
                                "Ignoring invalid PURE_CLEANUP_FILL_UNSHARP=%r",
                                env_unsharp,
                            )
                    if unsharp > 0:
                        # The upscaled fill is softer than the native-res
                        # surroundings; sharpen inside the mask only.
                        for i in range(length):
                            region = masks[i] > 0
                            if not region.any():
                                continue
                            f = result_bgr[i]
                            sharp = cv2.addWeighted(
                                f, 1.0 + unsharp,
                                cv2.GaussianBlur(f, (0, 0), 1.0), -unsharp,
                                0,
                            )
                            f[region] = sharp[region]

                if cls._fill_is_near_black(result_bgr, masks, frames_bgr):
                    logger.error(
                        "Inpaint produced a near-black fill (%dx%d model "
                        "input) — output is likely corrupted",
                        model_frames.shape[2], model_frames.shape[1],
                    )
                    if allow_canary_retry:
                        retried = cls._canary_retry_fp32(
                            frames_bgr, masks, status_cb=status_cb
                        )
                        if retried is not None:
                            return np.ascontiguousarray(retried)
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
        """Feathered float alpha [T, H, W, 1] from uint8 masks.

        The binary mask is eroded before blurring so the blend band sits
        entirely inside the repaired margin (>= 9px of dilation) instead of
        straddling the seam."""
        erode_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * COMPOSITE_ALPHA_ERODE_PX + 1, 2 * COMPOSITE_ALPHA_ERODE_PX + 1),
        )
        alphas = []
        for mask in masks:
            binary = (mask > 0).astype(np.uint8)
            if COMPOSITE_ALPHA_ERODE_PX > 0 and binary.any():
                eroded = cv2.erode(binary, erode_kernel)
                if eroded.any():
                    # Guard: never erase a mask too thin to survive erosion.
                    binary = eroded
            alpha = cv2.GaussianBlur(
                binary.astype(np.float32), (0, 0), COMPOSITE_FEATHER_SIGMA
            )
            alphas.append(alpha)
        return np.stack(alphas)[..., None]

    # -- full job ----------------------------------------------------------

    @classmethod
    def _run_inpaint_pass(
        cls,
        video_path: Path,
        clips: list[_Clip],
        spans_dir: Path,
        cancel_event: threading.Event,
        set_progress: Callable[[float, str], None],
    ) -> None:
        """Phase 2: single sequential decode pass over the video; each
        completed clip is inpainted on a 1-deep worker thread while the
        decoder keeps feeding the next clip."""
        # One sequential decode pass, zero seeks: every zone's active clip
        # crops from the same decoded frame. (The previous per-clip
        # seek-collection re-decoded up to a whole GOP — 5-12s on TikTok
        # media — at every zone alternation.)
        todo = [c for c in clips if not (spans_dir / c.cache_name).exists()]
        done_count = len(clips) - len(todo)
        if done_count:
            set_progress(0.15, f"{done_count}/{len(clips)} clip(s) cached — resuming")

        from concurrent.futures import ThreadPoolExecutor as _SavePool

        save_pool = _SavePool(max_workers=1)
        save_futures: list = []

        def _finish_clip(entry: dict) -> None:
            nonlocal done_count
            clip = entry["clip"]
            frames_bgr = np.stack(entry["frames"])
            # Per-frame text masks were computed on the decode thread;
            # building masks here is the windowed union + dilation.
            masks = cls._build_clip_masks(
                clip, frames_bgr, text_masks=entry.get("text_masks")
            )
            progress = 0.15 + 0.65 * (done_count / max(1, len(clips)))
            set_progress(
                progress,
                f"Inpainting clip {done_count + 1}/{len(clips)} "
                f"({clip.zone_plan.zone.kind}, frames {clip.out_start}-{clip.out_end})…",
            )
            result = cls._inpaint_clip_with_ladder(
                frames_bgr,
                masks,
                status_cb=lambda m: set_progress(progress, m),
            )
            rel_start = clip.out_start - clip.frame_start
            rel_end = clip.out_end - clip.frame_start
            # Off-thread, uncompressed: neither zlib nor disk I/O may sit
            # between two GPU clips.
            save_futures.append(
                save_pool.submit(
                    cls._save_clip_atomic,
                    spans_dir / clip.cache_name,
                    result[rel_start:rel_end],
                    masks[rel_start:rel_end],
                )
            )
            done_count += 1

        # The GPU inpaint of a completed clip runs in a single worker thread
        # (queue depth 1) so the decoder keeps feeding the next clip's frames
        # meanwhile — cv2 and torch both release the GIL.
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=1)
        inflight = None

        def _submit(entry: dict) -> None:
            nonlocal inflight
            if inflight is not None:
                inflight.result()  # backpressure + error propagation
            inflight = executor.submit(_finish_clip, entry)

        # One ffmpeg crop pipe per zone: decode happens in ffmpeg processes
        # (GIL-free, parallel); Python only reads small fixed-size crop
        # buffers. Full-frame cv2 decode on this thread was starving the GPU
        # worker's Python sections of the GIL (measured ~1.5s/clip).
        zone_plans: list = []
        for c in todo:
            if c.zone_plan not in zone_plans:
                zone_plans.append(c.zone_plan)
        readers: dict[int, tuple] = {}
        try:
            for plan in zone_plans:
                pcx, pcy, pcw, pch = plan.crop
                proc = cls._spawn_crop_reader(video_path, plan.crop)
                readers[id(plan)] = (proc, pcw, pch, pcw * pch * 3)

            active: list[dict] = []
            next_index = 0
            frame_index = 0
            while active or next_index < len(todo):
                crops: dict[int, np.ndarray] = {}
                eof = False
                for plan in zone_plans:
                    proc, pcw, pch, nbytes = readers[id(plan)]
                    buf = proc.stdout.read(nbytes)
                    if buf is None or len(buf) < nbytes:
                        eof = True
                        break
                    crops[id(plan)] = np.frombuffer(buf, dtype=np.uint8).reshape(
                        pch, pcw, 3
                    )
                if eof:
                    break
                if cancel_event.is_set():
                    raise CleanupCancelled()
                while (
                    next_index < len(todo)
                    and todo[next_index].frame_start <= frame_index
                ):
                    new_clip = todo[next_index]
                    entry: dict = {"clip": new_clip, "frames": []}
                    if new_clip.zone_plan.zone.kind == "subtitle":
                        # Per-frame masks at half resolution: 4x less
                        # per-frame CPU on this (GIL-sharing) decode thread;
                        # the mask dilation swallows the 2px quantization.
                        entry["text_masks"] = []
                    active.append(entry)
                    next_index += 1
                for entry in list(active):
                    clip = entry["clip"]
                    if clip.frame_start <= frame_index < clip.frame_end:
                        crop_frame = crops[id(clip.zone_plan)]
                        entry["frames"].append(crop_frame.copy())
                        if "text_masks" in entry:
                            if clip.mask_start <= frame_index < clip.mask_end:
                                cx, cy, _, _ = clip.zone_plan.crop
                                x, y, w, h = clip.zone_plan.rect
                                rx, ry = x - cx, y - cy
                                rect_crop = crop_frame[ry : ry + h, rx : rx + w]
                                mh, mw = max(1, h // 2), max(1, w // 2)
                                if (mh, mw) != (h, w):
                                    rect_crop = cv2.resize(
                                        rect_crop, (mw, mh),
                                        interpolation=cv2.INTER_AREA,
                                    )
                                entry["text_masks"].append(
                                    cls._text_mask(rect_crop)
                                )
                            else:
                                entry["text_masks"].append(None)
                    if frame_index >= clip.frame_end - 1:
                        active.remove(entry)
                        _submit(entry)
                frame_index += 1
            # Clips whose declared window ran past the actual stream end.
            for entry in active:
                if entry["frames"]:
                    _submit(entry)
            if inflight is not None:
                inflight.result()
        finally:
            for reader in readers.values():
                with suppress(Exception):
                    reader[0].kill()
            executor.shutdown(wait=True)
            for future in save_futures:
                future.result()
            save_pool.shutdown(wait=True)

    @staticmethod
    def _spawn_crop_reader(
        video_path: Path, crop: tuple[int, int, int, int]
    ) -> subprocess.Popen:
        """ffmpeg process piping one zone's raw BGR crop stream."""
        cx, cy, cw, ch = crop
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(video_path),
            "-vf", f"crop={cw}:{ch}:{cx}:{cy}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]
        cmd = rewrite_media_command(cmd)
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=get_media_subprocess_env(cmd),
            bufsize=10**7,
        )

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

        # Model load (GPU/disk) overlaps the CPU-bound detection pass.
        from .propainter_adapter import ProPainterEngine as _Engine

        engine_loader = threading.Thread(
            target=lambda: _Engine.load(), name="cleanup-engine-load", daemon=True
        )
        engine_loader.start()

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

        # Phases 2+3 overlapped: assembly trails the inpaint pass, consuming
        # clip caches in the same time order they are produced (it blocks on
        # each cache file, which is written atomically).
        from .propainter_adapter import ProPainterEngine

        engine_loader.join()
        if clips:
            ProPainterEngine.load(  # no-op when the preloader succeeded
                progress_cb=lambda m: set_progress(0.15, m)
            )
        clean_path = cls.get_clean_video_path(project_id)
        assembly_errors: list[BaseException] = []

        def _assembly_worker() -> None:
            try:
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
                    None,
                    wait_for_cache=True,
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                assembly_errors.append(exc)
                cancel_event.set()

        assembly_thread = threading.Thread(
            target=_assembly_worker, name="cleanup-assembly", daemon=True
        )
        assembly_thread.start()
        try:
            try:
                cls._run_inpaint_pass(
                    video_path, clips, spans_dir, cancel_event, set_progress
                )
            except CleanupCancelled:
                # A failing assembly cancels the inpaint pass; surface the
                # real error, not the induced cancellation.
                if not assembly_errors:
                    raise
        finally:
            assembly_thread.join()
            ProPainterEngine.unload()
        if assembly_errors:
            raise assembly_errors[0]
        if cancel_event.is_set():
            raise CleanupCancelled()

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
        wait_for_cache: bool = False,
    ) -> None:
        """Composite cached clip results and encode (NVENC, CPU fallback).

        With ``wait_for_cache`` the pass trails a concurrently running
        inpaint pass, blocking until each clip's (atomically renamed) cache
        file appears — clips complete in the same time order assembly
        consumes them.
        """
        tmp_path = output_path.with_name(output_path.name + ".tmp.mp4")
        try:
            cls._stream_composite(
                source_path, tmp_path, clips, spans_dir, fps, width, height,
                total_frames, cancel_event, progress_cb, try_nvenc=True,
                wait_for_cache=wait_for_cache,
            )
        except CleanupCancelled:
            raise
        except RuntimeError as exc:
            logger.warning("NVENC assembly failed (%s); retrying on CPU", exc)
            tmp_path.unlink(missing_ok=True)
            cls._stream_composite(
                source_path, tmp_path, clips, spans_dir, fps, width, height,
                total_frames, cancel_event, progress_cb, try_nvenc=False,
                wait_for_cache=wait_for_cache,
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
        wait_for_cache: bool = False,
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
                    if wait_for_cache:
                        while not cache_path.exists():
                            if cancel_event is not None and cancel_event.is_set():
                                process.kill()
                                raise CleanupCancelled()
                            time.sleep(0.2)
                    elif not cache_path.exists():
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
                    # NV12 halves the pipe traffic vs BGR24 (the pipe was the
                    # assembly bottleneck: ~2.7MB/frame -> ~1.4MB/frame).
                    process.stdin.write(
                        cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes()
                    )
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
            # Raw composited frames on stdin (I420/yuv420p: half the pipe
            # bandwidth of bgr24; callers convert with COLOR_BGR2YUV_I420).
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
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
                    process.stdin.write(
                        cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes()
                    )
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
