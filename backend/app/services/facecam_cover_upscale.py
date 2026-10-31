"""Anime-tuned 4x upscaling (Real-ESRGAN x4plus_anime_6B) so small catalog covers fill the end card."""
from __future__ import annotations

import hashlib
import logging
import threading

import httpx
import numpy as np
from PIL import Image

from ..config import settings


logger = logging.getLogger(__name__)

WEIGHTS_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"
WEIGHTS_SHA256 = "f872d837d3c90ed2e05227bed711af5671a6fd1c9f7d7e91c911a61f155e99da"
_lock = threading.Lock()


def weights_path():
    return settings.data_dir / "models" / "realesrgan" / "RealESRGAN_x4plus_anime_6B.pth"


def _weights():
    path = weights_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            response = client.get(WEIGHTS_URL)
            response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != WEIGHTS_SHA256:
            raise ValueError("Real-ESRGAN weights checksum mismatch.")
        tmp = path.with_suffix(".part")
        tmp.write_bytes(response.content)
        tmp.replace(path)
    return path


def _network():
    import torch
    from torch import nn
    from torch.nn import functional as F

    class RDB(nn.Module):
        def __init__(self, nf=64, gc=32):
            super().__init__()
            self.conv1, self.conv2 = nn.Conv2d(nf, gc, 3, 1, 1), nn.Conv2d(nf + gc, gc, 3, 1, 1)
            self.conv3, self.conv4 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1), nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
            self.conv5, self.lrelu = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1), nn.LeakyReLU(0.2, True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            return self.conv5(torch.cat((x, x1, x2, x3, x4), 1)) * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, nf=64):
            super().__init__()
            self.rdb1, self.rdb2, self.rdb3 = RDB(nf), RDB(nf), RDB(nf)

        def forward(self, x):
            return self.rdb3(self.rdb2(self.rdb1(x))) * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self, blocks=6, nf=64):
            super().__init__()
            self.conv_first = nn.Conv2d(3, nf, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB(nf) for _ in range(blocks)])
            self.conv_body, self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1), nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up2, self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1), nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_last, self.lrelu = nn.Conv2d(nf, 3, 3, 1, 1), nn.LeakyReLU(0.2, True)

        def forward(self, x):
            feat = self.conv_first(x)
            feat = feat + self.conv_body(self.body(feat))
            feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
            feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(feat)))

    net = RRDBNet()
    net.load_state_dict(torch.load(_weights(), map_location="cpu", weights_only=True)["params_ema"])
    return net.eval()


def upscale(image: Image.Image, tile: int = 128, pad: int = 12) -> Image.Image:
    """4x on CPU (~10 s for a 460×690 cover): no VRAM taken from matching/encoding jobs.

    Padded tiles keep the 4x-resolution feature maps small (whole-image inference peaks at ~4.6 GB RAM).
    """
    import torch
    source = np.asarray(image.convert("RGB"))
    height, width = source.shape[:2]
    out = np.zeros((height * 4, width * 4, 3), dtype=np.float32)
    with _lock, torch.inference_mode():
        net = _network()
        x = torch.from_numpy(source).permute(2, 0, 1).float().div(255).unsqueeze(0)
        for top in range(0, height, tile):
            for left in range(0, width, tile):
                bottom, right = min(top + tile, height), min(left + tile, width)
                y0, x0 = max(top - pad, 0), max(left - pad, 0)
                y = net(x[:, :, y0:min(bottom + pad, height), x0:min(right + pad, width)]).clamp(0, 1)
                y = y[0, :, (top - y0) * 4:(bottom - y0) * 4, (left - x0) * 4:(right - x0) * 4]
                out[top * 4:bottom * 4, left * 4:right * 4] = y.permute(1, 2, 0).numpy()
    return Image.fromarray((out * 255).round().astype("uint8"))
