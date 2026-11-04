"""A bounded, shared visual overview; sampled frames are clues, not shot boundaries."""
from __future__ import annotations

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..config import settings
from .facecam_illustration_llm import write_json
from .facecam_visual_index import FacecamVisualIndex, file_identity, fingerprint


class FrameObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    sample_id: str
    visual: str = Field(min_length=1, max_length=320)
    usable: StrictBool


class FrameObservations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[FrameObservation]


class FacecamEpisodeOverview:
    VERSION = 1
    MAX_FRAMES = 96
    FRAMES_PER_EPISODE = 48
    BATCH_SIZE = 48
    PROMPT = Path(__file__).parents[2] / "prompts/facecam/episode_overview.md"

    @classmethod
    def samples(cls, infos):
        """Share one frame allowance across every episode; include their full time range."""
        counts = [min(cls.FRAMES_PER_EPISODE, max(1, int(info["duration"] // 12))) for info in infos]
        quota = [0] * len(infos)
        for _ in range(cls.MAX_FRAMES):
            eligible = [i for i, count in enumerate(counts) if quota[i] < count]
            if not eligible:
                break
            i = min(eligible, key=lambda index: (quota[index], index))
            quota[i] += 1
        samples = []
        for episode, (info, count, allowance) in enumerate(zip(infos, counts, quota)):
            # Stable per-episode grid makes a second project reuse the same observations.
            total_windows = max(1, int(info["duration"] // 12))
            grid = sorted({round(i * (total_windows - 1) / max(1, count - 1)) for i in range(count)})
            selected = sorted({round(i * (len(grid) - 1) / max(1, allowance - 1)) for i in range(allowance)})
            source_key = fingerprint([cls.VERSION, info["identity"]])
            directory = settings.cache_dir / "facecam_episode_overviews" / source_key
            for index in selected:
                window = grid[index]
                frame = min(info["frame_count"] - 1, round((window * 12 + min(6, info["duration"] / 2)) * info["fps"]))
                samples.append({"sample_id": f"{source_key[:12]}f{frame}", "episode_index": episode,
                    "frame": frame, "window_id": f"e{episode}w{window}", "seconds": frame / info["fps"],
                    "directory": str(directory), "image": str(directory / f"{frame}.png")})
        return samples

    @staticmethod
    def extract(info, samples, check):
        import av
        from PIL import Image

        source = Path(info["source"])
        with FacecamVisualIndex.locked("overview-frames:" + info["source"], check=check):
            check()
            if file_identity(source) != info["identity"]:
                raise ValueError("Un épisode a changé pendant la préparation de son aperçu visuel.")
            missing = [s for s in samples if not Path(s["image"]).is_file()]
            if not missing:
                return
            with av.open(str(source)) as container:
                stream = container.streams.video[0]
                stream.codec_context.thread_count = 2
                stream.thread_type = "SLICE"
                origin = (stream.start_time or 0) * stream.time_base
                for sample in missing:
                    check()
                    container.seek(int((sample["seconds"] + float(origin)) / stream.time_base), stream=stream, backward=True)
                    for frame in container.decode(stream):
                        check()
                        if frame.pts is None:
                            raise ValueError("Image d'aperçu sans horodatage local.")
                        position = float(frame.pts * frame.time_base - origin) * info["fps"]
                        index = round(position)
                        if index < sample["frame"]:
                            continue
                        if index != sample["frame"] or abs(index - position) > .1:
                            raise ValueError("L'image d'aperçu ne correspond pas à la cadence du fichier local.")
                        scale = min(352 / frame.width, 198 / frame.height)
                        pixels = frame.reformat(width=max(1, round(frame.width * scale)),
                                                height=max(1, round(frame.height * scale)), format="rgb24").to_ndarray()
                        path = Path(sample["image"])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_suffix(".png.tmp")
                        Image.fromarray(pixels).save(temporary, format="PNG")
                        temporary.replace(path)
                        break
                    else:
                        raise ValueError("Impossible de lire une image de l'aperçu de l'épisode.")
            if file_identity(source) != info["identity"]:
                raise ValueError("Un épisode a changé pendant l'extraction de son aperçu visuel.")

    @staticmethod
    def sheets(samples):
        from PIL import Image, ImageDraw

        images = []
        for offset in range(0, len(samples), 8):
            sheet = Image.new("RGB", (704, 888), "#111111")
            draw = ImageDraw.Draw(sheet)
            for i, sample in enumerate(samples[offset:offset + 8]):
                x, y = i % 2 * 352, i // 2 * 222
                with Image.open(sample["image"]) as source:
                    thumbnail = source.convert("RGB")
                sheet.paste(thumbnail, (x + (352 - thumbnail.width) // 2, y + 24))
                draw.text((x + 4, y + 4), sample["sample_id"], fill="white")
            output = io.BytesIO()
            sheet.save(output, format="JPEG", quality=88)
            images.append("data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode())
        return images

    @classmethod
    def describe(cls, infos, subtitles, client, check):
        samples = cls.samples(infos)
        prompt = cls.PROMPT.read_text()
        model = settings.facecam_antigravity_overview_model
        model_key = fingerprint([cls.VERSION, prompt, model, FrameObservations.model_json_schema()])
        source_keys = [fingerprint([model_key, subs]) for subs in subtitles]
        paths = {s["sample_id"]: Path(s["directory"]) / source_keys[s["episode_index"]] / f"{s['frame']}.json" for s in samples}
        # Serialize shared descriptions across projects. Waiting consumes the same deadline.
        with FacecamVisualIndex.locked("antigravity:episode-overviews", check=check):
            check()
            missing = [s for s in samples if not paths[s["sample_id"]].is_file()]
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda i: cls.extract(infos[i], [s for s in missing if s["episode_index"] == i], check),
                              sorted({s["episode_index"] for s in missing})))

            def analyze(offset):
                check()
                batch = missing[offset:offset + cls.BATCH_SIZE]
                data = {"samples": [{"sample_id": s["sample_id"],
                    "episode": Path(infos[s["episode_index"]]["source"]).name,
                    "dialogue": " ".join(c["text"] for c in subtitles[s["episode_index"]]["cues"]
                        if c["start"] < s["seconds"] + 12 and c["end"] > s["seconds"] - 12)[:500]}
                    for s in batch]}
                value = client.call(f"overview-{offset:03d}", prompt + "\n" + json.dumps(data, ensure_ascii=False),
                                    FrameObservations.model_json_schema(), model=model, images=cls.sheets(batch))
                result = FrameObservations.model_validate(value)
                if [o.sample_id for o in result.observations] != [s["sample_id"] for s in batch]:
                    raise ValueError("L'aperçu doit décrire chaque image fournie une seule fois et dans l'ordre.")
                check()
                for i in {s["episode_index"] for s in batch}:
                    if file_identity(Path(infos[i]["source"])) != infos[i]["identity"]:
                        raise ValueError("Un épisode a changé pendant son analyse visuelle.")
                for observation in result.observations:
                    write_json(paths[observation.sample_id], observation.model_dump())

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(analyze, range(0, len(missing), cls.BATCH_SIZE)))
            observations = []
            for sample in samples:
                value = FrameObservation.model_validate_json(paths[sample["sample_id"]].read_text())
                if value.sample_id != sample["sample_id"]:
                    raise ValueError("Une observation visuelle ne correspond plus à son image locale.")
                observations.append({"window_id": sample["window_id"], "visual": value.visual, "usable": value.usable})
            return observations, {"sampled_frames": len(samples), "new_observations": len(missing),
                                   "cached_observations": len(samples) - len(missing)}
