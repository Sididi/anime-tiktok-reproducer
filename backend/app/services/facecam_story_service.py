"""Editorial selection and deterministic trims of immutable source references."""
from __future__ import annotations

import hashlib
import json
import math

from pydantic import TypeAdapter

from ..models import MatchList, Transcription
from ..models.facecam import StoryArc


class FacecamStoryService:
    WORDS_PER_SECOND = 3.35  # Initial estimate, not a measurement of the user's voice.
    TARGET_MIN = 65
    TARGET_MAX = 70
    COVER_SECONDS = 1

    @staticmethod
    def fingerprint(reference: Transcription, matches: MatchList) -> str:
        return hashlib.sha256(json.dumps({"reference": reference.model_dump(), "matches": matches.model_dump()},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def bounds(scene, clip) -> tuple[float, float]:
        if (clip.start_word is None) != (clip.end_word is None):
            raise ValueError("Une coupe doit fournir ses deux bornes de mots.")
        if clip.start_word is None:
            return scene.start_time, scene.end_time
        if scene.is_raw:
            raise ValueError("Un extrait audio original retenu doit rester entier.")
        a, b = clip.start_word, clip.end_word
        if not scene.words or not 0 <= a < b <= len(scene.words):
            raise ValueError("Les mots de la coupe ne correspondent pas à la scène source.")
        start = scene.start_time if a == 0 else max(scene.start_time, scene.words[a].start)
        end = scene.end_time if b == len(scene.words) else min(scene.end_time, scene.words[b - 1].end)
        if not math.isfinite(start) or not math.isfinite(end) or end - start < 1 / 60:
            raise ValueError("La coupe interne ne contient pas de durée exploitable.")
        return start, end

    @classmethod
    def validate(cls, value, reference, matches) -> list[StoryArc]:
        if not isinstance(value, dict) or not isinstance(value.get("stories"), list) or not 1 <= len(value["stories"]) <= 6:
            raise ValueError("L'analyse doit identifier au moins une histoire exploitable.")
        stories = TypeAdapter(list[StoryArc]).validate_python(value["stories"])
        scene_map = {s.scene_index: s for s in reference.scenes}
        match_map = {m.scene_index: m for m in matches.matches}
        positions = {s.scene_index: i for i, s in enumerate(reference.scenes)}
        for index, story in enumerate(stories):
            indices = [c.scene_index for c in story.clips]
            if (any(i not in scene_map or i not in match_map for i in indices)
                    or len(indices) != len(set(indices))
                    or indices != sorted(indices, key=lambda i: positions[i])):
                raise ValueError("Les plans d'une histoire doivent exister, être distincts et suivre l'ordre source.")
            if not any(not scene_map[i].is_raw for i in indices):
                raise ValueError("Une histoire doit contenir de la narration.")
            if any(not step.strip() for step in story.steps) or any(not item.strip() for item in story.omitted):
                raise ValueError("Les étapes et les omissions doivent être explicites.")
            story.id = f"story-{index + 1}"
            raw_duration = 0.0
            for clip in story.clips:
                scene, match = scene_map[clip.scene_index], match_map[clip.scene_index]
                if not match.episode:
                    raise ValueError("Validez la correspondance de chaque plan retenu avant de sélectionner une histoire.")
                if scene.duration <= 0 or not all(math.isfinite(v) for v in [scene.start_time, scene.end_time, match.start_time, match.end_time]) or match.end_time <= match.start_time:
                    raise ValueError("Une référence de montage a une durée invalide.")
                cls.bounds(scene, clip)
                if scene.is_raw:
                    # Actual inserted dialogue plays from the episode at its native speed.
                    raw_duration += match.end_time - match.start_time
            story.raw_duration_seconds = raw_duration
            if cls.budget(story)["max_words"] < 40:
                raise ValueError("Les extraits originaux occupent trop de temps pour raconter cette histoire en une minute.")
        return stories

    @classmethod
    def project(cls, reference, matches, story):
        """Retain original scene IDs until approval; trim both coordinate systems."""
        scene_map = {s.scene_index: s for s in reference.scenes}
        match_map = {m.scene_index: m for m in matches.matches}
        scenes, selected_matches, mapping = [], [], []
        for clip in story.clips:
            original = scene_map[clip.scene_index]
            match = match_map[clip.scene_index]
            start, end = cls.bounds(original, clip)
            scale = (match.end_time - match.start_time) / original.duration
            episode_start = match.start_time + (start - original.start_time) * scale
            episode_end = match.start_time + (end - original.start_time) * scale
            words = original.words if clip.start_word is None else original.words[clip.start_word:clip.end_word]
            text = original.text if clip.start_word is None else " ".join(w.text for w in words)
            scenes.append(original.model_copy(deep=True, update={"start_time": start, "end_time": end,
                                                                 "text": text, "words": words}))
            selected_matches.append(match.model_copy(deep=True, update={"start_time": episode_start, "end_time": episode_end,
                "speed_ratio": (end - start) / (episode_end - episode_start)}))
            mapping.append({"source_scene_index": original.scene_index, "source_start": start, "source_end": end,
                            "episode": match.episode, "episode_start": episode_start, "episode_end": episode_end,
                            "start_word": clip.start_word, "end_word": clip.end_word})
        return (Transcription(language=reference.language, scenes=scenes), MatchList(matches=selected_matches), mapping)

    @classmethod
    def budget(cls, story):
        overhead = story.raw_duration_seconds + cls.COVER_SECONDS
        return {"target_seconds": [cls.TARGET_MIN, cls.TARGET_MAX], "estimated_words_per_second": cls.WORDS_PER_SECOND,
                "raw_seconds": story.raw_duration_seconds, "cover_seconds": cls.COVER_SECONDS,
                "min_words": max(0, math.ceil((cls.TARGET_MIN - overhead) * cls.WORDS_PER_SECOND)),
                "max_words": max(0, math.floor((cls.TARGET_MAX - overhead) * cls.WORDS_PER_SECOND))}
