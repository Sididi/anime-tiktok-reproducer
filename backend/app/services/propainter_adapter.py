"""In-process adapter around the vendored ProPainter (modules/propainter).

Faithful re-implementation of the reference inference loop
(``inference_propainter.py`` at the pinned commit, see VENDORED.md) as a
library call over in-memory frames + masks, so the cleanup service can feed
cropped subtitle/watermark regions without touching the filesystem or the
reference script's matplotlib-importing helpers.

Weights are downloaded on first use from the official v0.1.0 release into
``backend/data/models/propainter/``.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import numpy as np

from ..config import settings

logger = logging.getLogger("uvicorn.error")

PROPAINTER_DIR = Path(__file__).resolve().parents[3] / "modules" / "propainter"
WEIGHTS_DIR = settings.data_dir / "models" / "propainter"

_WEIGHT_BASE_URL = "https://github.com/sczhou/ProPainter/releases/download/v0.1.0/"
# name -> minimum plausible size in bytes (guards truncated downloads; the
# release has no published checksums).
_WEIGHTS: dict[str, int] = {
    "ProPainter.pth": 100_000_000,
    "recurrent_flow_completion.pth": 15_000_000,
    "raft-things.pth": 15_000_000,
}

_load_lock = threading.Lock()


def ensure_weights(progress_cb=None) -> dict[str, Path]:
    """Download missing weight files; returns name -> local path."""
    import requests

    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, Path] = {}
    for name, min_size in _WEIGHTS.items():
        target = WEIGHTS_DIR / name
        if target.exists() and target.stat().st_size >= min_size:
            resolved[name] = target
            continue
        url = _WEIGHT_BASE_URL + name
        tmp = target.with_suffix(target.suffix + ".part")
        logger.info("Downloading ProPainter weight %s from %s", name, url)
        if progress_cb:
            progress_cb(f"Downloading model weight {name}…")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(tmp, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        handle.write(chunk)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Failed to download ProPainter weight {name!r} from {url}: {exc}. "
                f"You can download it manually into {WEIGHTS_DIR}."
            ) from exc
        if tmp.stat().st_size < min_size:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded ProPainter weight {name!r} is suspiciously small "
                f"({tmp.stat().st_size if tmp.exists() else 0} bytes); aborting."
            )
        tmp.replace(target)
        resolved[name] = target
    return resolved


def _import_propainter_modules():
    """Import vendored model modules (NOT core.utils — it pulls matplotlib)."""
    root = str(PROPAINTER_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    from model.modules.flow_comp_raft import RAFT_bi  # noqa: PLC0415
    from model.propainter import InpaintGenerator  # noqa: PLC0415
    from model.recurrent_flow_completion import (  # noqa: PLC0415
        RecurrentFlowCompleteNet,
    )

    return RAFT_bi, RecurrentFlowCompleteNet, InpaintGenerator


def _get_ref_index(mid_neighbor_id, neighbor_ids, length, ref_stride, ref_num):
    # Verbatim from the reference inference script.
    ref_index = []
    if ref_num == -1:
        for i in range(0, length, ref_stride):
            if i not in neighbor_ids:
                ref_index.append(i)
    else:
        start_idx = max(0, mid_neighbor_id - ref_stride * (ref_num // 2))
        end_idx = min(length, mid_neighbor_id + ref_stride * (ref_num // 2))
        for i in range(start_idx, end_idx, ref_stride):
            if i not in neighbor_ids:
                if len(ref_index) > ref_num:
                    break
                ref_index.append(i)
    return ref_index


class ProPainterEngine:
    """Process-global ProPainter model bundle (RAFT + flow completion +
    inpaint generator), loaded lazily and reusable across spans/jobs."""

    _raft = None
    _flow_complete = None
    _generator = None
    _device = None
    _fp16 = True

    @classmethod
    def load(cls, *, fp16: bool = True, progress_cb=None) -> None:
        with _load_lock:
            if cls._generator is not None and cls._fp16 == fp16:
                return
            cls.unload()

            import torch

            weights = ensure_weights(progress_cb=progress_cb)
            RAFT_bi, RecurrentFlowCompleteNet, InpaintGenerator = (
                _import_propainter_modules()
            )

            device = torch.device(
                "cuda" if torch.cuda.is_available() else "cpu"
            )
            use_half = fp16 and device.type == "cuda"
            if device.type == "cuda":
                # Crop shapes are fixed per zone: let cuDNN pick tuned kernels.
                torch.backends.cudnn.benchmark = True

            if progress_cb:
                progress_cb("Loading inpainting models…")
            raft = RAFT_bi(str(weights["raft-things.pth"]), device)
            if use_half:
                # RAFT has first-class autocast support (args.mixed_precision)
                # that the vendored initializer hardcodes off. ~1.6x on the
                # flow stage.
                try:
                    # initialize_RAFT unwraps DataParallel: fix_raft is RAFT.
                    raft.fix_raft.args.mixed_precision = True
                except AttributeError:
                    logger.warning("Could not enable RAFT mixed precision")
            flow_complete = RecurrentFlowCompleteNet(
                str(weights["recurrent_flow_completion.pth"])
            )
            for p in flow_complete.parameters():
                p.requires_grad = False
            flow_complete.to(device)
            flow_complete.eval()
            generator = InpaintGenerator(
                model_path=str(weights["ProPainter.pth"])
            ).to(device)
            generator.eval()

            if use_half:
                flow_complete = flow_complete.half()
                generator = generator.half()

            cls._raft = raft
            cls._flow_complete = flow_complete
            cls._generator = generator
            cls._device = device
            cls._fp16 = use_half

    @classmethod
    def unload(cls) -> None:
        if cls._generator is None and cls._raft is None:
            return
        cls._raft = None
        cls._flow_complete = None
        cls._generator = None
        cls._device = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # pragma: no cover - torch always importable here
            pass

    @classmethod
    def inpaint_clip(
        cls,
        frames_rgb: np.ndarray,
        masks: np.ndarray,
        *,
        neighbor_length: int = 10,
        ref_stride: int = 20,
        subvideo_length: int = 80,
        raft_iter: int = 12,
        flow_downscale: int = 2,
        flow_mask_dilates: int = 8,
        mask_dilates: int = 5,
    ) -> np.ndarray:
        """Inpaint the masked region of a clip.

        Args:
            frames_rgb: uint8 array [T, H, W, 3], H and W multiples of 8.
            masks: uint8 array [T, H, W]; nonzero = pixels to inpaint. Frames
                with an all-zero mask act as clean temporal references.
        Returns:
            uint8 array [T, H, W, 3] with the masked regions reconstructed.
        """
        if cls._generator is None:
            cls.load()

        import cv2
        import torch

        device = cls._device
        use_half = cls._fp16
        video_length, height, width = frames_rgb.shape[:3]
        if height % 8 or width % 8:
            raise ValueError(
                f"ProPainter input must be /8-aligned, got {width}x{height}"
            )

        # Mask preparation mirrors the reference read_mask(): a wider dilation
        # for the flow branch, a narrower one for the paint mask. cv2.dilate
        # with an equivalent kernel replaces scipy's per-iteration dilation
        # (~100x faster on 240-frame clips; iterations of a 3x3 cross ≈ one
        # (2N+1) ellipse kernel for these thin masks).
        flow_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * flow_mask_dilates + 1, 2 * flow_mask_dilates + 1),
        )
        paint_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * mask_dilates + 1, 2 * mask_dilates + 1),
        )
        flow_mask_list = []
        dilated_mask_list = []
        for i in range(video_length):
            raw = (masks[i] > 0).astype(np.uint8)
            if raw.any():
                flow_m = (
                    cv2.dilate(raw, flow_kernel) if flow_mask_dilates > 0 else raw
                ) > 0
                paint_m = (
                    cv2.dilate(raw, paint_kernel) if mask_dilates > 0 else raw
                ) > 0
            else:
                flow_m = raw > 0
                paint_m = raw > 0
            flow_mask_list.append(flow_m)
            dilated_mask_list.append(paint_m)

        frames = (
            torch.from_numpy(
                np.ascontiguousarray(frames_rgb, dtype=np.float32)
            )
            .permute(0, 3, 1, 2)
            .unsqueeze(0)
            / 255.0
        ) * 2 - 1
        flow_masks = (
            torch.from_numpy(
                np.stack(flow_mask_list).astype(np.float32)
            )
            .unsqueeze(1)
            .unsqueeze(0)
        )
        masks_dilated = (
            torch.from_numpy(
                np.stack(dilated_mask_list).astype(np.float32)
            )
            .unsqueeze(1)
            .unsqueeze(0)
        )
        frames = frames.to(device)
        flow_masks = flow_masks.to(device)
        masks_dilated = masks_dilated.to(device)
        h, w = height, width

        ori_frames = [frames_rgb[i] for i in range(video_length)]
        comp_frames: list[np.ndarray | None] = [None] * video_length

        with torch.no_grad():
            # ---- compute flow (RAFT stays fp32, chunked by width) ----
            # Flow guides propagation; it does not need full resolution.
            # Running RAFT at 1/flow_downscale and upsampling the flow field
            # (values scaled accordingly) measured ~3.5x faster on the flow
            # stage with no visible repair difference (2026-08-13 bench).
            import torch.nn.functional as F

            # Per-axis: RAFT's 4-level correlation pyramid (input already /8)
            # needs >= ~64px per side, so an axis is only halved when it can
            # stay above that floor.
            def _flow_dim(dim: int) -> int:
                # RAFT (this checkpoint) returns NaN flows when either input
                # dimension is < 128 (verified empirically: 128 ok, 96/88
                # NaN) — its level-4 correlation pyramid degenerates. Only
                # downscale an axis that can stay at or above that floor.
                if flow_downscale <= 1 or dim < 256:
                    return dim
                return max(128, (dim // flow_downscale) // 8 * 8)

            flow_h = _flow_dim(h)
            flow_w = _flow_dim(w)
            if (flow_h, flow_w) != (h, w):
                flow_frames = F.interpolate(
                    frames.view(-1, 3, h, w),
                    size=(flow_h, flow_w),
                    mode="bilinear",
                    align_corners=False,
                ).view(1, video_length, 3, flow_h, flow_w)
            else:
                flow_h, flow_w = h, w
                flow_frames = frames

            def _upsample_flows(flows: "torch.Tensor") -> "torch.Tensor":
                if (flow_h, flow_w) == (h, w):
                    return flows
                b, t, c, fh, fw = flows.shape
                scaled = F.interpolate(
                    flows.view(-1, c, fh, fw),
                    size=(h, w),
                    mode="bilinear",
                    align_corners=False,
                )
                scaled[:, 0] *= w / fw
                scaled[:, 1] *= h / fh
                return scaled.view(b, t, c, h, w)

            if flow_w <= 640:
                short_clip_len = 12
            elif flow_w <= 720:
                short_clip_len = 8
            elif flow_w <= 1280:
                short_clip_len = 4
            else:
                short_clip_len = 2

            def _raft_chunk(chunk_small, chunk_full):
                """RAFT on the (possibly downscaled) chunk, with a full-res
                fp32 retry if it ever returns NaN — loud, never silent."""
                flows_f, flows_b = cls._raft(chunk_small, iters=raft_iter)
                if torch.isnan(flows_f).any() or torch.isnan(flows_b).any():
                    logger.warning(
                        "RAFT returned NaN flows at %sx%s; retrying at full "
                        "resolution",
                        chunk_small.shape[-2], chunk_small.shape[-1],
                    )
                    flows_f, flows_b = cls._raft(chunk_full, iters=raft_iter)
                    return flows_f, flows_b, False
                return flows_f, flows_b, True

            if video_length > short_clip_len:
                gt_flows_f_list, gt_flows_b_list = [], []
                for f in range(0, video_length, short_clip_len):
                    end_f = min(video_length, f + short_clip_len)
                    s = f if f == 0 else f - 1
                    flows_f, flows_b, downscaled = _raft_chunk(
                        flow_frames[:, s:end_f], frames[:, s:end_f]
                    )
                    if downscaled:
                        flows_f = _upsample_flows(flows_f)
                        flows_b = _upsample_flows(flows_b)
                    gt_flows_f_list.append(flows_f)
                    gt_flows_b_list.append(flows_b)
                    torch.cuda.empty_cache()
                gt_flows_bi = (
                    torch.cat(gt_flows_f_list, dim=1),
                    torch.cat(gt_flows_b_list, dim=1),
                )
            else:
                flows_f, flows_b, downscaled = _raft_chunk(flow_frames, frames)
                if downscaled:
                    flows_f = _upsample_flows(flows_f)
                    flows_b = _upsample_flows(flows_b)
                gt_flows_bi = (flows_f, flows_b)
                torch.cuda.empty_cache()

            if use_half:
                frames = frames.half()
                flow_masks = flow_masks.half()
                masks_dilated = masks_dilated.half()
                gt_flows_bi = (gt_flows_bi[0].half(), gt_flows_bi[1].half())

            # ---- complete flow ----
            flow_length = gt_flows_bi[0].size(1)
            if flow_length > subvideo_length:
                pred_flows_f, pred_flows_b = [], []
                pad_len = 5
                for f in range(0, flow_length, subvideo_length):
                    s_f = max(0, f - pad_len)
                    e_f = min(flow_length, f + subvideo_length + pad_len)
                    pad_len_s = max(0, f) - s_f
                    pad_len_e = e_f - min(flow_length, f + subvideo_length)
                    pred_flows_bi_sub, _ = (
                        cls._flow_complete.forward_bidirect_flow(
                            (
                                gt_flows_bi[0][:, s_f:e_f],
                                gt_flows_bi[1][:, s_f:e_f],
                            ),
                            flow_masks[:, s_f : e_f + 1],
                        )
                    )
                    pred_flows_bi_sub = cls._flow_complete.combine_flow(
                        (
                            gt_flows_bi[0][:, s_f:e_f],
                            gt_flows_bi[1][:, s_f:e_f],
                        ),
                        pred_flows_bi_sub,
                        flow_masks[:, s_f : e_f + 1],
                    )
                    pred_flows_f.append(
                        pred_flows_bi_sub[0][:, pad_len_s : e_f - s_f - pad_len_e]
                    )
                    pred_flows_b.append(
                        pred_flows_bi_sub[1][:, pad_len_s : e_f - s_f - pad_len_e]
                    )
                    torch.cuda.empty_cache()
                pred_flows_bi = (
                    torch.cat(pred_flows_f, dim=1),
                    torch.cat(pred_flows_b, dim=1),
                )
            else:
                pred_flows_bi, _ = cls._flow_complete.forward_bidirect_flow(
                    gt_flows_bi, flow_masks
                )
                pred_flows_bi = cls._flow_complete.combine_flow(
                    gt_flows_bi, pred_flows_bi, flow_masks
                )
                torch.cuda.empty_cache()

            # ---- image propagation ----
            masked_frames = frames * (1 - masks_dilated)
            subvideo_length_img_prop = min(100, subvideo_length)
            if video_length > subvideo_length_img_prop:
                updated_frames_list, updated_masks_list = [], []
                pad_len = 10
                for f in range(0, video_length, subvideo_length_img_prop):
                    s_f = max(0, f - pad_len)
                    e_f = min(video_length, f + subvideo_length_img_prop + pad_len)
                    pad_len_s = max(0, f) - s_f
                    pad_len_e = e_f - min(
                        video_length, f + subvideo_length_img_prop
                    )

                    b, t, _, _, _ = masks_dilated[:, s_f:e_f].size()
                    pred_flows_bi_sub = (
                        pred_flows_bi[0][:, s_f : e_f - 1],
                        pred_flows_bi[1][:, s_f : e_f - 1],
                    )
                    prop_imgs_sub, updated_local_masks_sub = (
                        cls._generator.img_propagation(
                            masked_frames[:, s_f:e_f],
                            pred_flows_bi_sub,
                            masks_dilated[:, s_f:e_f],
                            "nearest",
                        )
                    )
                    updated_frames_sub = frames[:, s_f:e_f] * (
                        1 - masks_dilated[:, s_f:e_f]
                    ) + prop_imgs_sub.view(b, t, 3, h, w) * masks_dilated[
                        :, s_f:e_f
                    ]
                    updated_masks_sub = updated_local_masks_sub.view(
                        b, t, 1, h, w
                    )
                    updated_frames_list.append(
                        updated_frames_sub[:, pad_len_s : e_f - s_f - pad_len_e]
                    )
                    updated_masks_list.append(
                        updated_masks_sub[:, pad_len_s : e_f - s_f - pad_len_e]
                    )
                    torch.cuda.empty_cache()
                updated_frames = torch.cat(updated_frames_list, dim=1)
                updated_masks = torch.cat(updated_masks_list, dim=1)
            else:
                b, t, _, _, _ = masks_dilated.size()
                prop_imgs, updated_local_masks = cls._generator.img_propagation(
                    masked_frames, pred_flows_bi, masks_dilated, "nearest"
                )
                updated_frames = (
                    frames * (1 - masks_dilated)
                    + prop_imgs.view(b, t, 3, h, w) * masks_dilated
                )
                updated_masks = updated_local_masks.view(b, t, 1, h, w)
                torch.cuda.empty_cache()

            # ---- feature propagation + transformer ----
            # The reference uses centered windows stepping by half the window,
            # computing every frame twice and averaging. Forward windows of
            # the same size stepping by neighbor_length compute each frame
            # once (~2x faster) with a 1-frame blended overlap at each seam;
            # the flow-propagated features keep windows temporally
            # consistent.
            neighbor_stride = neighbor_length
            if video_length > subvideo_length:
                ref_num = subvideo_length // ref_stride
            else:
                ref_num = -1

            for f in range(0, video_length, neighbor_stride):
                neighbor_ids = [
                    i
                    for i in range(f, min(video_length, f + neighbor_length + 1))
                ]
                if len(neighbor_ids) < 2 and f > 0:
                    # Degenerate 1-frame tail window: the model needs at least
                    # one flow step, so fold the previous frame in.
                    neighbor_ids = [f - 1, f]
                ref_ids = _get_ref_index(
                    f, neighbor_ids, video_length, ref_stride, ref_num
                )
                selected_imgs = updated_frames[:, neighbor_ids + ref_ids]
                selected_masks = masks_dilated[:, neighbor_ids + ref_ids]
                selected_update_masks = updated_masks[:, neighbor_ids + ref_ids]
                selected_pred_flows_bi = (
                    pred_flows_bi[0][:, neighbor_ids[:-1]],
                    pred_flows_bi[1][:, neighbor_ids[:-1]],
                )

                l_t = len(neighbor_ids)
                pred_img = cls._generator(
                    selected_imgs,
                    selected_pred_flows_bi,
                    selected_masks,
                    selected_update_masks,
                    l_t,
                )
                pred_img = pred_img.view(-1, 3, h, w)
                pred_img = (pred_img + 1) / 2
                pred_img = pred_img.cpu().permute(0, 2, 3, 1).float().numpy() * 255
                binary_masks = (
                    masks_dilated[0, neighbor_ids]
                    .cpu()
                    .permute(0, 2, 3, 1)
                    .float()
                    .numpy()
                    .astype(np.uint8)
                )
                for i in range(len(neighbor_ids)):
                    idx = neighbor_ids[i]
                    img = np.array(pred_img[i]).astype(np.uint8) * binary_masks[
                        i
                    ] + ori_frames[idx] * (1 - binary_masks[i])
                    if comp_frames[idx] is None:
                        comp_frames[idx] = img
                    else:
                        comp_frames[idx] = (
                            comp_frames[idx].astype(np.float32) * 0.5
                            + img.astype(np.float32) * 0.5
                        )
                    comp_frames[idx] = comp_frames[idx].astype(np.uint8)

                torch.cuda.empty_cache()

        return np.stack([f for f in comp_frames])
