"""Read-only ASR + diarization throughput benchmark on existing project WAVs.

Run each capacity in a fresh process with the SAME inputs, then repeat in
reverse order to expose cache/thermal effects. No project files are written.
The output records total batch time, per-stage times, profiles, errors and
total device VRAM (including the desktop, unlike torch's allocator counters).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

for key, value in {"OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4",
                   "TORCHINDUCTOR_COMPILE_THREADS": "1", "JOBLIB_MULTIPROCESSING": "0"}.items():
    os.environ.setdefault(key, value)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=[1, 2], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, choices=[1, 2, 4, 8], default=8)
    parser.add_argument("audio", nargs="+", type=Path)
    args = parser.parse_args()
    from app.services.transcriber import TranscriberService as T
    from app.services.raw_scene_detector import RawSceneDetectorService as R
    from app.config import settings
    settings.transcription_batch_size = args.batch_size

    profiles: dict[int, list[str]] = {}
    class Profiles(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("ASR profile:"):
                profiles.setdefault(record.thread, []).append(record.getMessage())
    logging.basicConfig(level=logging.WARNING)
    asr_logger = logging.getLogger("app.services.transcriber")
    asr_logger.setLevel(logging.INFO)
    asr_logger.addHandler(Profiles())
    stop = threading.Event()
    samples = []
    def sample():
        while not stop.is_set():
            raw = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True)
            try:
                samples.append([time.monotonic(), *map(int, raw.stdout.strip().split(","))])
            except ValueError:
                pass
            stop.wait(0.5)
    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()

    def run(path):
        started = time.monotonic()
        row = {"audio": str(path), "bytes": path.stat().st_size}
        profiles[threading.get_ident()] = []
        try:
            result = T._transcribe_sync(path, None, "large-v3")
            words, language = result
            row.update(asr_seconds=time.monotonic()-started, language=language,
                       words=len(words), gaps=getattr(result, "unrecovered_gaps", []),
                       text_sha256=hashlib.sha256(" ".join(w["text"] for w in words).encode()).hexdigest())
            T.unload_models()
            diar_started = time.monotonic()
            analysis = R.analyze(path)
            row.update(diarization_seconds=time.monotonic()-diar_started,
                       diarization_error=analysis.error, diarization_warning=analysis.warning,
                       segments=len(analysis.segments))
        except Exception as exc:
            row["error"] = str(exc)
        finally:
            T.unload_models()
        row.update(total_seconds=time.monotonic()-started,
                   profiles=profiles.get(threading.get_ident(), []))
        print(json.dumps(row), flush=True)
        return row

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(run, args.audio))
    finally:
        stop.set()
        monitor.join()
    output = {"workers": args.workers, "batch_size": args.batch_size, "wall_seconds": time.monotonic()-started,
              "peak_device_memory_mib": max((r[1] for r in samples), default=None),
              "gpu_start": samples[0] if samples else None, "gpu_end": samples[-1] if samples else None,
              "jobs": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2))
    print(json.dumps({key: value for key, value in output.items() if key != "jobs"}), flush=True)
    return int(any(row.get("error") or row.get("diarization_error") for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
