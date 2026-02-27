#!/usr/bin/env python3
"""
trim_silence.py — Remove leading silence from audio files using ffmpeg.

Usage examples
--------------
# Analyse only (no modification)
  python trim_silence.py audio.wav --check

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
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MUSIC_CONFIG_PATH = REPO_ROOT / "config" / "music" / "config.yaml"
MANIFEST_FILENAME = ".trim_manifest.json"

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
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[ERROR] ffmpeg / ffprobe not found in PATH.", file=sys.stderr)
        sys.exit(1)


def _probe_duration_ms(path: Path) -> float:
    """Return duration in milliseconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return float(result.stdout.strip()) * 1000.0


def _detect_leading_silence_ms(path: Path, threshold: str) -> float:
    """
    Return the duration of leading silence in milliseconds.

    Uses ffmpeg's silencedetect filter and parses its output to find the
    first 'silence_end' timestamp, which marks where audio actually starts.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(path),
            "-af", f"silencedetect=noise={threshold}:duration=0.01",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    # silencedetect writes to stderr
    output = result.stderr
    for line in output.splitlines():
        if "silence_end" in line:
            # format: "silence_end: 0.234 | silence_duration: 0.234"
            parts = line.split("silence_end:")
            if len(parts) > 1:
                end_str = parts[1].split("|")[0].strip()
                return float(end_str) * 1000.0
    return 0.0


def _trim_audio(
    input_path: Path,
    output_path: Path,
    threshold: str,
) -> float:
    """
    Run ffmpeg silenceremove on start periods only.
    Returns the actual number of ms trimmed (estimated via duration delta).
    """
    duration_before = _probe_duration_ms(input_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af", (
            f"silenceremove="
            f"start_periods=1:"
            f"start_duration=0:"
            f"start_threshold={threshold}"
        ),
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg silenceremove failed:\n{result.stderr.strip()}")

    duration_after = _probe_duration_ms(output_path)
    return max(0.0, duration_before - duration_after)


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------

def check_file(path: Path, threshold: str) -> None:
    """Analyse leading silence without touching the file."""
    path = path.resolve()
    if not path.exists():
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    print(f"Analysing: {path.name}")
    silence_ms = _detect_leading_silence_ms(path, threshold)
    if silence_ms > 0:
        print(f"  → Leading silence detected: {silence_ms:.1f} ms  (threshold {threshold})")
    else:
        print(f"  → No leading silence detected at threshold {threshold}")
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
    silence_ms = _detect_leading_silence_ms(input_path, threshold)
    if silence_ms == 0:
        print(f"[OK]   {input_path.name} — no leading silence detected, nothing to do")
        if in_place and not _is_already_processed(input_path):
            if not dry_run:
                _mark_processed(input_path, 0.0)
        return

    print(f"[TRIM] {input_path.name} — {silence_ms:.1f} ms of leading silence to remove")

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
            trimmed_ms = _trim_audio(input_path, tmp_path, threshold)
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
        trimmed_ms = _trim_audio(input_path, effective_output, threshold)
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
        description="Remove leading silence from audio files using ffmpeg.",
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
        help="Analyse leading silence without modifying the file.",
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
