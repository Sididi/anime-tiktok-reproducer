"""Lossless BT.709 color tagging for final H.264 exports.

Premiere's hardware H.264 exports carry no VUI color metadata, so platforms
may guess the transfer/matrix and shift colors. This rewrites the SPS VUI
(bt709 primaries/transfer/matrix, tv range) via the h264_metadata bitstream
filter — a stream-copy remux, decoded frames stay byte-identical.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from pathlib import Path

from .media_binaries import get_media_subprocess_env, rewrite_media_command

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 30.0
_REMUX_TIMEOUT_SECONDS = 300.0

_BT709_BSF = (
    "h264_metadata="
    "colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1:"
    "video_full_range_flag=0"
)


def _probe_video_stream(path: Path) -> dict | None:
    cmd = rewrite_media_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,color_space,color_transfer,color_primaries",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        probe = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=get_media_subprocess_env(cmd),
        )
        streams = json.loads(probe.stdout or "{}").get("streams") or []
        return streams[0] if streams else None
    except Exception as exc:
        logger.warning("Color-tag probe failed for %s: %s", path, exc)
        return None


def ensure_bt709_tags(path: Path) -> bool:
    """Tag an untagged H.264 MP4 as BT.709 in place, losslessly.

    Returns True when the file was retagged. Skips (returns False) when the
    file is not H.264, is already fully tagged, or when anything fails — the
    original file is always left intact on failure. The path may carry a
    temporary suffix (.part, .lan_tmp); the codec probe is the actual guard.
    """
    stream = _probe_video_stream(path)
    if not stream or stream.get("codec_name") != "h264":
        return False
    if all(
        stream.get(key) == "bt709"
        for key in ("color_space", "color_transfer", "color_primaries")
    ):
        return False

    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.colortag.mp4")
    cmd = rewrite_media_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0",
            "-c",
            "copy",
            "-bsf:v",
            _BT709_BSF,
            "-movflags",
            "+faststart",
            "-y",
            str(tmp_path),
        ]
    )
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=_REMUX_TIMEOUT_SECONDS,
            env=get_media_subprocess_env(cmd),
        )
        retagged = _probe_video_stream(tmp_path)
        if not retagged or retagged.get("color_space") != "bt709":
            raise RuntimeError("remux output is missing bt709 tags")
        tmp_path.replace(path)
        logger.info("Tagged BT.709 color metadata on %s", path)
        return True
    except Exception as exc:
        logger.warning("Color tagging failed for %s (kept original): %s", path, exc)
        return False
    finally:
        tmp_path.unlink(missing_ok=True)
