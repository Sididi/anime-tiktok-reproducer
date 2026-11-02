"""Bounded, local-only episode sampling. No model calls or paid fallbacks."""
from __future__ import annotations

import io
import json
import logging
import math
import subprocess
import time
import uuid
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

from .anime_library import AnimeLibraryService
from .atomic_files import write_text_atomic
from .facecam_service import FacecamService, StaleFacecamRevision
from .facecam_visual_index import file_identity, fingerprint
from .project_service import ProjectService

logger = logging.getLogger(__name__)


def portrait(image, x=.5, y=.5, size=(1080, 1920)):
    return ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS, centering=(x, y))


def quality(image):
    """Reject fades and flat frames; favor readable, well-exposed detail."""
    rgb = np.asarray(image.resize((90, 160)).convert("RGB"), dtype=float)
    gray = rgb.mean(axis=2)
    flat = np.mean((gray < 18) | (gray > 242))
    contrast = float(gray.std())
    if flat > .88 or contrast < 12:
        return 0.0
    edges = (np.abs(np.diff(gray, axis=0)).mean() + np.abs(np.diff(gray, axis=1)).mean()) / 2
    saturation = (rgb.max(axis=2) - rgb.min(axis=2)).mean() / 255
    return float(min(edges / 14, 1) * .45 + min(contrast / 65, 1) * .30
                 + saturation * .15 + (1 - flat) * .10)


def best_crop(image):
    # Search the important horizontal/vertical regions at preview resolution.
    horizontal = image.width / image.height >= 9 / 16
    positions = [(i / 10, .5) if horizontal else (.5, i / 10) for i in range(11)]
    return max(positions, key=lambda p: quality(portrait(image, *p, size=(90, 160)))
               - .04 * (abs(p[0] - .5) + abs(p[1] - .5)))


def descriptor(image):
    gray = np.asarray(image.convert("L").resize((9, 8)), dtype=float)
    bits = (gray[:, 1:] > gray[:, :-1]).flatten()
    rgb = np.asarray(image.resize((8, 8)).convert("RGB"), dtype=float) / 255
    return bits, rgb


def similarity(a, b):
    # Color distance keeps different flat-colored anime shots distinguishable.
    return float(.65 * np.mean(a[0] == b[0]) + .35 * (1 - np.abs(a[1] - b[1]).mean()))


