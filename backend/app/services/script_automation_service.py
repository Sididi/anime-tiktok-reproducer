from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from pydub import AudioSegment

from ..config import settings
from ..models import Project, Transcription
from .elevenlabs_service import ElevenLabsService
from .gemini_service import GeminiService
from .metadata import MetadataService
from .project_service import ProjectService
from .voice_config_service import VoiceConfigService


_LANGUAGE_DISPLAY = {
    "fr": "Français",
    "en": "English",
    "es": "Español",
}

_SCRIPT_AUTOMATION_PROMPT = """# ROLE

Tu es un expert en adaptation de scripts vidéo court format.
Ta mission: réécrire le script en langue cible avec un style narratif oral, percutant, anti-plagiat.

# OBJECTIFS

- Conserver le sens narratif des scènes.
- Garder la première phrase comme hook (fidèle sur le fond, naturelle dans la langue cible).
- Éviter les prénoms de personnages.
- Style conversationnel storytime, pas littéraire.
- Phrases claires et fluides pour TTS.

# CONTRAINTES DE SORTIE (OBLIGATOIRE)

- Retourne UNIQUEMENT un JSON valide.
- Garde exactement le même nombre de scènes.
- Même structure racine: {{"language": "...", "scenes": [...]}}
- Chaque scène doit contenir: scene_index (int) et text (string).
- Aucun markdown, aucun commentaire, aucun texte hors JSON.

# LANGUE CIBLE

{target_language_display} ({target_language_code})

# TITRE CONTEXTE

{anime_name}

# DONNÉES D'ENTRÉE

{input_json}
"""

_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]*\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


