#!/usr/bin/env python3
"""Generate auto-editor preview renders and comparison files for one project."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import settings
from backend.app.services.auto_editor_profiles import (  # noqa: E402
    PREVIEW_AUTO_EDITOR_PROFILES,
    AutoEditorProfile,
)
from backend.app.utils.subprocess_runner import run_command  # noqa: E402

AUTO_EDITOR_TIMEOUT_SECONDS = 1800.0
FFPROBE_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class PreviewStats:
    """Structured stats extracted from auto-editor preview output."""

    input_duration_seconds: float
    output_duration_seconds: float
    delta_seconds: float
    clip_count: int
    clip_smallest_seconds: float
    clip_largest_seconds: float
    clip_median_seconds: float
    clip_average_seconds: float
    cut_count: int
    cut_smallest_seconds: float
    cut_largest_seconds: float
    cut_median_seconds: float
    cut_average_seconds: float


def parse_clock(value: str) -> float:
    """Convert auto-editor clock values like `0:01:38.17` to seconds."""
    sign = -1.0 if value.startswith("-") else 1.0
    cleaned = value[1:] if sign < 0 else value
    hours_text, minutes_text, seconds_text = cleaned.split(":")
    total = int(hours_text) * 3600 + int(minutes_text) * 60 + float(seconds_text)
    return sign * total


def parse_preview_report(output: str) -> PreviewStats:
    """Parse `auto-editor --preview` output into typed stats."""
    sections: dict[str, dict[str, str]] = {"length": {}, "clips": {}, "cuts": {}}
    current_section: str | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.endswith(":") and line[:-1] in sections:
            current_section = line[:-1]
            continue
        if current_section is None or not line.startswith("- "):
            continue
        key, _, rest = line[2:].partition(":")
        sections[current_section][key.strip()] = rest.strip()

    missing = [
        (section, key)
        for section, keys in (
            ("length", ("input", "output", "diff")),
            ("clips", ("amount", "smallest", "largest", "median", "average")),
            ("cuts", ("amount", "smallest", "largest", "median", "average")),
        )
        for key in keys
        if key not in sections[section]
    ]
    if missing:
        formatted = ", ".join(f"{section}.{key}" for section, key in missing)
        raise RuntimeError(f"Failed to parse auto-editor preview output: missing {formatted}")

    return PreviewStats(
        input_duration_seconds=parse_clock(sections["length"]["input"].split()[0]),
        output_duration_seconds=parse_clock(sections["length"]["output"].split()[0]),
        delta_seconds=parse_clock(sections["length"]["diff"].split()[0]),
        clip_count=int(sections["clips"]["amount"].split()[0]),
        clip_smallest_seconds=parse_clock(sections["clips"]["smallest"].split()[0]),
        clip_largest_seconds=parse_clock(sections["clips"]["largest"].split()[0]),
        clip_median_seconds=parse_clock(sections["clips"]["median"].split()[0]),
        clip_average_seconds=parse_clock(sections["clips"]["average"].split()[0]),
        cut_count=int(sections["cuts"]["amount"].split()[0]),
        cut_smallest_seconds=parse_clock(sections["cuts"]["smallest"].split()[0]),
        cut_largest_seconds=parse_clock(sections["cuts"]["largest"].split()[0]),
        cut_median_seconds=parse_clock(sections["cuts"]["median"].split()[0]),
        cut_average_seconds=parse_clock(sections["cuts"]["average"].split()[0]),
    )


async def probe_duration_seconds(path: Path) -> float:
    """Return container duration via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = await run_command(cmd, cwd=PROJECT_ROOT, timeout_seconds=FFPROBE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.decode()}")
    return float(result.stdout.decode().strip())


async def run_auto_editor_preview(profile: AutoEditorProfile, audio_path: Path) -> PreviewStats:
    """Run auto-editor in preview mode and return parsed metrics."""
    cmd = [
        "pixi",
        "run",
        "--locked",
        "--",
        "auto-editor",
        str(audio_path),
        *profile.command_args(),
        "--preview",
        "-q",
    ]
    result = await run_command(cmd, cwd=PROJECT_ROOT, timeout_seconds=AUTO_EDITOR_TIMEOUT_SECONDS)
    combined_output = (result.stdout + result.stderr).decode()
    if result.returncode != 0:
        raise RuntimeError(
            f"auto-editor preview failed for {profile.id}:\n{combined_output}"
        )
    return parse_preview_report(combined_output)


