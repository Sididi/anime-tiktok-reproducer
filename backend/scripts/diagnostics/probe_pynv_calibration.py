#!/usr/bin/env python3
"""G0 calibration: PyNvVideoCodec NATIVE (P010/NV12 on GPU) -> RGB in torch,
compared against cv2's BGR output for the SAME frames.

Alignment: find the frame-index offset (in {-1,0,+1}) that minimizes luma
delta, then measure color deltas for BT.601 vs BT.709 limited-range matrices.
"""
import sys
import time

import cv2
import numpy as np
import torch
import PyNvVideoCodec as nvc

EPISODE = "modules/anime_searcher/library/anime/Boukensha ni Naritai to Miyako ni Deteitta Musume ga S Rank ni Natteta (My Daughter Left the Nest and Returned an S-Rank Adventurer)/[Judas] S-Rank Musume - S01E01.mp4"
TIKTOK = "backend/data/projects/411f73d26c1d/tiktok.mp4"

MATRICES = {
    # Kr, Kb
    "bt601": (0.299, 0.114),
    "bt709": (0.2126, 0.0722),
}


def yuv_to_rgb(y, u, v, matrix, bit_depth):
    """Limited-range YUV -> RGB float in [0,255], y/u/v float tensors (10- or 8-bit code values)."""
    kr, kb = MATRICES[matrix]
    kg = 1.0 - kr - kb
    scale = 4.0 if bit_depth == 10 else 1.0  # 8-bit-equivalent codes
    y8 = y / scale
    u8 = u / scale
    v8 = v / scale
    c = (y8 - 16.0) * (255.0 / 219.0)
    d = (u8 - 128.0) * (255.0 / 224.0)
    e = (v8 - 128.0) * (255.0 / 224.0)
    r = c + 2.0 * (1.0 - kr) * e
    g = c - (2.0 * kb * (1.0 - kb) / kg) * d - (2.0 * kr * (1.0 - kr) / kg) * e
    b = c + 2.0 * (1.0 - kb) * d
    return torch.stack([r, g, b], dim=-1)


def pynv_frame_to_rgb(t, width, height, matrix):
    """t: uint16 (h*1.5, w) P010 or uint8 NV12 tensor on cuda."""
    if t.dtype == torch.uint16:
        bit_depth = 10
        f = t.to(torch.float32) / 64.0  # P010 stores 10-bit codes in MSBs
    else:
        bit_depth = 8
        f = t.to(torch.float32)
    y = f[:height, :width]
    uv = f[height:height + height // 2, :width].reshape(height // 2, width // 2, 2)
    u = uv[..., 0].repeat_interleave(2, 0).repeat_interleave(2, 1)
    v = uv[..., 1].repeat_interleave(2, 0).repeat_interleave(2, 1)
    rgb = yuv_to_rgb(y, u, v, matrix, bit_depth)
    return rgb.clamp(0, 255)


def cv2_frames_at_indices(path, indices, fps):
    cap = cv2.VideoCapture(path)
    out = {}
    for i in sorted(indices):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        if ok:
            out[i] = fr  # BGR uint8
    cap.release()
    return out


def calibrate(path, label, indices):
    dec = nvc.SimpleDecoder(path, gpu_id=0, use_device_memory=True,
                            output_color_type=nvc.OutputColorType.NATIVE)
    md = dec.get_stream_metadata()
    w, h, fps = md.width, md.height, md.average_fps
    print(f"== {label}: {md.codec_name} {w}x{h} fps={fps:.3f}")

    cv_frames = cv2_frames_at_indices(path, [i + d for i in indices for d in (-1, 0, 1)], fps)

    # alignment via luma on the first index
    i0 = indices[0]
    t = torch.from_dlpack(dec[i0])
    best = None
    for d in (-1, 0, 1):
        if i0 + d not in cv_frames:
            continue
        rgb = pynv_frame_to_rgb(t, w, h, "bt709").cpu().numpy()
        cv_rgb = cv_frames[i0 + d][:, :, ::-1].astype(np.float32)
        delta = float(np.abs(rgb[..., 0] * 0.3 + rgb[..., 1] * 0.6 + rgb[..., 2] * 0.1
                             - (cv_rgb[..., 0] * 0.3 + cv_rgb[..., 1] * 0.6 + cv_rgb[..., 2] * 0.1)).mean())
        if best is None or delta < best[1]:
            best = (d, delta)
    offset = best[0]
    print(f"   alignment offset: pynv[i] == cv2[i{offset:+d}] (luma delta {best[1]:.2f})")

    for matrix in MATRICES:
        deltas, maxs = [], []
        for i in indices:
            if i + offset not in cv_frames:
                continue
            t = torch.from_dlpack(dec[i])
            rgb = pynv_frame_to_rgb(t, w, h, matrix).cpu().numpy()
            cv_rgb = cv_frames[i + offset][:, :, ::-1].astype(np.float32)
            diff = np.abs(rgb - cv_rgb)
            deltas.append(float(diff.mean()))
            maxs.append(float(diff.max()))
        print(f"   {matrix}: mean delta {np.mean(deltas):.3f} (per-frame {['%.3f' % d for d in deltas]}), max {max(maxs):.1f}")


if __name__ == "__main__":
    calibrate(EPISODE, "episode HEVC 10-bit", [1500, 8000, 16000, 24000, 30000])
    calibrate(TIKTOK, "tiktok", [100, 1500, 3000, 4500])
