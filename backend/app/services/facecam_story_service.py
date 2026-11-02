"""Editorial selection and deterministic trims of immutable source references."""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..models import MatchList, Transcription
from ..models.facecam import StoryArc, StoryClip, default_ending


logger = logging.getLogger(__name__)


class FacecamStoryService:
    FULL_STORY_ID = "full-story"
    WORDS_PER_SECOND = 3.35  # Initial estimate, not a measurement of the user's voice.
    TARGET_MIN = 65
    TARGET_MAX = 70
    COVER_SECONDS = 0  # The reveal now overlaps the spoken ending.
    MAX_ATTEMPTS = 3

    @staticmethod
    def response_schema() -> dict:
        """The model proposes editorial choices; IDs and durations belong to us."""
        text = {"type": "string", "minLength": 1}
        word_bound = {"type": ["integer", "null"], "minimum": 0}
        return {
            "type": "object", "additionalProperties": False, "required": ["stories"],
            "properties": {"stories": {
                "type": "array", "minItems": 1, "maxItems": 6,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["title", "premise", "ending", "steps", "omitted", "clips"],
                    "properties": {
                        "title": text, "premise": text, "ending": text,
                        "steps": {"type": "array", "items": text, "minItems": 1, "maxItems": 6},
                        "omitted": {"type": "array", "items": text},
                        "clips": {
                            "type": "array", "minItems": 1,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["scene_index", "start_word", "end_word"],
                                "properties": {
                                    "scene_index": {"type": "integer"},
                                    "start_word": word_bound, "end_word": word_bound,
                                },
                            },
                        },
                    },
                },
            }},
        }

    @classmethod
    def analyze(cls, reference: Transcription, matches: MatchList, *, anime: str | None,
                generate: Callable[..., Any], context: str) -> list[StoryArc]:
        """Turn fallible model proposals into a complete, validated montage selection."""
        from .facecam_narration_service import FacecamNarrationService

        match_map = {m.scene_index: m for m in matches.matches}
        payload = {"anime": anime, "target_seconds": [cls.TARGET_MIN, cls.TARGET_MAX], "scenes": [
            {"scene_index": s.scene_index, "text": s.text, "raw": s.is_raw,
             "source_duration": s.duration,
             "raw_duration": (match_map[s.scene_index].end_time - match_map[s.scene_index].start_time
                              if s.is_raw and s.scene_index in match_map else 0),
             "word_count": len(s.words), "can_trim": bool(s.words) and not s.is_raw,
             "words": [{"index": i, "text": w.text} for i, w in enumerate(s.words)]}
            for s in reference.scenes]}
        instructions = FacecamNarrationService.prompt("stories")
        for attempt in range(1, cls.MAX_ATTEMPTS + 1):
            # Provider failures are not editorial validation failures: let them propagate.
            value = generate(instructions + "\n" + json.dumps(payload, ensure_ascii=False),
                             response_schema=cls.response_schema(), schema_name="facecam_stories")
            try:
                return cls.validate(value, reference, matches)
            except ValueError as exc:
                if isinstance(exc, ValidationError):
                    diagnostic = "; ".join(
                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                        for e in exc.errors(include_input=False, include_url=False)[:5]
                    )
                else:
                    diagnostic = str(exc)
                logger.warning("Facecam stories rejected (%s, attempt %d/%d): %s",
                               context, attempt, cls.MAX_ATTEMPTS, diagnostic)
                if attempt == cls.MAX_ATTEMPTS:
                    raise ValueError(
                        f"L'analyse des histoires reste invalide après {cls.MAX_ATTEMPTS} tentatives. "
                        f"{diagnostic} Le projet est conservé. Relancez l'analyse."
                    ) from exc
                payload["correction"] = {
                    "previous_response": value, "error": diagnostic,
                    "instruction": "Corrige les erreurs indiquées sans changer les choix éditoriaux valides. "
                                   "Retourne toutes les histoires corrigées, pas seulement le plan en erreur.",
                }
        raise AssertionError("Unreachable story analysis attempt")

    @classmethod
    def full_story(cls, reference, matches, anime_title: str | None = None) -> StoryArc:
        """An application-owned option, available independently of model analysis."""
        raw_indices = {s.scene_index for s in reference.scenes if s.is_raw}
        raw_duration = sum(m.end_time - m.start_time for m in matches.matches if m.scene_index in raw_indices)
        words = sum(len(s.text.split()) for s in reference.scenes if not s.is_raw) + len(default_ending(anime_title).split())
        return StoryArc(
            id=cls.FULL_STORY_ID, title="Garder toute l’histoire",
            premise="Conserver toutes les étapes du récit source et les extraits audio originaux.",
            ending="Aller jusqu’à la fin du récit source, sans écarter de branche narrative.",
            steps=["Reprendre l’ensemble des événements dans leur ordre, sans coupe narrative."],
            clips=[StoryClip(scene_index=s.scene_index) for s in reference.scenes],
            raw_duration_seconds=raw_duration,
            estimated_duration_seconds=round(words / cls.WORDS_PER_SECOND + raw_duration + cls.COVER_SECONDS, 2),
        )

    @staticmethod
    def fingerprint(reference: Transcription, matches: MatchList) -> str:
        return hashlib.sha256(json.dumps({"reference": reference.model_dump(), "matches": matches.model_dump()},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def normalize_clip(scene, clip: StoryClip) -> StoryClip:
        """Resolve open source-word bounds once, before slicing text or mapping time.

        null means the corresponding scene edge. Persist either a whole scene
        (null/null) or an explicit half-open word range, never a one-sided range.
        Invalid ranges are rejected, not clamped or replaced with whole scenes.
        """
        if clip.start_word is None and clip.end_word is None:
            return clip
        if scene.is_raw:
            raise ValueError("Un extrait audio original retenu doit rester entier.")
        a = 0 if clip.start_word is None else clip.start_word
        b = len(scene.words) if clip.end_word is None else clip.end_word
        if not scene.words or not 0 <= a < b <= len(scene.words):
            raise ValueError(
                f"La coupe [{a}, {b}[ doit respecter 0 <= start_word < end_word <= {len(scene.words)} "
                "(nombre de mots du plan source). Sans mots horodatés, conservez le plan entier."
            )
        if a == 0 and b == len(scene.words):
            return clip.model_copy(update={"start_word": None, "end_word": None})
        return clip.model_copy(update={"start_word": a, "end_word": b})

    @classmethod
    def bounds(cls, scene, clip) -> tuple[float, float]:
        clip = cls.normalize_clip(scene, clip)
        if clip.start_word is None:
            return scene.start_time, scene.end_time
        a, b = clip.start_word, clip.end_word
        start = scene.start_time if a == 0 else max(scene.start_time, scene.words[a].start)
        end = scene.end_time if b == len(scene.words) else min(scene.end_time, scene.words[b - 1].end)
        if not math.isfinite(start) or not math.isfinite(end) or end - start < 1 / 60:
            raise ValueError("La coupe interne ne contient pas de durée exploitable.")
        return start, end

    @classmethod
    def validate(cls, value, reference, matches, *, enforce_budget=True) -> list[StoryArc]:
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
            story.estimated_duration_seconds = None
            raw_duration = 0.0
            for clip_index, clip in enumerate(story.clips):
                scene, match = scene_map[clip.scene_index], match_map[clip.scene_index]
                if not match.episode:
                    raise ValueError("Validez la correspondance de chaque plan retenu avant de sélectionner une histoire.")
                if scene.duration <= 0 or not all(math.isfinite(v) for v in [scene.start_time, scene.end_time, match.start_time, match.end_time]) or match.end_time <= match.start_time:
                    raise ValueError("Une référence de montage a une durée invalide.")
                try:
                    clip = cls.normalize_clip(scene, clip)
                    cls.bounds(scene, clip)
                except ValueError as exc:
                    raise ValueError(f"Histoire {index + 1} (« {story.title} »), plan {scene.scene_index} : {exc}") from exc
                story.clips[clip_index] = clip
                if scene.is_raw:
                    # Actual inserted dialogue plays from the episode at its native speed.
                    raw_duration += match.end_time - match.start_time
            story.raw_duration_seconds = raw_duration
            if enforce_budget and cls.budget(story)["max_words"] < 40:
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
            clip = cls.normalize_clip(original, clip)
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
        if story.id == cls.FULL_STORY_ID:
            return None
        overhead = story.raw_duration_seconds + cls.COVER_SECONDS
        return {"target_seconds": [cls.TARGET_MIN, cls.TARGET_MAX], "estimated_words_per_second": cls.WORDS_PER_SECOND,
                "raw_seconds": story.raw_duration_seconds, "cover_seconds": cls.COVER_SECONDS,
                "min_words": max(0, math.ceil((cls.TARGET_MIN - overhead) * cls.WORDS_PER_SECOND)),
                "max_words": max(0, math.floor((cls.TARGET_MAX - overhead) * cls.WORDS_PER_SECOND))}
