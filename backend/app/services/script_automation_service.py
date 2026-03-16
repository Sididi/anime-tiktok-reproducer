from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
import wave
from pathlib import Path
from typing import Any, AsyncIterator

from pydub import AudioSegment

from ..config import settings
from ..models import Project, Transcription
from .elevenlabs_service import ElevenLabsService
from .gemini_service import GeminiService
from .metadata import MetadataService
from .project_service import ProjectService
from .script_payload_service import ScriptPayloadService
from .script_phase_prompt_service import ScriptPhasePromptService
from .tts_text_normalizer import TtsTextNormalizer
from .voice_config_service import VoiceConfigService

_OVERLAY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title_hooks": {
            "type": "array",
            "minItems": 10,
            "maxItems": 10,
            "items": {"type": "string"},
        },
        "category": {"type": "string"},
    },
    "required": ["title_hooks", "category"],
    "additionalProperties": False,
}

_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]*\s*$")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?…][\"')\]]*(?:\s+|$)")


class ScriptAutomationService:
    """End-to-end automation for /script: script JSON + optional metadata + TTS chunks."""

    RUNS_DIR_NAME = "script_automation_runs"
    MAX_OVERLAY_TITLE_CHARS = 45

    TTS_TARGET = 625
    TTS_MIN = 500
    TTS_SOFT_MAX = 750
    TTS_HARD_MAX = 800

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
        return ScriptPhasePromptService.build_script_prompt(
            project=project,
            transcription=transcription,
            target_language=target_language,
        )

    @classmethod
    def _normalize_script_payload(
        cls,
        *,
        payload: dict[str, Any],
        transcription: Transcription,
        target_language: str,
    ) -> dict[str, Any]:
        normalized = ScriptPayloadService.normalize(
            payload=payload,
            transcription=transcription,
            target_language=target_language,
        )
        return normalized.public_payload

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
    def _resolve_tts_language(
        cls,
        *,
        script_payload: dict[str, Any],
        target_language: str | None,
    ) -> str:
        return ScriptPayloadService.resolve_language(
            payload=script_payload,
            target_language=target_language,
        )

    @classmethod
    def _truncate_overlay_title(cls, title: str) -> str:
        cleaned = title.strip()
        if len(cleaned) <= cls.MAX_OVERLAY_TITLE_CHARS:
            return cleaned

        truncated = cleaned[: cls.MAX_OVERLAY_TITLE_CHARS]
        last_space = truncated.rfind(" ")
        if last_space > cls.MAX_OVERLAY_TITLE_CHARS // 2:
            return truncated[:last_space].rstrip()
        return truncated.rstrip()

    @classmethod
    def _normalize_overlay_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        raw_hooks = payload.get("title_hooks")
        if not isinstance(raw_hooks, list):
            raise RuntimeError("Overlay JSON must contain a 'title_hooks' array")

        title_hooks = [
            cls._truncate_overlay_title(item)
            for item in raw_hooks
            if isinstance(item, str) and item.strip()
        ]
        if len(title_hooks) != 10:
            raise RuntimeError(
                f"Overlay JSON must contain exactly 10 non-empty title hooks ({len(title_hooks)} received)"
            )

        category = str(payload.get("category", "")).strip()
        if not category:
            raise RuntimeError("Overlay JSON must contain a non-empty 'category'")

        return {
            "title": title_hooks[0],
            "title_hooks": title_hooks,
            "category": category,
        }

    @classmethod
    def prepare_tts_payload(
        cls,
        *,
        script_payload: dict[str, Any],
        target_language: str | None = None,
    ) -> dict[str, Any]:
        scenes = script_payload.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError("Script JSON must contain a non-empty 'scenes' array")

        language = cls._resolve_tts_language(script_payload=script_payload, target_language=target_language)
        normalized_scenes: list[dict[str, Any]] = []
        for idx, item in enumerate(scenes):
            if not isinstance(item, dict):
                raise RuntimeError(f"Scene at position {idx} is not an object")

            raw_text = item.get("text")
            if not isinstance(raw_text, str):
                raise RuntimeError(f"Scene at position {idx} must contain a 'text' string")

            scene_index_raw = item.get("scene_index")
            scene_index = scene_index_raw if isinstance(scene_index_raw, int) else idx + 1
            normalized_text = TtsTextNormalizer.normalize_text(raw_text, language=language).strip()
            if not normalized_text:
                continue
            normalized_scenes.append(
                {
                    "scene_index": scene_index,
                    "text": normalized_text,
                }
            )

        normalized_payload = {
            "language": language,
            "scenes": normalized_scenes,
        }
        segments = cls._segment_scenes_for_tts_payload(normalized_payload)
        normalized_full_text = " ".join(scene["text"] for scene in normalized_scenes if scene["text"]).strip()
        return {
            "language": language,
            "normalized_full_text": normalized_full_text,
            "segments": segments,
        }

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
    def _ensure_sentence_end(cls, text: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return cleaned
        if _SENTENCE_END_RE.search(cleaned):
            return cleaned
        # Never add punctuation if it would violate hard cap.
        if len(cleaned) >= cls.TTS_HARD_MAX:
            return cleaned
        return f"{cleaned}."

    @classmethod
    def _split_at_preferred_tts_boundary(cls, text: str, cap: int) -> tuple[str, str]:
        """Split text at cap, preferring a sentence boundary before whitespace fallback."""
        if len(text) <= cap:
            return text, ""

        last_sentence_boundary: int | None = None
        for match in _SENTENCE_BOUNDARY_RE.finditer(text[:cap]):
            last_sentence_boundary = match.end()

        if last_sentence_boundary is not None:
            head = text[:last_sentence_boundary].rstrip()
            tail = text[last_sentence_boundary:].lstrip()
            if head:
                return head, tail

        split_index = text.rfind(" ", 0, cap)
        if split_index > 0:
            head = text[:split_index].rstrip()
            tail = text[split_index + 1:].lstrip()
            if head:
                return head, tail

        return text[:cap], text[cap:]

    @classmethod
    def _coalesce_scene_fragments(
        cls,
        scene_fragments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        coalesced: list[dict[str, Any]] = []
        for raw_fragment in scene_fragments:
            if not isinstance(raw_fragment, dict):
                continue
            scene_index = raw_fragment.get("scene_index")
            if not isinstance(scene_index, int):
                continue
            fragment_text = str(raw_fragment.get("text") or "")
            if not fragment_text:
                continue
            if (
                coalesced
                and int(coalesced[-1]["scene_index"]) == scene_index
            ):
                coalesced[-1]["text"] = str(coalesced[-1]["text"]) + fragment_text
                continue
            coalesced.append(
                {
                    "scene_index": scene_index,
                    "text": fragment_text,
                }
            )
        return coalesced

    @classmethod
    def _build_segment_from_fragments(
        cls,
        *,
        segment_id: int,
        scene_fragments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        coalesced = cls._coalesce_scene_fragments(scene_fragments)
        segment_text = "".join(str(fragment.get("text") or "") for fragment in coalesced).strip()
        scene_indices: list[int] = []
        seen_scene_indices: set[int] = set()
        normalized_fragments: list[dict[str, Any]] = []
        for fragment in coalesced:
            scene_index = int(fragment["scene_index"])
            fragment_text = str(fragment["text"])
            normalized_fragments.append(
                {
                    "scene_index": scene_index,
                    "text": fragment_text,
                }
            )
            if scene_index not in seen_scene_indices:
                seen_scene_indices.add(scene_index)
                scene_indices.append(scene_index)
        return {
            "id": segment_id,
            "scene_indices": scene_indices,
            "scene_fragments": normalized_fragments,
            "text": segment_text,
            "character_count": len(segment_text),
        }

    @classmethod
    def _split_scene_fragments_for_segment_text(
        cls,
        *,
        scene_fragments: list[dict[str, Any]],
        head_text: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raw_text = "".join(str(fragment.get("text") or "") for fragment in scene_fragments)
        if not head_text:
            return [], cls._coalesce_scene_fragments(scene_fragments)

        split_pos = len(head_text)
        if split_pos > len(raw_text):
            raise ValueError("Hard split text exceeds segment text length")

        while split_pos < len(raw_text) and raw_text[split_pos].isspace():
            split_pos += 1

        head_fragments: list[dict[str, Any]] = []
        tail_fragments: list[dict[str, Any]] = []
        remaining_head_chars = split_pos

        for raw_fragment in scene_fragments:
            if not isinstance(raw_fragment, dict):
                continue
            scene_index = raw_fragment.get("scene_index")
            if not isinstance(scene_index, int):
                continue
            fragment_text = str(raw_fragment.get("text") or "")
            if not fragment_text:
                continue

            if remaining_head_chars <= 0:
                tail_fragments.append(
                    {
                        "scene_index": scene_index,
                        "text": fragment_text,
                    }
                )
                continue

            if remaining_head_chars >= len(fragment_text):
                head_fragments.append(
                    {
                        "scene_index": scene_index,
                        "text": fragment_text,
                    }
                )
                remaining_head_chars -= len(fragment_text)
                continue

            head_part = fragment_text[:remaining_head_chars]
            tail_part = fragment_text[remaining_head_chars:]
            if head_part:
                head_fragments.append(
                    {
                        "scene_index": scene_index,
                        "text": head_part,
                    }
                )
            if tail_part:
                tail_fragments.append(
                    {
                        "scene_index": scene_index,
                        "text": tail_part,
                    }
                )
            remaining_head_chars = 0

        return (
            cls._coalesce_scene_fragments(head_fragments),
            cls._coalesce_scene_fragments(tail_fragments),
        )

    @classmethod
    def _segment_scenes_for_tts_payload(cls, script_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Segment script scenes for TTS and keep scene index mapping."""
        scenes = script_payload.get("scenes", [])
        if not isinstance(scenes, list) or not scenes:
            return []

        def _create_empty_segment(segment_id: int) -> dict[str, Any]:
            return cls._build_segment_from_fragments(segment_id=segment_id, scene_fragments=[])

        segments: list[dict[str, Any]] = []
        current_segment = _create_empty_segment(1)

        for i, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            scene_text = str(scene.get("text") or "").strip()
            if not scene_text:
                continue

            scene_index_raw = scene.get("scene_index")
            scene_index = scene_index_raw if isinstance(scene_index_raw, int) else i + 1
            new_fragment_text = scene_text
            if current_segment.get("scene_fragments"):
                new_fragment_text = f" {scene_text}"
            current_segment = cls._build_segment_from_fragments(
                segment_id=int(current_segment["id"]),
                scene_fragments=[
                    *list(current_segment.get("scene_fragments") or []),
                    {
                        "scene_index": scene_index,
                        "text": new_fragment_text,
                    },
                ],
            )

            while int(current_segment["character_count"]) > cls.TTS_HARD_MAX:
                hard_chunk, remainder = cls._split_at_preferred_tts_boundary(
                    str(current_segment["text"]),
                    cls.TTS_HARD_MAX,
                )
                if not hard_chunk:
                    break

                head_fragments, tail_fragments = cls._split_scene_fragments_for_segment_text(
                    scene_fragments=list(current_segment.get("scene_fragments") or []),
                    head_text=hard_chunk,
                )
                head_segment = cls._build_segment_from_fragments(
                    segment_id=len(segments) + 1,
                    scene_fragments=head_fragments,
                )
                if not head_segment["text"]:
                    break

                segments.append(head_segment)
                current_segment = cls._build_segment_from_fragments(
                    segment_id=len(segments) + 1,
                    scene_fragments=tail_fragments,
                )
                if remainder and current_segment["text"] != remainder:
                    current_segment = cls._build_segment_from_fragments(
                        segment_id=len(segments) + 1,
                        scene_fragments=[
                            {
                                "scene_index": int(fragment["scene_index"]),
                                "text": str(fragment["text"]),
                            }
                            for fragment in tail_fragments
                        ],
                    )

            if not current_segment["text"]:
                continue

            current_len = int(current_segment["character_count"])

            is_last = i == len(scenes) - 1
            ends_sentence = bool(_SENTENCE_END_RE.search(str(current_segment["text"])))

            if not ends_sentence and not is_last:
                continue

            if is_last:
                normalized = cls._ensure_sentence_end(str(current_segment["text"]))
                segments.append(
                    cls._build_segment_from_fragments(
                        segment_id=int(current_segment["id"]),
                        scene_fragments=[
                            *list(current_segment.get("scene_fragments") or []),
                        ],
                    )
                )
                segments[-1]["text"] = normalized
                segments[-1]["character_count"] = len(normalized)
                current_segment = _create_empty_segment(len(segments) + 1)
                continue

            next_scene = scenes[i + 1] if i + 1 < len(scenes) and isinstance(scenes[i + 1], dict) else {}
            next_text = str(next_scene.get("text") or "").strip()
            with_next_len = len(f"{current_segment['text']} {next_text}") if next_text else current_len

            if current_len >= cls.TTS_MIN:
                close_now = (
                    current_len >= cls.TTS_SOFT_MAX
                    or with_next_len > cls.TTS_SOFT_MAX
                    or abs(current_len - cls.TTS_TARGET) <= abs(with_next_len - cls.TTS_TARGET)
                )
            else:
                # Prefer short sentence-complete chunks over drifting toward hard cap.
                close_now = with_next_len > cls.TTS_SOFT_MAX

            if close_now:
                normalized = cls._ensure_sentence_end(str(current_segment["text"]))
                segments.append(
                    cls._build_segment_from_fragments(
                        segment_id=int(current_segment["id"]),
                        scene_fragments=[
                            *list(current_segment.get("scene_fragments") or []),
                        ],
                    )
                )
                segments[-1]["text"] = normalized
                segments[-1]["character_count"] = len(normalized)
                current_segment = _create_empty_segment(len(segments) + 1)

        if current_segment["scene_fragments"]:
            normalized = cls._ensure_sentence_end(str(current_segment["text"]))
            segments.append(
                cls._build_segment_from_fragments(
                    segment_id=int(current_segment["id"]),
                    scene_fragments=[
                        *list(current_segment.get("scene_fragments") or []),
                    ],
                )
            )
            segments[-1]["text"] = normalized
            segments[-1]["character_count"] = len(normalized)

        # Merge small final chunk only when it keeps us within soft max and preserves sentence ending.
        if (
            len(segments) >= 2
            and int(segments[-1]["character_count"]) < cls.TTS_MIN
            and _SENTENCE_END_RE.search(str(segments[-1]["text"]))
        ):
            merged = f"{segments[-2]['text']} {segments[-1]['text']}".strip()
            if len(merged) <= cls.TTS_SOFT_MAX:
                merged_segment = cls._build_segment_from_fragments(
                    segment_id=int(segments[-2]["id"]),
                    scene_fragments=[
                        *list(segments[-2].get("scene_fragments") or []),
                        *list(segments[-1].get("scene_fragments") or []),
                    ],
                )
                merged_segment["text"] = cls._ensure_sentence_end(merged)
                merged_segment["character_count"] = len(str(merged_segment["text"]))
                segments[-2] = merged_segment
                segments.pop()

        for i, segment in enumerate(segments):
            segment["id"] = i + 1

        return segments

    @classmethod
    def _segment_scenes_for_tts(cls, script_payload: dict[str, Any]) -> list[str]:
        return [str(segment.get("text", "")).strip() for segment in cls._segment_scenes_for_tts_payload(script_payload)]

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
    def _is_pcm_format(cls) -> bool:
        return (settings.elevenlabs_output_format or "").strip().lower().startswith("pcm")

    @staticmethod
    def _wrap_pcm_as_wav(
        pcm_data: bytes,
        *,
        sample_rate: int = 44100,
        sample_width: int = 2,
        channels: int = 1,
    ) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return buf.getvalue()

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
    def generate_video_overlay(
        cls,
        *,
        project: Project,
        script_payload: dict[str, Any],
        target_language: str,
    ) -> dict[str, Any]:
        """Generate a video overlay (title hooks + category) via Gemini light model."""
        anime_name = project.anime_name or "Inconnu"
        script_summary = cls._script_text_from_payload(script_payload)[:500]
        prompt = ScriptPhasePromptService.build_overlay_prompt(
            anime_name=anime_name,
            script_summary=script_summary,
            target_language=target_language,
        )

        result = GeminiService.generate_json(
            prompt,
            model=settings.gemini_light_model,
            response_json_schema=_OVERLAY_RESPONSE_SCHEMA,
        )
        return cls._normalize_overlay_payload(result)

    @classmethod
    def get_latest_run(cls, project_id: str) -> dict[str, Any] | None:
        """Return the latest automation run with its script.json and TTS parts."""
        project_dir = ProjectService.get_project_dir(project_id)
        runs_dir = project_dir / cls.RUNS_DIR_NAME
        if not runs_dir.is_dir():
            return None

        subdirs = [d for d in runs_dir.iterdir() if d.is_dir()]
        if not subdirs:
            return None

        # Pick the most recently modified run directory
        latest = max(subdirs, key=lambda d: d.stat().st_mtime)
        run_id = latest.name

        script_path = latest / "script.json"
        if not script_path.exists():
            return None

        script_json = json.loads(script_path.read_text(encoding="utf-8"))

        parts: list[dict[str, Any]] = []
        parts_dir = latest / "parts"
        if parts_dir.is_dir():
            part_files = sorted(parts_dir.glob("part_*.*"))
            for pf in part_files:
                # Extract part id from filename like part_1.mp3
                stem = pf.stem  # e.g. "part_1"
                part_id_str = stem.split("_", 1)[1] if "_" in stem else None
                if part_id_str and part_id_str.isdigit():
                    parts.append({
                        "id": part_id_str,
                        "char_count": 0,
                        "download_url": f"/api/projects/{project_id}/script/automate/runs/{run_id}/parts/{part_id_str}",
                    })

        return {
            "run_id": run_id,
            "script_json": script_json,
            "parts": parts,
        }

    @classmethod
    async def stream_automation(
        cls,
        *,
        project_id: str,
        target_language: str,
        voice_key: str,
        existing_script_json: dict[str, Any] | None = None,
        skip_metadata: bool = False,
        skip_tts: bool = False,
        pause_after_script: bool = False,
        skip_overlay: bool = False,
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

            if existing_script_json is None and not GeminiService.is_configured():
                raise RuntimeError("Gemini API key is missing (ATR_GEMINI_API_KEY)")
            if not skip_tts and not ElevenLabsService.is_configured():
                raise RuntimeError("ElevenLabs API key is missing (ATR_ELEVENLABS_API_KEY)")

            voice = VoiceConfigService.get_voice(voice_key)

            run_id = uuid.uuid4().hex
            run_dir, parts_dir = cls._prepare_run_dirs(project_id, run_id)

            yield cls._event(
                "starting",
                message="Automation started",
                run_id=run_id,
            )

            # --- Script generation (or reuse existing) ---
            if existing_script_json is not None:
                yield cls._event("llm_script", message="Script JSON provided — skipping generation")
                script_payload = cls._normalize_script_payload(
                    payload=existing_script_json,
                    transcription=transcription,
                    target_language=target_language,
                )
            else:
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
                message="Script JSON ready",
                script_scene_count=len(script_payload.get("scenes", [])),
            )

            # --- Pause for validation ---
            if pause_after_script:
                yield cls._event(
                    "script_ready",
                    status="paused",
                    message="Script ready for validation",
                    run_id=run_id,
                    script_json=script_payload,
                )
                return

            # --- TTS generation ---
            parts: list[dict[str, Any]] = []
            metadata_payload: dict[str, Any] | None = None
            metadata_warning: str | None = None
            overlay_json: dict[str, Any] | None = None

            if skip_tts:
                yield cls._event("tts_generating", message="TTS generation skipped")
            else:
                yield cls._event("tts_segmenting", message="Segmenting script for TTS...")
                prepared_tts = cls.prepare_tts_payload(script_payload=script_payload, target_language=target_language)
                segments = prepared_tts.get("segments", [])
                chunks = [str(segment.get("text", "")).strip() for segment in segments]
                if not chunks:
                    raise RuntimeError("Failed to segment script text for TTS")

                yield cls._event(
                    "tts_segmenting",
                    message=f"Prepared {len(chunks)} TTS segment(s)",
                    segment_count=len(chunks),
                )

                extension = cls._output_extension()
                part_paths: list[Path] = []

                for idx, chunk in enumerate(chunks, start=1):
                    segment_char_count = len(chunk)
                    if idx - 1 < len(segments):
                        raw_count = segments[idx - 1].get("character_count")
                        if isinstance(raw_count, int):
                            segment_char_count = raw_count
                    yield cls._event(
                        "tts_generating",
                        message=f"Generating audio part {idx}/{len(chunks)}...",
                        part_id=str(idx),
                        part_index=idx,
                        part_total=len(chunks),
                        char_count=segment_char_count,
                    )

                    effective_model = voice.model_id or settings.elevenlabs_model_id
                    # eleven_v3 does not support previous_text/next_text
                    supports_context = not effective_model.startswith("eleven_v3")
                    audio_bytes = await asyncio.to_thread(
                        ElevenLabsService.synthesize,
                        voice_id=voice.elevenlabs_voice_id,
                        text=chunk,
                        model_id=effective_model,
                        output_format=settings.elevenlabs_output_format,
                        voice_settings=voice.voice_settings or None,
                        previous_text=(chunks[idx - 2] if idx >= 2 else None) if supports_context else None,
                        next_text=(chunks[idx] if idx <= len(chunks) - 1 else None) if supports_context else None,
                    )
                    if cls._is_pcm_format():
                        audio_bytes = cls._wrap_pcm_as_wav(audio_bytes)
                    part_path = parts_dir / f"part_{idx}.{extension}"
                    part_path.write_bytes(audio_bytes)
                    part_paths.append(part_path)

                    parts.append(
                        {
                            "id": str(idx),
                            "char_count": segment_char_count,
                            "download_url": f"/api/projects/{project_id}/script/automate/runs/{run_id}/parts/{idx}",
                        }
                    )

                merged_path = run_dir / "merged.wav"
                await asyncio.to_thread(cls._merge_parts_to_wav, part_paths, merged_path)

            # --- Metadata generation ---
            if skip_metadata or not settings.automate_metadata_overlay_enabled:
                yield cls._event("llm_metadata", message="Metadata generation skipped")
            else:
                yield cls._event("llm_metadata", message="Generating metadata JSON with Gemini...")
                try:
                    metadata_prompt = MetadataService.build_prompt_from_script_payload(
                        anime_name=project.anime_name or "Inconnu",
                        script_payload=script_payload,
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

            # --- Video overlay generation ---
            if not skip_overlay and settings.automate_metadata_overlay_enabled and GeminiService.is_configured():
                yield cls._event("generating_overlay", message="Generating video overlay...")
                try:
                    overlay_json = await asyncio.to_thread(
                        cls.generate_video_overlay,
                        project=project,
                        script_payload=script_payload,
                        target_language=target_language,
                    )
                    yield cls._event(
                        "overlay_ready",
                        message="Video overlay generated",
                        overlay_json=overlay_json,
                    )
                except Exception as exc:
                    yield cls._event(
                        "overlay_ready",
                        message=f"Overlay generation failed: {exc}",
                        warning=f"Overlay generation failed: {exc}",
                    )

            complete_payload = cls._event(
                "complete",
                status="complete",
                message="Automation complete",
                run_id=run_id,
                script_json=script_payload,
                metadata_json=metadata_payload,
                metadata_warning=metadata_warning,
                overlay_json=overlay_json,
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
