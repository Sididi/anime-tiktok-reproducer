from __future__ import annotations

import shutil
from pathlib import Path

from pydub import AudioSegment as PydubSegment

from ..utils.subprocess_runner import run_command


class AudioSpeedService:
    """Apply speed changes to audio files via ffmpeg atempo."""

    SPEED_MIN = 0.9
    SPEED_MAX = 1.5

    @classmethod
    async def apply_speed(cls, input_path: Path, output_path: Path, speed: float) -> float:
        """Apply ffmpeg atempo filter. Returns new duration in seconds."""
        if speed == 1.0:
            if input_path != output_path:
                shutil.copy2(input_path, output_path)
            return cls._probe_duration(output_path)

        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-filter:a", f"atempo={speed}",
            "-vn", str(output_path),
        ]
        result = await run_command(cmd, timeout_seconds=120.0)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg atempo failed: {result.stderr.decode('utf-8', errors='replace')}")
        return cls._probe_duration(output_path)

    @classmethod
    def _probe_duration(cls, path: Path) -> float:
        audio = PydubSegment.from_file(str(path))
        return len(audio) / 1000.0