class FacecamThumbnailService:
    VERSION = 1
    MAX_SAMPLES = 80
    COUNT = 10

    @staticmethod
    def directory(project_id):
        return FacecamService.directory(project_id) / "thumbnails"

    @staticmethod
    @lru_cache(maxsize=128)
    def _probe(path, size, mtime):
        from .facecam_window_media import FacecamWindowMedia
        return FacecamWindowMedia.probe(Path(path))

    @classmethod
    def context(cls, project_id):
        from .facecam_placement_service import FacecamPlacementService
        state, project = FacecamService.load(project_id), FacecamService.project(project_id)
        matches = (ProjectService.load_matches(project_id) if state.assignment
                   else FacecamService.montage_reference(project_id)[1])
        spans = [{"episode": m.episode, "start": m.start_time, "end": m.end_time}
                 for m in matches.matches if m.episode] if matches else []
        if state.placement and state.placement.status == "complete":
            spans.extend({"episode": c["episode"], "start": c["start"], "end": c["end"]}
                         for c in FacecamPlacementService.candidates(project_id))
        previous = cls._read(cls.directory(project_id) / "sources.json") or {}
        previous_sources = {s["path"]: s for s in previous.get("sources", [])}
        sources, by_path = [], {}
        for span in spans:
            path = AnimeLibraryService.resolve_episode_path(span["episode"], library_type=project.media_library)
            if path is None or not path.is_file():
                raise ValueError(f"Épisode local introuvable : {span['episode']}.")
            path = path.resolve()
            key = str(path)
            if key not in by_path:
                identity = file_identity(path)
                info = cls._probe(*identity)
                old = previous_sources.get(key)
                # Processing may repair audio while stream-copying identical video.
                if old and FacecamPlacementService.same_visual_source({"source": key, "identity": old["identity"]}):
                    identity = old["identity"]
                source = {"id": fingerprint(key)[:16], "path": key, "identity": identity,
                          "label": path.name, "duration": info["duration"], "fps": info["fps"]}
                sources.append(source)
                by_path[key] = source
            span["episode"] = by_path[key]["id"]
        if not sources:
            raise ValueError("Aucun épisode associé au montage n'est disponible.")
        version = fingerprint([cls.VERSION, [(s["id"], s["identity"]) for s in sources], spans])
        return {"version": version, "sources": sources, "spans": spans}

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @classmethod
    def status(cls, project_id):
        context = cls.context(project_id)
        version = context["version"]
        cache = cls.directory(project_id) / version
        manifest = cls._read(cache / "candidates.json") or {}
        candidates = [c for c in manifest.get("candidates", []) if (cache / (c["id"] + ".jpg")).is_file()]
        selected = FacecamService.load(project_id).thumbnail
        if not selected or selected["version"] != version or not cls.selected_path(project_id, selected).is_file():
            selected = None
        return {"version": version, "candidates": candidates,
                "episodes": [{k: s[k] for k in ("id", "label", "duration", "fps")} for s in context["sources"]],
                "selected": selected, "ready": selected is not None,
                "generated": bool(manifest), "warning": manifest.get("warning"), "cost_eur": 0}

    @classmethod
    def checked_context(cls, project_id, version):
        context = cls.context(project_id)
        if context["version"] != version:
            raise StaleFacecamRevision("Les épisodes ou le montage ont changé. Rechargez les miniatures.")
        return context

    @staticmethod
    def source(context, episode):
        result = next((s for s in context["sources"] if s["id"] == episode), None)
        if result is None:
            raise ValueError("Choisissez un épisode associé à ce montage.")
        return result

    @staticmethod
    def timestamp(source, seconds):
        if not math.isfinite(seconds) or seconds < 0 or seconds >= source["duration"]:
            raise ValueError("L'instant choisi est en dehors de l'épisode.")
        return min(round(seconds * source["fps"]) / source["fps"],
                   max(0, source["duration"] - 1 / source["fps"]))

    @staticmethod
    def extract(source, timestamp, width=None):
        from .facecam_media_service import ffmpeg
        args = ["-v", "error", "-ss", f"{timestamp:.6f}", "-i", source["path"],
                "-map", "0:v:0", "-frames:v", "1", "-threads", "1"]
        if width:
            args += ["-vf", f"scale='min({width},iw)':-2"]
        result = ffmpeg([*args, "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "pipe:1"], timeout=30)
        if not result.stdout:
            raise ValueError("Cette image ne peut pas être extraite. Choisissez un autre instant.")
        with Image.open(io.BytesIO(result.stdout)) as image:
            return image.convert("RGB")

    @staticmethod
    def save_jpeg(image, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
        try:
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            for level in (92, 85, 75, 65):
                image.save(tmp, format="JPEG", quality=level, optimize=True, icc_profile=profile)
                if tmp.stat().st_size < 2_000_000:
                    break
            if tmp.stat().st_size >= 2_000_000:
                raise ValueError("La miniature dépasse la taille maximale de 2 Mo.")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    @classmethod
    def sample_points(cls, context):
        points = {}
        sources = {s["id"]: s for s in context["sources"]}
        # Keep a bounded spread of montage shots, followed by other episode scenes.
        spans = context["spans"]
        indices = np.linspace(0, len(spans) - 1, min(12, len(spans)), dtype=int)
        for index in indices:
            span = spans[index]
            source = sources[span["episode"]]
            start, end = max(0, span["start"]), min(source["duration"], span["end"])
            if end <= start:
                continue
            for fraction in (.25, .5, .75):
                timestamp = cls.timestamp(source, start + (end - start) * fraction)
                points[(source["id"], timestamp)] = True
        remaining = cls.MAX_SAMPLES - len(points)
        # Round robin across episodes, skipping the usual opening/ending regions.
        for i in range(remaining):
            source = context["sources"][i % len(sources)]
            per_episode = max(1, math.ceil(remaining / len(sources)))
            fraction = .10 + .78 * ((i // len(sources) + .5) / per_episode)
            timestamp = cls.timestamp(source, source["duration"] * fraction)
            points.setdefault((source["id"], timestamp), False)
        return [(episode, timestamp, relevant) for (episode, timestamp), relevant in points.items()]

    @classmethod
    def generate(cls, project_id):
        context = cls.context(project_id)
        cache = cls.directory(project_id) / context["version"]
        cache.mkdir(parents=True, exist_ok=True)
        if (cache / "candidates.json").is_file() and cls.status(project_id)["candidates"]:
            return  # Retry/reload costs no additional decode when already complete.
        pool = []
        points = cls.sample_points(context)
        failures = 0
        deadline = time.monotonic() + 60
        for i, (episode, timestamp, relevant) in enumerate(points):
            if time.monotonic() > deadline:
                failures += 1
                break
            source = cls.source(context, episode)
            try:
                image = cls.extract(source, timestamp, width=640)
                x, y = best_crop(image)
                cropped = portrait(image, x, y, size=(180, 320))
                score = quality(cropped)
                if score < .2:
                    continue
                candidate = {"id": fingerprint([episode, timestamp])[:24], "episode": episode,
                             "label": source["label"], "timestamp": timestamp, "crop_x": x, "crop_y": y}
                pool.append((score + (.10 if relevant else 0), candidate, descriptor(cropped), cropped))
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
                failures += 1
                logger.warning("Facecam thumbnail sample failed: %s", exc)
            if i % 10 == 0:
                FacecamService.update(project_id, {"job": {"status": "processing", "step": "Miniatures",
                    "message": f"Analyse des images : {i + 1}/{len(points)}", "progress": (i + 1) / len(points)}})
        chosen = []
        while pool and len(chosen) < cls.COUNT:
            def rank(item):
                redundancy = max((similarity(item[2], previous[2]) for previous in chosen), default=0)
                return item[0] - .45 * redundancy
            best = pool.pop(max(range(len(pool)), key=lambda index: rank(pool[index])))
            if any(similarity(best[2], c[2]) > .96 or (best[1]["episode"] == c[1]["episode"]
                    and abs(best[1]["timestamp"] - c[1]["timestamp"]) < .8) for c in chosen):
                continue
            chosen.append(best)
        if not chosen:
            raise ValueError("Aucune image exploitable trouvée. Utilisez le sélecteur manuel ou réessayez.")
        with FacecamService.lock(project_id):
            cls.checked_context(project_id, context["version"])
            for _, candidate, _, image in chosen:
                cls.save_jpeg(image, cache / (candidate["id"] + ".jpg"))
            write_text_atomic(cache / "candidates.json", json.dumps({"candidates": [c[1] for c in chosen],
                "warning": "Certaines images n'ont pas pu être extraites." if failures else None}))
            write_text_atomic(cls.directory(project_id) / "sources.json", json.dumps(context))

    @classmethod
    def frame(cls, project_id, version, episode, timestamp):
        context = cls.checked_context(project_id, version)
        source = cls.source(context, episode)
        timestamp = cls.timestamp(source, timestamp)
        image = cls.extract(source, timestamp, width=1280)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()

    @classmethod
    def select(cls, project_id, version, episode, timestamp, crop_x=.5, crop_y=.5):
        context = cls.checked_context(project_id, version)
        source = cls.source(context, episode)
        timestamp = cls.timestamp(source, timestamp)
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (crop_x, crop_y)):
            raise ValueError("Recadrage invalide.")
        image = portrait(cls.extract(source, timestamp), crop_x, crop_y)
        selection = {"id": uuid.uuid4().hex, "version": version, "episode": episode,
                     "label": source["label"], "timestamp": timestamp, "crop_x": crop_x, "crop_y": crop_y}
        with FacecamService.lock(project_id):
            if project_id in FacecamService._processing:
                raise ValueError("Attendez la fin du traitement Facecam.")
            cls.checked_context(project_id, version)
            cls.save_jpeg(image, cls.selected_path(project_id, selection))
            FacecamService.update(project_id, {"thumbnail": selection})
            write_text_atomic(cls.directory(project_id) / "sources.json", json.dumps(context))
            from .facecam_publication_service import FacecamPublicationService
            FacecamPublicationService.invalidate_bundle(project_id)
        return cls.status(project_id)

    @classmethod
    def selected_path(cls, project_id, selection):
        return cls.directory(project_id) / "selected" / f"{selection['id']}.jpg"

    @classmethod
    def require_selected(cls, project_id):
        selected = cls.status(project_id)["selected"]
        if selected is None:
            raise ValueError("Choisissez une miniature avant le traitement.")
        path = cls.selected_path(project_id, selected)
        with Image.open(path) as image:
            if image.size != (1080, 1920) or image.format != "JPEG":
                raise ValueError("Miniature invalide. Sélectionnez à nouveau une image.")
            image.verify()
        return path
