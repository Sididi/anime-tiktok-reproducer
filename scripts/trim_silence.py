#!/usr/bin/env python3
"""
trim_silence.py — Remove leading and trailing silence from audio files using ffmpeg.

Usage examples
--------------
# Analyse only (no modification)
  python trim_silence.py --check audio.wav

# In-place trim (creates a .bak backup by default)
  python trim_silence.py audio.wav

# In-place trim without backup
  python trim_silence.py audio.wav --no-backup

# Trim to a specific output file
  python trim_silence.py audio.wav -o cleaned.wav

# Process all music files declared in config (skips already-processed ones)
  python trim_silence.py --all

# Preview what --all would do, without touching any file
  python trim_silence.py --all --dry-run

# Re-process even if already in the manifest
  python trim_silence.py audio.wav --force
  python trim_silence.py --all --force

# Custom silence threshold (default: -50dB)
  python trim_silence.py audio.wav --threshold -40dB
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _env import load_dotenv
from _media_binaries import (
    get_ffmpeg_binary,
    get_ffprobe_binary,
    get_media_subprocess_env,
    rewrite_media_command,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MUSIC_CONFIG_PATH = REPO_ROOT / "config" / "music" / "config.yaml"
MANIFEST_FILENAME = ".trim_manifest.json"
SILENCE_DETECT_DURATION_S = 0.01
EDGE_EPSILON_S = 0.001


@dataclass(frozen=True)
class SilenceWindow:
    start_s: float
    end_s: float


@dataclass(frozen=True)
class TrimAnalysis:
    duration_ms: float
    leading_ms: float
    trailing_ms: float

    @property
    def total_trim_ms(self) -> float:
        return self.leading_ms + self.trailing_ms

    @property
    def content_end_ms(self) -> float:
        return max(self.leading_ms, self.duration_ms - self.trailing_ms)


# ---------------------------------------------------------------------------
# Manifest helpers  (tracks already-processed files via their MD5)
# ---------------------------------------------------------------------------


def _manifest_path(audio_path: Path) -> Path:
    """Manifest lives alongside the audio file."""
    return audio_path.parent / MANIFEST_FILENAME


def _load_manifest(audio_path: Path) -> dict:
    p = _manifest_path(audio_path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_manifest(audio_path: Path, manifest: dict) -> None:
    p = _manifest_path(audio_path)
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_already_processed(audio_path: Path) -> bool:
    manifest = _load_manifest(audio_path)
    key = audio_path.name
    if key not in manifest:
        return False
    return manifest[key].get("md5") == _md5(audio_path)


def _mark_processed(audio_path: Path, trimmed_ms: float) -> None:
    manifest = _load_manifest(audio_path)
    manifest[audio_path.name] = {
        "md5": _md5(audio_path),
        "trimmed_ms": round(trimmed_ms, 2),
    }
    _save_manifest(audio_path, manifest)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _check_ffmpeg() -> None:
    try:
        ffmpeg_binary = get_ffmpeg_binary()
        ffprobe_binary = get_ffprobe_binary()
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    if (ffmpeg_binary == "ffmpeg" and not shutil.which("ffmpeg")) or (
        ffprobe_binary == "ffprobe" and not shutil.which("ffprobe")
    ):
        print("[ERROR] ffmpeg / ffprobe not found in PATH.", file=sys.stderr)
        sys.exit(1)


def _probe_duration_ms(path: Path) -> float:
    """Return duration in milliseconds via ffprobe."""
    cmd = rewrite_media_command(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=get_media_subprocess_env(cmd),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return float(result.stdout.strip()) * 1000.0


def _run_silencedetect(path: Path, threshold: str) -> str:
    """Run ffmpeg silencedetect and return stderr output."""
    cmd = rewrite_media_command(
        [
            "ffmpeg", "-i", str(path),
            "-af", f"silencedetect=noise={threshold}:duration={SILENCE_DETECT_DURATION_S}",
            "-f", "null", "-",
        ]
    )
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=get_media_subprocess_env(cmd),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg silencedetect failed:\n{result.stderr.strip()}")
    # silencedetect writes to stderr
    return result.stderr


def _parse_silence_windows(output: str, duration_ms: float) -> list[SilenceWindow]:
    """Parse ffmpeg silencedetect output into silence windows."""
    windows: list[SilenceWindow] = []
    current_start: float | None = None

    for line in output.splitlines():
        if "silence_start:" in line:
            start_str = line.split("silence_start:", 1)[1].strip()
            current_start = float(start_str)
            continue

        if "silence_end:" in line:
            end_str = line.split("silence_end:", 1)[1].split("|", 1)[0].strip()
            end_s = float(end_str)
            start_s = current_start if current_start is not None else 0.0
            windows.append(
                SilenceWindow(
                    start_s=max(0.0, start_s),
                    end_s=max(start_s, end_s),
                )
            )
            current_start = None

    if current_start is not None:
        windows.append(
            SilenceWindow(
                start_s=max(0.0, current_start),
                end_s=duration_ms / 1000.0,
            )
        )

    return windows


def _analyze_trim(path: Path, threshold: str) -> TrimAnalysis:
    """Return trim bounds for leading and trailing silence only."""
    duration_ms = _probe_duration_ms(path)
    output = _run_silencedetect(path, threshold)
    windows = _parse_silence_windows(output, duration_ms)

    leading_ms = 0.0
    if windows and windows[0].start_s <= EDGE_EPSILON_S:
        leading_ms = min(duration_ms, windows[0].end_s * 1000.0)

    content_end_ms = duration_ms
    if windows and (duration_ms / 1000.0 - windows[-1].end_s) <= EDGE_EPSILON_S:
        content_end_ms = max(leading_ms, windows[-1].start_s * 1000.0)

    trailing_ms = max(0.0, duration_ms - content_end_ms)
    return TrimAnalysis(
        duration_ms=duration_ms,
        leading_ms=leading_ms,
        trailing_ms=trailing_ms,
    )


def _format_trim_parts(analysis: TrimAnalysis) -> str:
    """Format per-edge trim details for CLI output."""
    parts: list[str] = []
    if analysis.leading_ms > 0:
        parts.append(f"{analysis.leading_ms:.1f} ms leading")
    if analysis.trailing_ms > 0:
        parts.append(f"{analysis.trailing_ms:.1f} ms trailing")
    return " + ".join(parts)


def _trim_audio(
    input_path: Path,
    output_path: Path,
    analysis: TrimAnalysis,
) -> float:
    """
    Trim only the leading/trailing silence bounds detected by _analyze_trim.
    Returns the actual number of ms trimmed (estimated via duration delta).
    """
    duration_before = _probe_duration_ms(input_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af",
        (
            f"atrim="
            f"start={analysis.leading_ms / 1000.0:.6f}:"
            f"end={analysis.content_end_ms / 1000.0:.6f},"
            f"asetpts=PTS-STARTPTS"
        ),
        str(output_path),
    ]
    cmd = rewrite_media_command(cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=get_media_subprocess_env(cmd),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg trim failed:\n{result.stderr.strip()}")

    duration_after = _probe_duration_ms(output_path)
    return max(0.0, duration_before - duration_after)


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------

def check_file(path: Path, threshold: str) -> None:
    """Analyse leading/trailing silence without touching the file."""
    path = path.resolve()
    if not path.exists():
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    print(f"Analysing: {path.name}")
    analysis = _analyze_trim(path, threshold)
    if analysis.total_trim_ms > 0:
        trim_parts = _format_trim_parts(analysis)
        print(
            f"  → Silence detected: {trim_parts} "
            f"({analysis.total_trim_ms:.1f} ms total, threshold {threshold})"
        )
    else:
        print(f"  → No leading/trailing silence detected at threshold {threshold}")
    already = _is_already_processed(path)
    print(f"  → Already trimmed (manifest): {already}")


def trim_file(
    input_path: Path,
    output_path: Path | None,
    threshold: str,
    backup: bool,
    force: bool,
    dry_run: bool,
) -> None:
    input_path = input_path.resolve()
    if not input_path.exists():
        print(f"[ERROR] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    in_place = output_path is None
    effective_output = output_path.resolve() if output_path else input_path

    # --- Manifest check ---
    if not force and in_place and _is_already_processed(input_path):
        print(f"[SKIP] {input_path.name} — already processed (use --force to override)")
        return

    # --- Quick silence check ---
    analysis = _analyze_trim(input_path, threshold)
    if analysis.total_trim_ms == 0:
        print(f"[OK]   {input_path.name} — no leading/trailing silence detected, nothing to do")
        if in_place and not _is_already_processed(input_path):
            if not dry_run:
                _mark_processed(input_path, 0.0)
        return

    trim_parts = _format_trim_parts(analysis)
    print(
        f"[TRIM] {input_path.name} — {trim_parts} "
        f"({analysis.total_trim_ms:.1f} ms total) to remove"
    )

    if dry_run:
        print("       (dry-run: no file written)")
        return

    # --- Actual trim ---
    if in_place:
        # Write to a temp file first, then atomically replace
        with tempfile.NamedTemporaryFile(
            suffix=input_path.suffix, dir=input_path.parent, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            trimmed_ms = _trim_audio(input_path, tmp_path, analysis)
            if backup:
                bak_path = input_path.with_suffix(input_path.suffix + ".bak")
                shutil.copy2(input_path, bak_path)
                print(f"       Backup saved → {bak_path.name}")
            shutil.move(str(tmp_path), str(input_path))
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        _mark_processed(input_path, trimmed_ms)
        print(f"       Done — {trimmed_ms:.1f} ms removed  ✓")
    else:
        trimmed_ms = _trim_audio(input_path, effective_output, analysis)
        print(f"       Done — {trimmed_ms:.1f} ms removed → {effective_output}  ✓")


def trim_all(threshold: str, force: bool, dry_run: bool, config_path: Path) -> None:
    """Process all music files declared in the music config."""
    if not config_path.exists():
        print(f"[ERROR] Music config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    music_entries = config.get("music", {})
    if not music_entries:
        print("[WARN] No music entries found in config.")
        return

    print(f"Found {len(music_entries)} music file(s) in config.\n")
    for key, entry in music_entries.items():
        file_path = Path(entry["file_path"])
        print(f"── {key}: {file_path.name}")
        trim_file(
            input_path=file_path,
            output_path=None,
            threshold=threshold,
            backup=True,
            force=force,
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trim_silence",
        description="Remove leading and trailing silence from audio files using ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # --- Target (mutually exclusive) ---
    target = p.add_mutually_exclusive_group()
    target.add_argument(
        "input",
        nargs="?",
        type=Path,
        metavar="INPUT",
        help="Input audio file to process.",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="Process all files declared in the music config.",
    )
    target.add_argument(
        "--check",
        type=Path,
        metavar="FILE",
        help="Analyse leading/trailing silence without modifying the file.",
    )

    # --- Output ---
    p.add_argument(
        "-o", "--output",
        type=Path,
        metavar="OUTPUT",
        help="Output file (only valid with INPUT). Defaults to in-place editing.",
    )

    # --- Options ---
    p.add_argument(
        "--threshold",
        default="-50dB",
        metavar="dB",
        help="Silence threshold (default: -50dB). Example: -40dB",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable automatic backup when editing in-place.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-process even if the file is already in the manifest.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse and report without writing any file.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=MUSIC_CONFIG_PATH,
        metavar="CONFIG",
        help=f"Path to music config YAML (default: {MUSIC_CONFIG_PATH})",
    )

    return p


def main() -> None:
    load_dotenv(str(REPO_ROOT / ".env"))
    _check_ffmpeg()
    parser = build_parser()
    args = parser.parse_args()

    if args.check:
        check_file(args.check, args.threshold)

    elif args.all:
        trim_all(
            threshold=args.threshold,
            force=args.force,
            dry_run=args.dry_run,
            config_path=args.config,
        )

    elif args.input:
        if args.output and args.input == args.output:
            print("[ERROR] INPUT and OUTPUT must be different paths.", file=sys.stderr)
            sys.exit(1)
        trim_file(
            input_path=args.input,
            output_path=args.output,
            threshold=args.threshold,
            backup=not args.no_backup,
            force=args.force,
            dry_run=args.dry_run,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
