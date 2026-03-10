#!/usr/bin/env python3
"""Generate audio-only auto-editor tuning variants for sharp TikTok-style cuts."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.utils.subprocess_runner import run_command  # noqa: E402

AUTO_EDITOR_TIMEOUT_SECONDS = 1800.0
FFPROBE_TIMEOUT_SECONDS = 30.0
DEFAULT_PRESET = "sharp_tiktok"
DEFAULT_THRESHOLDS = ("0.055", "0.065", "0.075", "0.085", "0.095")
DEFAULT_MARGINS = (
    "0.02sec,0.12sec",
    "0.03sec,0.14sec",
    "0.04sec,0.16sec",
    "0.04sec,0.20sec",
)
ADVANCED_COMBOS = (
    ("0.075", "0.03sec,0.14sec"),
    ("0.075", "0.02sec,0.12sec"),
    ("0.085", "0.03sec,0.14sec"),
    ("0.085", "0.02sec,0.12sec"),
)
ADVANCED_MINCUT_MINCLIP = ((4, 2), (2, 3))
TIME_BASE = 30
THRESHOLD_SCALE = Decimal("1000")
MARGIN_SCALE = Decimal("100")
LENGTH_RE = re.compile(
    r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>sec|secs|s|ms|frame|frames)$",
    re.IGNORECASE,
)
SLUG_RE = re.compile(r"[^a-z0-9]+")


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


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """Describe one auto-editor tuning variant."""

    threshold: str
    margin: str
    mincut: int | None = None
    minclip: int | None = None
    silent_speed: int = 99999
    stream: str = "all"
    time_base: int = TIME_BASE

    @property
    def variant_id(self) -> str:
        margin_start, margin_end = self.margin_tokens()
        parts = [
            f"t{threshold_token(self.threshold)}",
            f"m{margin_start}_{margin_end}",
        ]
        if self.mincut is not None:
            parts.append(f"mc{self.mincut}")
        if self.minclip is not None:
            parts.append(f"mp{self.minclip}")
        return "_".join(parts)

    @property
    def output_filename(self) -> str:
        return f"{self.variant_id}.wav"

    @property
    def requires_schema_extension(self) -> bool:
        return self.mincut is not None or self.minclip is not None

    def edit_value(self) -> str:
        values = [f"threshold={self.threshold}", f"stream={self.stream}"]
        if self.mincut is not None:
            values.append(f"mincut={self.mincut}")
        if self.minclip is not None:
            values.append(f"minclip={self.minclip}")
        return "audio:" + ",".join(values)

    def margin_tokens(self) -> tuple[str, str]:
        start_text, end_text = split_margin(self.margin)
        return length_token(start_text), length_token(end_text)

    def base_command(self, audio_path: Path) -> list[str]:
        return [
            "pixi",
            "run",
            "--locked",
            "--",
            "auto-editor",
            str(audio_path),
            "--edit",
            self.edit_value(),
            "--margin",
            self.margin,
            "--silent-speed",
            str(self.silent_speed),
            "--time-base",
            str(self.time_base),
            "--no-open",
        ]

    def preview_command(self, audio_path: Path) -> list[str]:
        return [*self.base_command(audio_path), "--preview", "-q"]

    def render_command(self, audio_path: Path, output_path: Path) -> list[str]:
        return [*self.base_command(audio_path), "-o", str(output_path), "-q"]

    def voice_override_snippet(self) -> str:
        return f'threshold: "{self.threshold}"\nmargin: "{self.margin}"'


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

    def required_value(section: str, key: str) -> str:
        value = sections[section].get(key)
        if value is None:
            raise RuntimeError(
                f"Failed to parse auto-editor preview output: missing {section}.{key}"
            )
        return value

    def amount(section: str) -> int:
        return int(required_value(section, "amount").split()[0])

    def timed_value(section: str, key: str) -> float:
        value = sections[section].get(key)
        if value is None:
            section_amount = amount(section)
            if section_amount == 0:
                return 0.0
            if section_amount == 1:
                fallback_keys = ("smallest", "largest", "median", "average")
                for fallback_key in fallback_keys:
                    fallback_value = sections[section].get(fallback_key)
                    if fallback_value is not None:
                        return parse_clock(fallback_value.split()[0])
            raise RuntimeError(
                f"Failed to parse auto-editor preview output: missing {section}.{key}"
            )
        return parse_clock(value.split()[0])

    return PreviewStats(
        input_duration_seconds=parse_clock(required_value("length", "input").split()[0]),
        output_duration_seconds=parse_clock(required_value("length", "output").split()[0]),
        delta_seconds=parse_clock(required_value("length", "diff").split()[0]),
        clip_count=amount("clips"),
        clip_smallest_seconds=timed_value("clips", "smallest"),
        clip_largest_seconds=timed_value("clips", "largest"),
        clip_median_seconds=timed_value("clips", "median"),
        clip_average_seconds=timed_value("clips", "average"),
        cut_count=amount("cuts"),
        cut_smallest_seconds=timed_value("cuts", "smallest"),
        cut_largest_seconds=timed_value("cuts", "largest"),
        cut_median_seconds=timed_value("cuts", "median"),
        cut_average_seconds=timed_value("cuts", "average"),
    )


def slugify_stem(value: str) -> str:
    """Build a stable slug for temp directory names."""
    slug = SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or "audio"


def normalize_threshold(raw_value: str) -> str:
    """Validate threshold values and normalize to three decimals."""
    try:
        number = Decimal(raw_value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid threshold '{raw_value}': expected decimal between 0 and 1"
        ) from exc
    if number <= 0 or number >= 1:
        raise argparse.ArgumentTypeError(
            f"Invalid threshold '{raw_value}': expected decimal between 0 and 1"
        )
    return f"{number.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP):.3f}"


def parse_length_component(raw_value: str) -> tuple[Decimal, str]:
    """Validate a single auto-editor length value."""
    candidate = raw_value.strip().lower()
    match = LENGTH_RE.match(candidate)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"Invalid length '{raw_value}': expected values like 0.04sec or 2frames"
        )
    value = Decimal(match.group("value"))
    unit = match.group("unit")
    if value < 0:
        raise argparse.ArgumentTypeError(f"Invalid length '{raw_value}': must be >= 0")
    return value, unit


def normalize_margin(raw_value: str) -> str:
    """Validate margin input and normalize spacing."""
    parts = [part.strip() for part in raw_value.split(",")]
    if len(parts) != 2 or any(not part for part in parts):
        raise argparse.ArgumentTypeError(
            f"Invalid margin '{raw_value}': expected PRE,POST like 0.04sec,0.20sec"
    )
    normalized_parts = []
    for part in parts:
        value, unit = parse_length_component(part)
        normalized_parts.append(f"{value.normalize()}{unit}")
    return ",".join(normalized_parts)


def split_margin(margin: str) -> tuple[str, str]:
    """Split a validated margin string into its two length components."""
    start_text, end_text = margin.split(",", maxsplit=1)
    return start_text, end_text


def length_to_seconds(length_text: str) -> Decimal:
    """Convert auto-editor length syntax to seconds for variant id tokens."""
    value, unit = parse_length_component(length_text)
    if unit in {"sec", "secs", "s"}:
        return value
    if unit == "ms":
        return value / Decimal("1000")
    return value / Decimal(TIME_BASE)


def length_token(length_text: str) -> str:
    """Encode a margin length as a sortable token."""
    centiseconds = (length_to_seconds(length_text) * MARGIN_SCALE).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return str(int(centiseconds)).zfill(3)


def threshold_token(threshold_text: str) -> str:
    """Encode a threshold as a sortable token."""
    thousandths = (Decimal(threshold_text) * THRESHOLD_SCALE).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return str(int(thousandths)).zfill(3)


def build_variants(
    thresholds: tuple[str, ...],
    margins: tuple[str, ...],
    *,
    include_sharp_presets: bool,
) -> list[VariantSpec]:
    """Build the sweep definition for one run."""
    variants = [
        VariantSpec(threshold=threshold, margin=margin)
        for threshold in thresholds
        for margin in margins
    ]
    if include_sharp_presets:
        for threshold, margin in ADVANCED_COMBOS:
            for mincut, minclip in ADVANCED_MINCUT_MINCLIP:
                variants.append(
                    VariantSpec(
                        threshold=threshold,
                        margin=margin,
                        mincut=mincut,
                        minclip=minclip,
                    )
                )

    variant_ids = [variant.variant_id for variant in variants]
    duplicates = sorted(
        {
            variant_id
            for variant_id in variant_ids
            if variant_ids.count(variant_id) > 1
        }
    )
    if duplicates:
        duplicate_text = ", ".join(duplicates)
        raise RuntimeError(f"Variant id collision detected: {duplicate_text}")
    return variants


def shell_join(cmd: list[str]) -> str:
    """Render a shell-safe command string for logs and reports."""
    return shlex.join(cmd)


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


def make_output_dir(audio_path: Path, requested_output_dir: Path | None) -> Path:
    """Resolve and create the output directory for one tuning run."""
    if requested_output_dir is not None:
        output_dir = requested_output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    slug = slugify_stem(audio_path.stem)
    temp_dir = tempfile.mkdtemp(prefix=f"atr-auto-editor-variants-{slug}-", dir="/tmp")
    return Path(temp_dir).resolve()


async def process_variant(
    variant: VariantSpec,
    *,
    audio_path: Path,
    input_duration_seconds: float,
    variants_dir: Path,
    logs_dir: Path,
) -> dict[str, object]:
    """Run preview + render for one variant and capture its report row."""
    output_path = variants_dir / variant.output_filename
    log_path = logs_dir / f"{variant.variant_id}.log"
    base_command = variant.base_command(audio_path)
    preview_command = variant.preview_command(audio_path)
    render_command = variant.render_command(audio_path, output_path)
    log_lines = [
        f"variant_id: {variant.variant_id}",
        f"output_file: {output_path.name}",
        f"base_command: {shell_join(base_command)}",
        "",
    ]

    row: dict[str, object] = {
        "variant_id": variant.variant_id,
        "status": "pending",
        "threshold": variant.threshold,
        "margin": variant.margin,
        "mincut": variant.mincut,
        "minclip": variant.minclip,
        "time_base": variant.time_base,
        "silent_speed": variant.silent_speed,
        "stream": variant.stream,
        "command": shell_join(base_command),
        "preview_command": shell_join(preview_command),
        "render_command": shell_join(render_command),
        "requires_schema_extension": variant.requires_schema_extension,
        "voice_override_snippet": variant.voice_override_snippet(),
        "input_duration_seconds": input_duration_seconds,
        "output_duration_seconds": None,
        "delta_seconds": None,
        "clip_count": None,
        "clip_smallest_seconds": None,
        "clip_largest_seconds": None,
        "clip_median_seconds": None,
        "clip_average_seconds": None,
        "cut_count": None,
        "cut_smallest_seconds": None,
        "cut_largest_seconds": None,
        "cut_median_seconds": None,
        "cut_average_seconds": None,
        "output_filename": output_path.name,
        "output_relative_path": f"variants/{output_path.name}",
        "log_filename": log_path.name,
        "log_relative_path": f"logs/{log_path.name}",
        "error": None,
        "rank": None,
    }

    try:
        preview_result = await run_command(
            preview_command,
            cwd=PROJECT_ROOT,
            timeout_seconds=AUTO_EDITOR_TIMEOUT_SECONDS,
        )
        preview_output = (preview_result.stdout + preview_result.stderr).decode(errors="replace")
        log_lines.extend(
            [
                f"preview_command: {shell_join(preview_command)}",
                preview_output.rstrip(),
                "",
            ]
        )
        if preview_result.returncode != 0:
            row["status"] = "preview_failed"
            row["error"] = preview_output.strip() or "auto-editor preview failed"
            return row

        stats = parse_preview_report(preview_output)
        row.update(
            {
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
            }
        )
    except Exception as exc:
        row["status"] = "preview_parse_failed"
        row["error"] = str(exc)
        log_lines.extend(["preview_error:", str(exc), ""])
        return row
    finally:
        log_path.write_text("\n".join(log_lines).rstrip() + "\n", encoding="utf-8")

    try:
        render_result = await run_command(
            render_command,
            cwd=PROJECT_ROOT,
            timeout_seconds=AUTO_EDITOR_TIMEOUT_SECONDS,
        )
        render_output = (render_result.stdout + render_result.stderr).decode(errors="replace")
        log_path.write_text(
            log_path.read_text(encoding="utf-8")
            + f"render_command: {shell_join(render_command)}\n"
            + render_output.rstrip()
            + "\n",
            encoding="utf-8",
        )
        if render_result.returncode != 0:
            row["status"] = "render_failed"
            row["error"] = render_output.strip() or "auto-editor render failed"
            return row
        if not output_path.exists():
            row["status"] = "render_failed"
            row["error"] = f"auto-editor finished without creating {output_path.name}"
            return row

        rendered_duration_seconds = await probe_duration_seconds(output_path)
        row["status"] = "success"
        row["output_duration_seconds"] = rendered_duration_seconds
        row["delta_seconds"] = round(rendered_duration_seconds - input_duration_seconds, 6)
        return row
    except Exception as exc:
        row["status"] = "probe_failed"
        row["error"] = str(exc)
        log_path.write_text(
            log_path.read_text(encoding="utf-8") + f"render_error:\n{exc}\n",
            encoding="utf-8",
        )
        return row


def rank_variants(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Rank successful variants from sharpest to safest."""
    successful_rows = [row for row in rows if row["status"] == "success"]
    ranked_rows = sorted(
        successful_rows,
        key=lambda row: (
            float(row["output_duration_seconds"]),
            -int(row["cut_count"]),
            float(row["cut_median_seconds"]),
        ),
    )
    for index, row in enumerate(ranked_rows, start=1):
        row["rank"] = index

    failed_rows = sorted(
        [row for row in rows if row["status"] != "success"],
        key=lambda row: str(row["variant_id"]),
    )
    return [*ranked_rows, *failed_rows]