async def render_preview_audio(
    profile: AutoEditorProfile,
    audio_path: Path,
    output_path: Path,
) -> None:
    """Render one preview WAV with the specified profile."""
    cmd = [
        "pixi",
        "run",
        "--locked",
        "--",
        "auto-editor",
        str(audio_path),
        *profile.command_args(),
        "-o",
        str(output_path),
    ]
    result = await run_command(cmd, cwd=PROJECT_ROOT, timeout_seconds=AUTO_EDITOR_TIMEOUT_SECONDS)
    if result.returncode != 0:
        combined_output = (result.stdout + result.stderr).decode()
        raise RuntimeError(f"auto-editor render failed for {profile.id}:\n{combined_output}")


def build_markdown(project_id: str, rows: list[dict[str, object]]) -> str:
    """Render a compact markdown summary for human review."""
    margins = sorted({str(row["margin"]) for row in rows})
    silent_speeds = sorted({str(row["silent_speed"]) for row in rows})
    streams = sorted({str(row["stream"]) for row in rows})
    lines = [
        f"# auto-editor previews for {project_id}",
        "",
        "| profile | threshold | output (s) | delta (s) | clips | cuts | cut median (s) | file |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {id} | {threshold} | {output_duration_seconds:.2f} | {delta_seconds:.2f} | "
            "{clip_count} | {cut_count} | {cut_median_seconds:.2f} | {output_filename} |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "Rendered from the native `new_tts.wav` source with shared preview profiles.",
            f"Margins: {', '.join(margins)}",
            f"Silent speeds: {', '.join(silent_speeds)}",
            f"Streams: {', '.join(streams)}",
        ]
    )
    return "\n".join(lines) + "\n"


async def generate_previews(project_id: str) -> Path:
    """Generate preview WAVs and comparison artifacts for one project."""
    project_dir = settings.projects_dir / project_id
    audio_path = project_dir / "new_tts.wav"
    if not audio_path.exists():
        raise FileNotFoundError(f"Missing input audio: {audio_path}")

    output_dir = project_dir / "output" / "auto_editor_previews"
    output_dir.mkdir(parents=True, exist_ok=True)

    input_duration_seconds = await probe_duration_seconds(audio_path)
    comparison_rows: list[dict[str, object]] = []

    for profile in PREVIEW_AUTO_EDITOR_PROFILES:
        stats = await run_auto_editor_preview(profile, audio_path)
        output_path = profile.preview_path(output_dir)
        await render_preview_audio(profile, audio_path, output_path)
        rendered_duration_seconds = await probe_duration_seconds(output_path)

        row = {
            "id": profile.id,
            "threshold": profile.threshold,
            "margin": profile.margin,
            "silent_speed": profile.silent_speed,
            "stream": profile.stream,
            "input_duration_seconds": input_duration_seconds,
            "output_duration_seconds": rendered_duration_seconds,
            "delta_seconds": round(rendered_duration_seconds - input_duration_seconds, 6),
            "clip_count": stats.clip_count,
            "clip_smallest_seconds": stats.clip_smallest_seconds,
            "clip_largest_seconds": stats.clip_largest_seconds,
            "clip_median_seconds": stats.clip_median_seconds,
            "clip_average_seconds": stats.clip_average_seconds,
            "cut_count": stats.cut_count,
            "cut_smallest_seconds": stats.cut_smallest_seconds,
            "cut_largest_seconds": stats.cut_largest_seconds,
            "cut_median_seconds": stats.cut_median_seconds,
            "cut_average_seconds": stats.cut_average_seconds,
            "output_filename": output_path.name,
        }
        comparison_rows.append(row)

    comparison_json_path = output_dir / "comparison.json"
    comparison_md_path = output_dir / "comparison.md"

    payload = {
        "project_id": project_id,
        "source_audio": audio_path.name,
        "source_audio_duration_seconds": input_duration_seconds,
        "profiles": comparison_rows,
    }
    comparison_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    comparison_md_path.write_text(
        build_markdown(project_id, comparison_rows),
        encoding="utf-8",
    )
    return output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate auto-editor preview WAVs and comparison files for one project.",
    )
    parser.add_argument("project_id", help="Project identifier under backend/data/projects/")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    output_dir = await generate_previews(args.project_id)
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