class ScriptAutomationService:
    """End-to-end automation for /script: script JSON + optional metadata + TTS chunks."""

    RUNS_DIR_NAME = "script_automation_runs"

    TTS_TARGET = 300
    TTS_MIN = 200
    TTS_MAX = 400

    @classmethod
    def _event(
        cls,
        event: str,
        *,
        status: str = "processing",
        message: str,
        **extra: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": event,
            "status": status,
            "message": message,
            "error": None,
        }
        payload.update(extra)
        return payload

    @classmethod
    def _build_script_prompt(
        cls,
        *,
        project: Project,
        transcription: Transcription,
        target_language: str,
    ) -> str:
        target_language_code = target_language.strip().lower()
        target_language_display = _LANGUAGE_DISPLAY.get(target_language_code, target_language_code)
        anime_name = project.anime_name or "Inconnu"

        scenes_payload = [
            {
                "scene_index": scene.scene_index,
                "text": scene.text,
                "duration_seconds": f"{max(scene.end_time - scene.start_time, 0):.2f}",
                "estimated_word_count": len([token for token in scene.text.split() if token.strip()]),
            }
            for scene in transcription.scenes
        ]

        input_json = json.dumps(
            {
                "language": target_language_code,
                "scenes": scenes_payload,
            },
            ensure_ascii=False,
            indent=2,
        )

        return _SCRIPT_AUTOMATION_PROMPT.format(
            target_language_display=target_language_display,
            target_language_code=target_language_code,
            anime_name=anime_name,
            input_json=input_json,
        )

    @classmethod
    def _normalize_script_payload(
        cls,
        *,
        payload: dict[str, Any],
        transcription: Transcription,
        target_language: str,
    ) -> dict[str, Any]:
        scenes = payload.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError("Script JSON must contain a non-empty 'scenes' array")

        if len(scenes) != len(transcription.scenes):
            raise RuntimeError(
                f"Script scene count mismatch: expected {len(transcription.scenes)}, got {len(scenes)}"
            )

        normalized_scenes: list[dict[str, Any]] = []
        for idx, item in enumerate(scenes):
            if not isinstance(item, dict):
                raise RuntimeError(f"Scene at position {idx} is not an object")

            raw_text = item.get("text")
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise RuntimeError(f"Scene at position {idx} must contain non-empty text")

            expected_scene_index = transcription.scenes[idx].scene_index
            normalized_scenes.append(
                {
                    "scene_index": expected_scene_index,
                    "text": raw_text.strip(),
                }
            )

        return {
            "language": target_language.strip().lower(),
            "scenes": normalized_scenes,
        }

    @classmethod
    def _script_text_from_payload(cls, script_payload: dict[str, Any]) -> str:
        scenes = script_payload.get("scenes")
        if not isinstance(scenes, list):
            return ""
        parts: list[str] = []
        for scene in scenes:
            if isinstance(scene, dict):
                text = scene.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return " ".join(parts).strip()

    @classmethod
    def _script_response_schema(
        cls,
        *,
        target_language: str,
        scene_count: int,
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": [target_language.strip().lower()],
                },
                "scenes": {
                    "type": "array",
                    "minItems": scene_count,
                    "maxItems": scene_count,
                    "items": {
                        "type": "object",
                        "properties": {
                            "scene_index": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                        "required": ["scene_index", "text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["language", "scenes"],
            "additionalProperties": False,
        }

    @classmethod
    def _metadata_response_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "facebook": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    },
                    "required": ["title", "description", "tags"],
                    "additionalProperties": False,
                },
                "instagram": {
                    "type": "object",
                    "properties": {
                        "caption": {"type": "string"},
                    },
                    "required": ["caption"],
                    "additionalProperties": False,
                },
                "youtube": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    },
                    "required": ["title", "description", "tags"],
                    "additionalProperties": False,
                },
                "tiktok": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                    },
                    "required": ["description"],
                    "additionalProperties": False,
                },
            },
            "required": ["facebook", "instagram", "youtube", "tiktok"],
            "additionalProperties": False,
        }

    @classmethod
    def _split_sentences(cls, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return []
        if not _SENTENCE_END_RE.search(cleaned):
            return [cleaned]
        return [segment.strip() for segment in _SENTENCE_SPLIT_RE.split(cleaned) if segment.strip()]

    @classmethod
    def _ensure_sentence_end(cls, text: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return cleaned
        if _SENTENCE_END_RE.search(cleaned):
            return cleaned
        return f"{cleaned}."

    @classmethod
    def _segment_text_for_tts(cls, text: str) -> list[str]:
        sentences = cls._split_sentences(text)
        if not sentences:
            return []

        chunks: list[str] = []
        current = ""
        index = 0

        while index < len(sentences):
            sentence = sentences[index]
            if not current:
                current = sentence
                index += 1
                continue

            candidate = f"{current} {sentence}".strip()
            current_len = len(current)
            candidate_len = len(candidate)

            if candidate_len <= cls.TTS_MAX:
                # Close at the current sentence boundary if we are already in-range
                # and adding one more sentence would move us further from the 300-char target.
                close_now = current_len >= cls.TTS_MIN and abs(current_len - cls.TTS_TARGET) <= abs(candidate_len - cls.TTS_TARGET)
                if close_now:
                    chunks.append(cls._ensure_sentence_end(current))
                    current = sentence
                else:
                    current = candidate
                index += 1
                continue

            # Candidate would exceed soft max.
            if current_len >= cls.TTS_MIN:
                chunks.append(cls._ensure_sentence_end(current))
                current = ""
                continue

            # Current chunk too short (<200): keep extending even above 400 to preserve sentence ending.
            current = candidate
            index += 1

        if current:
            chunks.append(cls._ensure_sentence_end(current))

        if len(chunks) >= 2 and len(chunks[-1]) < cls.TTS_MIN:
            chunks[-2] = cls._ensure_sentence_end(f"{chunks[-2]} {chunks[-1]}")
            chunks.pop()

        return chunks

    @classmethod
    def _output_extension(cls) -> str:
        fmt = (settings.elevenlabs_output_format or "").strip().lower()
        if fmt.startswith("mp3"):
            return "mp3"
        if fmt.startswith("pcm"):
            return "wav"
        if fmt.startswith("ulaw"):
            return "wav"
        return "bin"

    @classmethod
    def _run_dir(cls, project_id: str, run_id: str) -> Path:
        project_dir = ProjectService.get_project_dir(project_id)
        return project_dir / cls.RUNS_DIR_NAME / run_id

    @classmethod
    def _prepare_run_dirs(cls, project_id: str, run_id: str) -> tuple[Path, Path]:
        run_dir = cls._run_dir(project_id, run_id)
        parts_dir = run_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        return run_dir, parts_dir

    @classmethod
    def _merge_parts_to_wav(cls, part_paths: list[Path], output_path: Path) -> None:
        if not part_paths:
            raise RuntimeError("No audio parts to merge")
        combined = AudioSegment.empty()
        for path in part_paths:
            combined += AudioSegment.from_file(str(path))
        combined.export(str(output_path), format="wav")

    @classmethod
    def get_part_path(cls, project_id: str, run_id: str, part_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
            raise FileNotFoundError("Invalid run_id")
        if not re.fullmatch(r"\d+", part_id or ""):
            raise FileNotFoundError("Invalid part_id")

        parts_dir = cls._run_dir(project_id, run_id) / "parts"
        if not parts_dir.exists():
            raise FileNotFoundError("Run not found")

        matches = sorted(parts_dir.glob(f"part_{int(part_id)}.*"))
        if not matches:
            raise FileNotFoundError("Part not found")
        return matches[0]

    @classmethod
    async def stream_automation(
        cls,
        *,
        project_id: str,
        target_language: str,
        voice_key: str,
        include_metadata: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            if not settings.script_automate_enabled:
                raise RuntimeError("Script automation is disabled (ATR_SCRIPT_AUTOMATE_ENABLED=false)")

            project = ProjectService.load(project_id)
            if not project:
                raise RuntimeError("Project not found")

            transcription = ProjectService.load_transcription(project_id)
            if not transcription or not transcription.scenes:
                raise RuntimeError("No transcription found for this project")

            if not GeminiService.is_configured():
                raise RuntimeError("Gemini API key is missing (ATR_GEMINI_API_KEY)")
            if not ElevenLabsService.is_configured():
                raise RuntimeError("ElevenLabs API key is missing (ATR_ELEVENLABS_API_KEY)")

            voice = VoiceConfigService.get_voice(voice_key)

            run_id = uuid.uuid4().hex
            run_dir, parts_dir = cls._prepare_run_dirs(project_id, run_id)

            yield cls._event(
                "starting",
                message="Automation started",
                run_id=run_id,
            )

            yield cls._event("llm_script", message="Generating script JSON with Gemini...")
            prompt = cls._build_script_prompt(
                project=project,
                transcription=transcription,
                target_language=target_language,
            )
            raw_script_payload = await asyncio.to_thread(
                GeminiService.generate_json,
                prompt,
                response_json_schema=cls._script_response_schema(
                    target_language=target_language,
                    scene_count=len(transcription.scenes),
                ),
            )
            script_payload = cls._normalize_script_payload(
                payload=raw_script_payload,
                transcription=transcription,
                target_language=target_language,
            )
            script_path = run_dir / "script.json"
            script_path.write_text(
                json.dumps(script_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            yield cls._event(
                "llm_script",
                message="Script JSON generated",
                script_scene_count=len(script_payload.get("scenes", [])),
            )

            metadata_payload: dict[str, Any] | None = None
            metadata_warning: str | None = None

            if include_metadata:
                yield cls._event("llm_metadata", message="Generating metadata JSON with Gemini...")
                try:
                    metadata_prompt = MetadataService.build_prompt_from_script_json(
                        anime_name=project.anime_name or "Inconnu",
                        script_json=json.dumps(script_payload, ensure_ascii=False),
                        target_language=target_language,
                    )
                    raw_metadata_payload = await asyncio.to_thread(
                        GeminiService.generate_json,
                        metadata_prompt,
                        response_json_schema=cls._metadata_response_schema(),
                    )
                    validated_metadata = MetadataService.validate_payload(raw_metadata_payload)
                    metadata_payload = validated_metadata.model_dump()
                    (run_dir / "metadata.json").write_text(
                        json.dumps(metadata_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    yield cls._event("llm_metadata", message="Metadata JSON generated")
                except Exception as exc:
                    metadata_warning = f"Metadata generation failed: {exc}"
                    yield cls._event(
                        "llm_metadata",
                        message=metadata_warning,
                        warning=metadata_warning,
                    )
            else:
                yield cls._event("llm_metadata", message="Metadata generation skipped")

            full_text = cls._script_text_from_payload(script_payload)
            if not full_text:
                raise RuntimeError("Script JSON contains no text for TTS generation")

            yield cls._event("tts_segmenting", message="Segmenting script for TTS...")
            chunks = cls._segment_text_for_tts(full_text)
            if not chunks:
                raise RuntimeError("Failed to segment script text for TTS")

            yield cls._event(
                "tts_segmenting",
                message=f"Prepared {len(chunks)} TTS segment(s)",
                segment_count=len(chunks),
            )

            extension = cls._output_extension()
            part_paths: list[Path] = []
            parts: list[dict[str, Any]] = []

            for idx, chunk in enumerate(chunks, start=1):
                yield cls._event(
                    "tts_generating",
                    message=f"Generating audio part {idx}/{len(chunks)}...",
                    part_id=str(idx),
                    part_index=idx,
                    part_total=len(chunks),
                    char_count=len(chunk),
                )

                audio_bytes = await asyncio.to_thread(
                    ElevenLabsService.synthesize,
                    voice_id=voice.elevenlabs_voice_id,
                    text=chunk,
                    model_id=settings.elevenlabs_model_id,
                    output_format=settings.elevenlabs_output_format,
                    voice_settings=voice.voice_settings or None,
                )
                part_path = parts_dir / f"part_{idx}.{extension}"
                part_path.write_bytes(audio_bytes)
                part_paths.append(part_path)

                parts.append(
                    {
                        "id": str(idx),
                        "char_count": len(chunk),
                        "download_url": f"/api/projects/{project_id}/script/automate/runs/{run_id}/parts/{idx}",
                    }
                )

            merged_path = run_dir / "merged.wav"
            await asyncio.to_thread(cls._merge_parts_to_wav, part_paths, merged_path)

            complete_payload = cls._event(
                "complete",
                status="complete",
                message="Automation complete",
                run_id=run_id,
                script_json=script_payload,
                metadata_json=metadata_payload,
                metadata_warning=metadata_warning,
                parts=parts,
            )
            yield complete_payload
        except Exception as exc:
            yield {
                "event": "error",
                "status": "error",
                "message": "Script automation failed",
                "error": str(exc),
            }
