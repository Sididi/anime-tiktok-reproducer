"""Freeze a dated TikTok corpus, download videos and transcribe locally.

Run with the project's Python. Artifacts stay in ignored backend/data/research.
The manifest is immutable on resume; failures never silently replace a sample.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
DEFAULT_OUTPUT = ROOT / "backend/data/research/facecam-2026-09-26"
# Same fallback as DownloaderService: this host gets empty impersonated responses.
YTDLP = [sys.executable, "-c", 'import sys; sys.modules["curl_cffi"] = None; from yt_dlp import main; main()',
         "--ignore-config", "--socket-timeout", "25", "--retries", "2"]


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def latest(entries: list[dict], count: int) -> list[dict]:
    unique = {}
    for entry in entries:
        if not re.fullmatch(r"\d+", str(entry.get("id", ""))):
            raise ValueError("Missing or invalid TikTok video ID")
        if not isinstance(entry.get("timestamp"), (int, float)):
            raise ValueError("Publication timestamp missing; cannot establish latest videos")
        unique[entry["id"]] = entry
    ordered = sorted(unique.values(), key=lambda e: (e["timestamp"], int(e["id"])), reverse=True)
    if len(ordered) < count:
        raise ValueError(f"Only {len(ordered)} unique publications; {count} requested")
    return ordered[:count]


def freeze(output: Path, creator: str, count: int, listing: Path | None):
    directory = output / creator
    manifest = directory / "manifest.json"
    if manifest.exists():
        existing = json.loads(manifest.read_text())
        if existing["requested_count"] != count or existing["creator"] != creator:
            raise ValueError("Existing manifest differs; use a new output directory")
        return existing
    directory.mkdir(parents=True, exist_ok=True)
    if listing:
        payload = json.loads(listing.read_text())
    else:
        result = subprocess.run(YTDLP + ["--flat-playlist", "--playlist-end", str(count + 15),
            "--dump-single-json", f"https://www.tiktok.com/@{creator}"],
            capture_output=True, text=True, timeout=300, check=True)
        payload = json.loads(result.stdout)
    selected = latest(payload["entries"], count)
    if any(e.get("uploader", "").casefold() != creator.casefold() for e in selected):
        raise ValueError("Listing contains a different creator")
    save_json(directory / "discovery.json", payload)
    fields = ("id", "timestamp", "duration", "description", "title", "view_count", "like_count",
              "comment_count", "repost_count", "save_count")
    data = {"creator": creator, "requested_count": count,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "selection": "unique publications, descending publication timestamp; pinned order ignored",
            "discovered_count": len(payload["entries"]),
            "videos": [{**{k: e.get(k) for k in fields},
                        "url": f"https://www.tiktok.com/@{creator}/video/{e['id']}"} for e in selected]}
    save_json(manifest, data)
    return data


def probe(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=True)
    metadata = json.loads(result.stdout)
    types = {s["codec_type"] for s in metadata["streams"]}
    if not {"audio", "video"}.issubset(types):
        raise ValueError("Downloaded file must contain both video and audio")
    return metadata


def download(directory: Path, entry: dict) -> Path:
    folder = directory / "videos" / entry["id"]
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "video.mp4"
    if video.exists():
        probe(video)
        return video
    with (folder / "download.log").open("w") as log:
        subprocess.run(YTDLP + ["--no-playlist", "--no-progress", "--write-info-json",
            "-f", "b[ext=mp4][acodec!=none]/b[acodec!=none]/best", "--remux-video", "mp4",
            "-o", str(folder / "video.%(ext)s"), entry["url"]],
            stdout=log, stderr=log, timeout=300, check=True)
    probe(video)
    return video


# Remove only the branded opening, never later story content. Raw ASR is retained.
BRANDED_INTRO = re.compile(
    r"^\s*(?:(?:salut|hey|yo)[,!\s]+)?j[e’'\s]*(?:(?:te|vous|t['’])\s*)?"
    r"pr[ée]sente\s+(?:un\s+)?(?:nouvel|nouveau)\s+anim[ée][,!\s]+tous\s+les\s+jours[.!,:;\s]*",
    re.IGNORECASE,
)


def analysis_text(creator: str, text: str) -> tuple[str, str]:
    match = BRANDED_INTRO.match(text) if creator == "theomcx" else None
    return (text[match.end():].lstrip(), match.group()) if match else (text, "")


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def transcribe(video: Path, creator: str, model, model_name: str, batch_size: int):
    from faster_whisper.audio import decode_audio
    from app.services.asr_engine import decode_with_coverage, uncovered_speech_spans
    audio = decode_audio(str(video), sampling_rate=16000)
    segments, language, speech = decode_with_coverage(model, audio, language="fr", batch_size=batch_size)
    text = " ".join(s["text"].strip() for s in segments).strip()
    if not text:
        raise ValueError("No speech transcribed")
    cleaned, removed = analysis_text(creator, text)
    data = {"engine": "faster-whisper + project coverage repair", "model": model_name,
            "language": language, "transcribed_at": datetime.now(timezone.utc).isoformat(),
            "video_sha256": file_sha256(video),
            "duration": len(audio) / 16000, "segments": segments, "text": text,
            "analysis_text": cleaned, "excluded_branded_intro": removed,
            "speech_regions": speech, "uncovered_speech": uncovered_speech_spans(speech, segments),
            "timing_precision": "ASR segment estimates, not forced-aligned words"}
    save_json(video.parent / "transcript.json", data)
    (video.parent / "transcript.txt").write_text(text + "\n", encoding="utf-8")
    return data


def run(args):
    from faster_whisper import WhisperModel
    tasks = []
    for creator in args.creators:
        listing = args.listings / f"facecam-{creator}-list.json" if args.listings else None
        manifest = freeze(args.output, creator, args.count, listing)
        tasks.extend((creator, entry) for entry in manifest["videos"])
    # Interleave creators so early inspection never covers only one account.
    tasks.sort(key=lambda pair: -pair[1]["timestamp"])
    model = None
    failures = []
    with ThreadPoolExecutor(max_workers=args.download_workers) as pool:
        futures = {pool.submit(download, args.output / creator, entry): (creator, entry)
                   for creator, entry in tasks}
        for done, future in enumerate(as_completed(futures), 1):
            creator, entry = futures[future]
            try:
                video = future.result()
                path = video.parent / "transcript.json"
                result = None
                if path.exists():
                    try:
                        cached = json.loads(path.read_text())
                        if (cached.get("model") == args.model and cached.get("text", "").strip()
                                and cached.get("video_sha256") == file_sha256(video)):
                            result = cached
                    except (ValueError, TypeError, AttributeError):
                        pass
                if result is None:
                    if model is None:
                        print("Loading local ASR model", flush=True)
                        model = WhisperModel(args.model, device=args.device,
                            compute_type="int8_float16" if args.device == "cuda" else "int8", cpu_threads=4)
                    result = transcribe(video, creator, model, args.model, args.batch_size)
                print(f"{done}/{len(tasks)} {creator}/{entry['id']} {len(result['text'].split())} words", flush=True)
            except Exception as exc:
                # Download logs retain the detailed diagnostic locally.
                failures.append({"creator": creator, "id": entry["id"], "error": str(exc)})
                print(f"FAILED {creator}/{entry['id']}: {type(exc).__name__}", flush=True)
            save_json(args.output / "run-status.json", {"processed": done, "target": len(tasks), "failures": failures})
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--creators", nargs="+", default=["theomcx", "adhnime"])
    parser.add_argument("--count", type=int, default=75)
    parser.add_argument("--listings", type=Path, help="Directory containing facecam-<creator>-list.json")
    parser.add_argument("--download-workers", type=int, default=2)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args()
    if args.count < 1 or not 1 <= args.download_workers <= 4 or args.batch_size < 1:
        parser.error("count/batch-size must be positive; download-workers must be between 1 and 4")
    if any(not re.fullmatch(r"[a-zA-Z0-9_.]+", c) or c in {".", ".."} for c in args.creators):
        parser.error("Invalid TikTok creator handle")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