def build_markdown(
    *,
    audio_path: Path,
    output_dir: Path,
    preset: str,
    input_duration_seconds: float,
    rows: list[dict[str, object]],
) -> str:
    """Render a compact markdown summary for human review."""
    success_count = sum(1 for row in rows if row["status"] == "success")
    lines = [
        "# auto-editor audio variants",
        "",
        f"Source audio: `{audio_path}`",
        f"Preset: `{preset}`",
        f"Output directory: `{output_dir}`",
        f"Input duration: {input_duration_seconds:.2f}s",
        f"Successful variants: {success_count}/{len(rows)}",
        "",
        "| rank | status | variant | threshold | margin | mincut | minclip | out (s) | delta (s) | cuts | cut median (s) | schema ext | file |",
        "| ---: | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]

    for row in rows:
        lines.append(
            "| {rank} | {status} | {variant_id} | {threshold} | {margin} | {mincut} | {minclip} | "
            "{output_duration_seconds} | {delta_seconds} | {cut_count} | {cut_median_seconds} | "
            "{requires_schema_extension} | {output_filename} |".format(
                rank=row["rank"] if row["rank"] is not None else "-",
                status=row["status"],
                variant_id=row["variant_id"],
                threshold=row["threshold"],
                margin=row["margin"],
                mincut=row["mincut"] if row["mincut"] is not None else "-",
                minclip=row["minclip"] if row["minclip"] is not None else "-",
                output_duration_seconds=f"{float(row['output_duration_seconds']):.2f}"
                if row["output_duration_seconds"] is not None
                else "-",
                delta_seconds=f"{float(row['delta_seconds']):.2f}"
                if row["delta_seconds"] is not None
                else "-",
                cut_count=row["cut_count"] if row["cut_count"] is not None else "-",
                cut_median_seconds=f"{float(row['cut_median_seconds']):.2f}"
                if row["cut_median_seconds"] is not None
                else "-",
                requires_schema_extension="yes" if row["requires_schema_extension"] else "no",
                output_filename=row["output_filename"]
                if row["status"] == "success"
                else "-",
            )
        )

    lines.extend(["", "## Copy-ready voice overrides", ""])
    for row in rows:
        lines.append(f"### {row['variant_id']}")
        lines.append("")
        lines.append("```yaml")
        lines.append(str(row["voice_override_snippet"]))
        lines.append("```")
        if row["requires_schema_extension"]:
            lines.append(
                "Needs a schema extension to persist `mincut` / `minclip`; only `threshold` and `margin` are shown above."
            )
        if row["status"] != "success":
            lines.append(f"Status: `{row['status']}`")
            if row["error"]:
                lines.append(f"Error: `{row['error']}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_playlist(rows: list[dict[str, object]]) -> str:
    """Build an M3U playlist ordered from sharpest to safest."""
    lines = ["#EXTM3U"]
    for row in rows:
        if row["status"] != "success":
            continue
        description = (
            f"{row['variant_id']} | threshold={row['threshold']} | margin={row['margin']} | "
            f"mincut={row['mincut'] if row['mincut'] is not None else '-'} | "
            f"minclip={row['minclip'] if row['minclip'] is not None else '-'}"
        )
        lines.append(f"#EXTINF:-1,{description}")
        lines.append(str(row["output_relative_path"]))
    return "\n".join(lines).rstrip() + "\n"


def top_ranked_rows(rows: list[dict[str, object]], limit: int = 5) -> list[dict[str, object]]:
    """Return the highest ranked successful rows."""
    return [row for row in rows if row["status"] == "success"][:limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate audio-only auto-editor tuning variants for one input file.",
    )
    parser.add_argument("audio_path", help="Path to the source audio file to sweep")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for this tuning run. Defaults to a unique folder under /tmp.",
    )
    parser.add_argument(
        "--preset",
        default=DEFAULT_PRESET,
        choices=[DEFAULT_PRESET],
        help="Variant preset to use (default: sharp_tiktok).",
    )
    parser.add_argument(
        "--threshold",
        dest="thresholds",
        action="append",
        type=normalize_threshold,
        help="Override the threshold list. Repeat the flag to add several values.",
    )
    parser.add_argument(
        "--margin",
        dest="margins",
        action="append",
        type=normalize_margin,
        help="Override the margin list. Repeat the flag to add several values.",
    )
    parser.add_argument(
        "--no-sharp-presets",
        action="store_true",
        help="Disable the additional mincut/minclip sharp presets.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned variants and output folder without running auto-editor.",
    )
    return parser.parse_args()


async def generate_variants(args: argparse.Namespace) -> int:
    """Execute the requested sweep and write all output artifacts."""
    audio_path = Path(args.audio_path).expanduser().resolve()
    if not audio_path.exists():
        print(f"Input audio not found: {audio_path}", file=sys.stderr)
        return 1
    if not audio_path.is_file():
        print(f"Input path is not a file: {audio_path}", file=sys.stderr)
        return 1

    thresholds = tuple(args.thresholds or DEFAULT_THRESHOLDS)
    margins = tuple(args.margins or DEFAULT_MARGINS)
    variants = build_variants(
        thresholds,
        margins,
        include_sharp_presets=not args.no_sharp_presets,
    )
    output_dir = make_output_dir(audio_path, args.output_dir)

    if args.dry_run:
        print(f"Output directory: {output_dir}")
        print(f"Preset: {args.preset}")
        print(f"Planned variants: {len(variants)}")
        for variant in variants:
            advanced_suffix = (
                f" mincut={variant.mincut} minclip={variant.minclip}"
                if variant.requires_schema_extension
                else ""
            )
            print(
                f"- {variant.variant_id}: threshold={variant.threshold} margin={variant.margin}{advanced_suffix}"
            )
        return 0

    variants_dir = output_dir / "variants"
    logs_dir = output_dir / "logs"
    variants_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    input_duration_seconds = await probe_duration_seconds(audio_path)
    rows: list[dict[str, object]] = []

    for variant in variants:
        row = await process_variant(
            variant,
            audio_path=audio_path,
            input_duration_seconds=input_duration_seconds,
            variants_dir=variants_dir,
            logs_dir=logs_dir,
        )
        rows.append(row)

    ordered_rows = rank_variants(rows)
    comparison_json_path = output_dir / "comparison.json"
    comparison_md_path = output_dir / "comparison.md"
    playlist_path = output_dir / "ranked_playlist.m3u"
    success_count = sum(1 for row in ordered_rows if row["status"] == "success")
    payload = {
        "preset": args.preset,
        "source_audio": str(audio_path),
        "source_audio_duration_seconds": input_duration_seconds,
        "output_dir": str(output_dir),
        "variant_count": len(variants),
        "successful_variant_count": success_count,
        "failed_variant_count": len(variants) - success_count,
        "variants": ordered_rows,
    }
    comparison_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    comparison_md_path.write_text(
        build_markdown(
            audio_path=audio_path,
            output_dir=output_dir,
            preset=args.preset,
            input_duration_seconds=input_duration_seconds,
            rows=ordered_rows,
        ),
        encoding="utf-8",
    )
    playlist_path.write_text(build_playlist(ordered_rows), encoding="utf-8")

    print(output_dir)
    for row in top_ranked_rows(ordered_rows):
        print(
            "rank={rank} variant={variant_id} output={output_duration_seconds:.2f}s "
            "cuts={cut_count} cut_median={cut_median_seconds:.2f}s".format(**row)
        )

    return 0 if success_count > 0 else 1


async def async_main() -> int:
    args = parse_args()
    return await generate_variants(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
