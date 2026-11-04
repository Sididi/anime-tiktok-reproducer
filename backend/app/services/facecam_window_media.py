"""Seek and decode short local windows, never a whole episode for V4 retrieval."""
from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path

from ..config import settings
from .facecam_illustration_llm import write_json
from .facecam_visual_index import FacecamVisualIndex, file_identity, fingerprint


class FacecamWindowMedia:
    VERSION = 1

    @classmethod
    def probe(cls, source: Path):
        import av
        identity = file_identity(source)
        with av.open(str(source)) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate)
            rate = FacecamVisualIndex.native_rate(fps)
            duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                        else float(container.duration / av.time_base))
            if abs(rate.rate_float - fps) > 0.002 or not math.isfinite(duration) or duration <= 0:
                raise ValueError(f"Cadence ou durée vidéo V4 inexploitable : {source.name}.")
        return {"source": str(source.resolve()), "identity": identity,
                "fingerprint": fingerprint([cls.VERSION, identity, settings.facecam_illustration_min_preview_seconds]),
                "fps": rate.rate_float,
                "duration": duration, "frame_count": round(duration * rate.rate_float)}

    @classmethod
    def window(cls, info, start: float, end: float, check, *, sample_count=2):
        import av
        import cv2
        from PIL import Image
        from scenedetect import ContentDetector

        source = Path(info["source"])
        fps = info["fps"]
        first = max(0, math.ceil(start * fps - 1e-5))
        last = min(info["frame_count"], math.floor(end * fps + 1e-5))
        if last <= first or last - first > math.ceil(16 * fps):
            raise ValueError("Une fenêtre V4 doit durer au maximum 16 secondes.")
        if sample_count not in {2, 3}:
            raise ValueError("Deux ou trois images sont nécessaires pour vérifier un plan.")
        directory = settings.cache_dir / "facecam_windows" / info["fingerprint"]
        cache = directory / f"{first}-{last}{'-3frames' if sample_count == 3 else ''}.json"
        with FacecamVisualIndex.locked(cache, check=check):
            check()
            if file_identity(source) != info["identity"]:
                raise ValueError("L'épisode V4 a changé pendant la recherche.")
            if cache.exists():
                shots = json.loads(cache.read_text())
                if all(all((directory / f"{frame}.png").is_file() for frame in s["samples"]) for s in shots):
                    return shots
            frames = {}
            cuts = []
            detector = ContentDetector(threshold=27, min_scene_len=max(1, round(fps * 0.3)))
            with av.open(str(source)) as container:
                stream = container.streams.video[0]
                stream.codec_context.thread_count = 2
                stream.thread_type = "SLICE"
                origin = (stream.start_time or 0) * stream.time_base
                container.seek(int((first / fps + float(origin)) / stream.time_base), stream=stream, backward=True)
                for frame in container.decode(stream):
                    check()
                    if frame.pts is None:
                        raise ValueError("Image V4 sans horodatage local.")
                    position = float(frame.pts * frame.time_base - origin) * fps
                    index = round(position)
                    if index < first:
                        continue
                    if index >= last:
                        break
                    if abs(index - position) > 0.1 or index != first + len(frames):
                        raise ValueError("Horloge vidéo V4 discontinue ; aucun timecode n'a été deviné.")
                    scale = min(352 / frame.width, 198 / frame.height)
                    pixels = frame.reformat(width=max(1, round(frame.width * scale)),
                                            height=max(1, round(frame.height * scale)), format="rgb24").to_ndarray()
                    frames[index] = pixels
                    cuts.extend(detector.process_frame(index, cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)))
            if len(frames) != last - first:
                raise ValueError(f"Décodage incomplet de la fenêtre V4 dans {source.name}.")
            cuts.extend(detector.post_process(last - 1))
            bounds = sorted({first, last, *(c for c in cuts if first < c < last)})
            directory.mkdir(parents=True, exist_ok=True)
            shots = []
            for a, b in zip(bounds, bounds[1:]):
                if (b - a) / fps < settings.facecam_illustration_min_preview_seconds:
                    continue
                samples = [a + i * (b - a) // (sample_count + 1) for i in range(1, sample_count + 1)]
                for sample in samples:
                    path = directory / f"{sample}.png"
                    temporary = path.with_suffix(".png.tmp")
                    Image.fromarray(frames[sample]).save(temporary, format="PNG")
                    temporary.replace(path)
                shots.append({"shot_id": f"{info['fingerprint'][:8]}f{a}-{b}", "episode": info["source"],
                    "episode_fingerprint": info["fingerprint"], "fps": fps,
                    "in_frame": a, "out_frame": b, "start": a / fps, "end": b / fps,
                    "samples": samples, "frames_dir": str(directory)})
            if file_identity(source) != info["identity"]:
                raise ValueError("L'épisode V4 a changé pendant le décodage.")
            write_json(cache, shots)
            return shots

    @staticmethod
    def contact_sheets(shots, *, shots_per_sheet=4):
        from PIL import Image, ImageDraw

        sheets = []
        for offset in range(0, len(shots), shots_per_sheet):
            batch = shots[offset:offset + shots_per_sheet]
            columns = max(len(s["samples"]) for s in batch)
            sheet = Image.new("RGB", (352 * columns, 222 * len(batch)), "#111111")
            draw = ImageDraw.Draw(sheet)
            for row, shot in enumerate(batch):
                for col, frame in enumerate(shot["samples"]):
                    with Image.open(Path(shot["frames_dir"]) / f"{frame}.png") as source:
                        thumbnail = source.convert("RGB")
                    x, y = col * 352, row * 222
                    sheet.paste(thumbnail, (x + (352 - thumbnail.width) // 2, y + 24))
                    draw.text((x + 4, y + 4), f"{shot['shot_id']} / {frame}", fill="white")
            output = io.BytesIO()
            sheet.save(output, format="JPEG", quality=85)
            sheets.append("data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode())
        return sheets
