"""Shared episode-local shot catalogue: actual frames, local subtitles, cached vision."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import threading
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..config import settings
from .facecam_illustration_llm import IllustrationBudget, write_json
from .llm_config_service import LLMConfigService


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_identity(path: Path):
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]


def check_cancelled(event):
    if event is not None and event.is_set():
        raise RuntimeError("Recherche d'illustrations V4 annulée ; les lots terminés restent en cache.")


class ShotDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    shot_id: str
    visual: str = Field(min_length=1, max_length=300)
    usable: StrictBool


class ShotDescriptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shots: list[ShotDescription]


class FacecamVisualIndex:
    VERSION = 1
    BATCH_SIZE = 32
    PROMPT = Path(__file__).parents[2] / "prompts/facecam/visual_index.md"
    _guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    @classmethod
    def lock(cls, key):
        with cls._guard:
            return cls._locks.setdefault(str(key), threading.Lock())

    @classmethod
    @contextmanager
    def locked(cls, key):
        import fcntl
        with cls.lock(key):
            path = settings.cache_dir / "facecam_visual" / "locks" / fingerprint(str(key))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def subtitles(source: Path):
        from .anime_library import AnimeLibraryService as Library
        from .facecam_fact_context import FacecamFactContext
        from .premiere_subtitle_baker import PremiereSubtitleBakerService

        groups = Library.get_preferred_subtitle_language_groups(
            [t for t in Library.load_subtitle_sidecar_entries(source) if t.kind == "text"],
            target_language="fr")
        for language, tracks in groups:
            for track in tracks:
                path = Library.get_subtitle_sidecar_asset_path(source, track)
                if path and path.is_file():
                    try:
                        cues = [{"start": c.start, "end": c.end,
                                 "text": FacecamFactContext.clean_text(c.text)}
                                for c in PremiereSubtitleBakerService.parse_srt_entries(path)]
                    except (OSError, ValueError, UnicodeError):
                        continue
                    cues = [c for c in cues if c["text"] and math.isfinite(c["start"])
                            and math.isfinite(c["end"]) and 0 <= c["start"] < c["end"]]
                    if cues:
                        return {"language": language, "cues": cues}
        return {"language": None, "cues": []}

    @classmethod
    def manifest(cls, source: Path):
        """Free, deterministic cut detection. No model supplies media coordinates."""
        from scenedetect import ContentDetector, SceneManager, open_video

        identity = file_identity(source)
        key = fingerprint([cls.VERSION, identity])
        directory = settings.cache_dir / "facecam_visual" / key
        path = directory / "shots.json"
        if path.exists():
            data = json.loads(path.read_text())
            cls.validate_manifest(data)
            if data["fingerprint"] != key or data["identity"] != identity or data["source"] != str(source.resolve()):
                raise ValueError("L'index V4 ne correspond pas à ce fichier vidéo local.")
            return directory, data
        video = open_video(str(source))
        fps = float(video.frame_rate)
        expected_frames = video.duration.get_frames()
        rate = cls.native_rate(fps)
        if abs(rate.rate_float - fps) > 0.002:
            raise ValueError(f"Cadence non prise en charge pour l'index V4 : {fps} ({source.name}).")
        manager = SceneManager()
        manager.auto_downscale = False
        manager.downscale = max(1, round(video.frame_size[0] / 384))
        manager.add_detector(ContentDetector(threshold=27, min_scene_len=max(1, round(fps * 0.3))))
        manager.detect_scenes(video=video, show_progress=False)
        boundaries = [(a.get_frames(), b.get_frames())
                      for a, b in manager.get_scene_list(start_in_scene=True)]
        if not boundaries or abs(boundaries[-1][1] - expected_frames) > 1:
            raise ValueError(f"Décodage incomplet de l'épisode V4 : {source.name}.")
        # Release OpenCV's decoder, including on libraries without VideoStream.close.
        del video, manager
        shots = []
        for start, end in boundaries:
            # Long static shots are split to expose changes inside the shot.
            step = max(1, round(fps * 10))
            for first in range(start, end, step):
                last = min(end, first + step)
                if (last - first) / fps < 0.5:
                    continue
                shots.append({"shot_id": f"s{len(shots):05d}", "in_frame": first, "out_frame": last,
                              "start": rate.seconds_from_frames(first), "end": rate.seconds_from_frames(last),
                              "samples": [first + (last - first) // 3, first + 2 * (last - first) // 3]})
        data = {"fingerprint": key, "source": str(source.resolve()), "identity": identity,
                "fps": rate.rate_float, "frame_count": boundaries[-1][1] if boundaries else 0,
                "shots": shots}
        cls.validate_manifest(data)
        if file_identity(source) != identity:
            raise ValueError("L'épisode a changé pendant son découpage V4.")
        write_json(path, data)
        return directory, data

    @staticmethod
    def native_rate(fps):
        from .otio_timing import FrameRateInfo
        # 24 and 23.976 are both within a broad 0.05 tolerance. Pick the closest.
        rates = [FrameRateInfo(base, ntsc) for base in (24, 25, 30, 50, 60) for ntsc in (False, True)]
        return min(rates, key=lambda rate: abs(rate.rate_float - fps))

    @staticmethod
    def validate_manifest(data):
        if not data["shots"] or not math.isfinite(data["fps"]) or data["fps"] <= 0:
            raise ValueError("Index de plans V4 vide ou invalide.")
        previous, ids = 0, set()
        for shot in data["shots"]:
            first, last = shot["in_frame"], shot["out_frame"]
            if (shot["shot_id"] in ids or not 0 <= previous <= first < last <= data["frame_count"]
                    or not all(math.isfinite(shot[k]) for k in ("start", "end"))
                    or any(not first <= frame < last for frame in shot["samples"])
                    or abs(shot["start"] - first / data["fps"]) > 1e-6
                    or abs(shot["end"] - last / data["fps"]) > 1e-6):
                raise ValueError("Bornes locales de l'index V4 invalides.")
            previous = last
            ids.add(shot["shot_id"])

    @classmethod
    def prepare_frames(cls, source: Path, manifest, directory: Path, *, cancel_event=None):
        """Decode once in order; hundreds of independent long-GOP seeks are costly."""
        import cv2
        from PIL import Image

        directory.mkdir(parents=True, exist_ok=True)
        wanted = {frame for shot in manifest["shots"] for frame in shot["samples"]
                  if not (directory / f"{frame}.png").is_file()}
        if not wanted:
            return
        capture = cv2.VideoCapture(str(source))
        try:
            for frame in range(max(wanted) + 1):
                if frame % 100 == 0:
                    check_cancelled(cancel_event)
                if not capture.grab():
                    raise ValueError(f"Décodage incomplet des images V4 : {source.name}, image {frame}.")
                if frame not in wanted:
                    continue
                ok, pixels = capture.retrieve()
                if not ok:
                    raise ValueError(f"Image V4 illisible : {source.name}, image {frame}.")
                thumbnail = Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))
                thumbnail.thumbnail((352, 198))
                path = directory / f"{frame}.png"
                temporary = path.with_suffix(".png.tmp")
                thumbnail.save(temporary, format="PNG")
                temporary.replace(path)
        finally:
            capture.release()
        if file_identity(source) != manifest["identity"]:
            raise ValueError("L'épisode a changé pendant l'extraction des images V4.")

    @classmethod
    def contact_sheets(cls, source: Path, shots: list[dict], *, frames_dir: Path | None = None):
        """Four shots per sheet, two actual local frames each; never generated images."""
        import cv2
        from PIL import Image, ImageDraw

        capture = None if frames_dir else cv2.VideoCapture(str(source))
        sheets = []
        try:
            for offset in range(0, len(shots), 4):
                sheet = Image.new("RGB", (704, 888), "#111111")
                draw = ImageDraw.Draw(sheet)
                for row, shot in enumerate(shots[offset:offset + 4]):
                    for col, frame in enumerate(shot["samples"]):
                        if frames_dir:
                            with Image.open(frames_dir / f"{frame}.png") as cached:
                                thumbnail = cached.convert("RGB")
                        else:
                            capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
                            ok, pixels = capture.read()
                            if not ok or abs(capture.get(cv2.CAP_PROP_POS_FRAMES) - (frame + 1)) > 1:
                                raise ValueError(f"Image locale V4 inaccessible : {source.name}, image {frame}.")
                            thumbnail = Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))
                            thumbnail.thumbnail((352, 198))
                        x, y = col * 352, row * 222
                        sheet.paste(thumbnail, (x + (352 - thumbnail.width) // 2, y + 24))
                        draw.text((x + 4, y + 4), f"{shot['shot_id']} | frame {frame}", fill="white")
                output = io.BytesIO()
                sheet.save(output, format="JPEG", quality=85)
                sheets.append("data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode())
        finally:
            if capture:
                capture.release()
        return sheets

    @staticmethod
    def validate_descriptions(value, shots):
        result = ShotDescriptions.model_validate(value)
        if [s.shot_id for s in result.shots] != [s["shot_id"] for s in shots]:
            raise ValueError("L'analyse visuelle doit décrire chaque plan local une seule fois, dans l'ordre.")
        return result

    @classmethod
    def build(cls, source: Path, *, cancel_event=None):
        source = source.resolve()
        with cls.locked(source):
            check_cancelled(cancel_event)
            directory, manifest = cls.manifest(source)
            subtitles = cls.subtitles(source)
            entry = LLMConfigService.facecam_illustrations_entry()
            prompt = cls.PROMPT.read_text()
            key = fingerprint([cls.VERSION, manifest["fingerprint"], subtitles, prompt, entry.model_dump()])
            analysis_dir = directory / key
            budget = IllustrationBudget(analysis_dir, settings.facecam_illustration_episode_budget_usd, entry)
            descriptions = []
            frames_ready = False
            for offset in range(0, len(manifest["shots"]), cls.BATCH_SIZE):
                check_cancelled(cancel_event)
                batch = manifest["shots"][offset:offset + cls.BATCH_SIZE]
                path = analysis_dir / f"batch_{offset:05d}.json"
                if path.exists():
                    result = cls.validate_descriptions(json.loads(path.read_text()), batch)
                else:
                    if not frames_ready:
                        cls.prepare_frames(source, manifest, directory / "frames", cancel_event=cancel_event)
                        frames_ready = True
                    context = {"episode": source.name, "subtitle_language": subtitles["language"],
                               "shots": [{**s, "dialogue": " ".join(c["text"] for c in subtitles["cues"]
                                   if c["start"] < s["end"] + 2 and c["end"] > s["start"] - 2)[:900]}
                                   for s in batch]}
                    images = cls.contact_sheets(source, batch, frames_dir=directory / "frames")
                    check_cancelled(cancel_event)
                    value = budget.call(prompt + "\n" + json.dumps(context, ensure_ascii=False),
                                        ShotDescriptions.model_json_schema(),
                                        images=images)
                    result = cls.validate_descriptions(value, batch)
                    if file_identity(source) != manifest["identity"]:
                        raise ValueError("L'épisode a changé pendant l'analyse visuelle V4.")
                    write_json(path, result.model_dump())
                descriptions.extend(result.shots)
            shots = [{**s, **d.model_dump(), "dialogue": " ".join(c["text"] for c in subtitles["cues"]
                         if c["start"] < s["end"] and c["end"] > s["start"])[:600]}
                     for s, d in zip(manifest["shots"], descriptions)]
            if file_identity(source) != manifest["identity"]:
                raise ValueError("L'épisode a changé pendant la lecture de son index V4.")
            return {**manifest, "fingerprint": key, "subtitle_language": subtitles["language"],
                    "shots": shots, "usage_path": str(budget.path)}
