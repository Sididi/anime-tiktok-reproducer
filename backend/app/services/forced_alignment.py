from __future__ import annotations

import json
import math
import shutil
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydub import AudioSegment

from ..models import SceneTranscription, Transcription, Word
from .audio_speed_service import AudioSpeedService
from .project_service import ProjectService
from .script_automation_service import ScriptAutomationService
from .transcriber import LANGUAGE_MAP, TranscriberService


@dataclass(frozen=True)
class PreparedAlignmentAudio:
    mode: str
    edited_audio_path: Path
    segment_audio_paths: list[Path]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ForcedAlignmentResult:
    transcription: Transcription
    report: dict[str, Any]
    manifest: dict[str, Any]


class ForcedAlignmentError(RuntimeError):
    def __init__(self, message: str, *, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report or {}


class ForcedAlignmentService:
    MANIFEST_FILENAME = "tts_alignment_manifest.json"
    PARTS_DIRNAME = "tts_parts"
    REPORT_FILENAME = "alignment_report.json"
    PARTS_OUTPUT_DIRNAME = "alignment_parts"

    COVERAGE_MIN = 0.90
    WINDOW_PADDING_SEC = 0.35
    WINDOW_RETRY_PADDING_SEC = 1.0
    MIN_WINDOW_SEC = 1.2
    LONG_WORD_ABS_MAX_SEC = 3.0
    LONG_WORD_MEDIAN_FACTOR = 4.0

    @classmethod
    def manifest_path(cls, project_id: str) -> Path:
        return ProjectService.get_project_dir(project_id) / cls.MANIFEST_FILENAME

    @classmethod
    def parts_dir(cls, project_id: str) -> Path:
        return ProjectService.get_project_dir(project_id) / cls.PARTS_DIRNAME

    @classmethod
    def report_path(cls, output_dir: Path) -> Path:
        return output_dir / cls.REPORT_FILENAME

    @classmethod
    def clear_upload_artifacts(cls, project_id: str) -> None:
        manifest_path = cls.manifest_path(project_id)
        if manifest_path.exists():
            manifest_path.unlink()

        parts_dir = cls.parts_dir(project_id)
        if parts_dir.exists():
            shutil.rmtree(parts_dir)

    @classmethod
    def load_upload_manifest(cls, project_id: str) -> dict[str, Any] | None:
        manifest_path = cls.manifest_path(project_id)
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    @classmethod
    def save_upload_manifest(
        cls,
        project_id: str,
        *,
        script_payload: dict[str, Any],
        mode: str,
        stored_part_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        prepared = ScriptAutomationService.prepare_tts_payload(
            script_payload=script_payload,
            target_language=script_payload.get("language"),
        )
        segments = prepared.get("segments") or []

        if mode not in {"audio_parts", "single_audio"}:
            raise ValueError(f"Unsupported alignment manifest mode: {mode}")

        if stored_part_paths is not None and len(stored_part_paths) != len(segments):
            raise ValueError(
                "Audio parts count does not match expected TTS segment count "
                f"({len(stored_part_paths)} != {len(segments)})"
            )

        manifest_segments: list[dict[str, Any]] = []
        for idx, segment in enumerate(segments):
            entry = {
                "id": int(segment["id"]),
                "scene_indices": [int(value) for value in segment.get("scene_indices", [])],
                "text": str(segment.get("text") or ""),
                "character_count": int(segment.get("character_count") or 0),
            }
            if stored_part_paths is not None:
                entry["audio_path"] = stored_part_paths[idx]
            manifest_segments.append(entry)

        manifest = {
            "version": 1,
            "mode": mode,
            "language": prepared.get("language") or script_payload.get("language") or "fr",
            "segments": manifest_segments,
        }
        cls.manifest_path(project_id).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest

    @classmethod
    async def prepare_audio_from_parts(
        cls,
        *,
        project_id: str,
        output_dir: Path,
        tts_speed: float,
        auto_editor_runner: Callable[[Path, Path], Awaitable[bool]],
    ) -> PreparedAlignmentAudio:
        manifest = cls.load_upload_manifest(project_id)
        if not manifest or manifest.get("mode") != "audio_parts":
            raise RuntimeError("No audio_parts alignment manifest available")

        project_dir = ProjectService.get_project_dir(project_id)
        parts_output_dir = output_dir / cls.PARTS_OUTPUT_DIRNAME
        if parts_output_dir.exists():
            shutil.rmtree(parts_output_dir)
        parts_output_dir.mkdir(parents=True, exist_ok=True)

        processed_paths: list[Path] = []
        runtime_segments: list[dict[str, Any]] = []

        for segment in manifest.get("segments", []):
            segment_id = int(segment["id"])
            raw_rel = segment.get("audio_path")
            if not isinstance(raw_rel, str) or not raw_rel:
                raise RuntimeError(f"Missing raw audio_path for alignment segment {segment_id}")

            raw_path = project_dir / raw_rel
            if not raw_path.exists():
                raise RuntimeError(f"Missing audio part for alignment: {raw_path}")

            work_input = raw_path
            if tts_speed != 1.0:
                speed_path = parts_output_dir / f"part_{segment_id:04d}_speed.wav"
                await AudioSpeedService.apply_speed(raw_path, speed_path, tts_speed)
                work_input = speed_path

            edited_path = parts_output_dir / f"part_{segment_id:04d}.wav"
            await auto_editor_runner(work_input, edited_path)
            if not edited_path.exists():
                raise RuntimeError(f"auto-editor did not produce {edited_path.name}")

            processed_paths.append(edited_path)
            runtime_entry = dict(segment)
            runtime_entry["prepared_audio_path"] = str(edited_path.relative_to(project_dir))
            runtime_entry["prepared_duration"] = cls._probe_wav_duration(edited_path)
            runtime_segments.append(runtime_entry)

        edited_audio_path = output_dir / "tts_edited.wav"
        cls._concat_audio_files(processed_paths, edited_audio_path)

        runtime_manifest = {
            **manifest,
            "segments": runtime_segments,
        }
        return PreparedAlignmentAudio(
            mode="audio_parts",
            edited_audio_path=edited_audio_path,
            segment_audio_paths=processed_paths,
            manifest=runtime_manifest,
        )

    @classmethod
    def build_single_audio_manifest(cls, *, script_payload: dict[str, Any]) -> dict[str, Any]:
        prepared = ScriptAutomationService.prepare_tts_payload(
            script_payload=script_payload,
            target_language=script_payload.get("language"),
        )
        return {
            "version": 1,
            "mode": "single_audio",
            "language": prepared.get("language") or script_payload.get("language") or "fr",
            "segments": [
                {
                    "id": int(segment["id"]),
                    "scene_indices": [int(value) for value in segment.get("scene_indices", [])],
                    "text": str(segment.get("text") or ""),
                    "character_count": int(segment.get("character_count") or 0),
                }
                for segment in prepared.get("segments", [])
            ],
        }

    @classmethod
    def align_known_script(
        cls,
        *,
        project_id: str,
        script_payload: dict[str, Any],
        reference_transcription: Transcription,
        prepared_audio: PreparedAlignmentAudio,
        output_dir: Path,
        coarse_model_size: str = "large-v3",
    ) -> ForcedAlignmentResult:
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            manifest = cls._validate_manifest_against_script(
                manifest=prepared_audio.manifest,
                script_payload=script_payload,
            )
            align_language = cls._resolve_alignment_language(manifest.get("language"))
            if prepared_audio.mode == "audio_parts":
                result = cls._align_audio_parts(
                    project_id=project_id,
                    script_payload=script_payload,
                    reference_transcription=reference_transcription,
                    manifest=manifest,
                    segment_audio_paths=prepared_audio.segment_audio_paths,
                    align_language=align_language,
                    output_dir=output_dir,
                )
            else:
                result = cls._align_single_audio(
                    project_id=project_id,
                    script_payload=script_payload,
                    reference_transcription=reference_transcription,
                    manifest=manifest,
                    edited_audio_path=prepared_audio.edited_audio_path,
                    align_language=align_language,
                    coarse_model_size=coarse_model_size,
                    output_dir=output_dir,
                )
        except ForcedAlignmentError as exc:
            cls.write_alignment_report(output_dir, exc.report)
            raise
        except Exception as exc:
            report = {
                "status": "error",
                "mode": prepared_audio.mode,
                "message": str(exc),
                "segments": [],
                "global_issues": [str(exc)],
            }
            cls.write_alignment_report(output_dir, report)
            raise ForcedAlignmentError(str(exc), report=report) from exc

        cls.write_alignment_report(output_dir, result.report)
        return result

    @classmethod
    def write_alignment_report(cls, output_dir: Path, report: dict[str, Any]) -> None:
        cls.report_path(output_dir).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def validate_transcription_basics(cls, transcription: Transcription) -> list[str]:
        issues: list[str] = []
        previous_end = -1.0

        for scene in transcription.scenes:
            if scene.is_raw:
                continue

            if scene.text.strip() and not scene.words:
                issues.append(f"Scene {scene.scene_index} has text but no aligned words")
                continue

            durations = [word.end - word.start for word in scene.words if word.end > word.start]
            median_duration = cls._median_duration(durations)
            long_word_threshold = max(
                cls.LONG_WORD_ABS_MAX_SEC,
                cls.LONG_WORD_MEDIAN_FACTOR * median_duration,
            )

            for word in scene.words:
                if word.end <= word.start:
                    issues.append(
                        f"Scene {scene.scene_index} has non-positive word duration for '{word.text}'"
                    )
                    continue
                if previous_end > 0 and word.start + 1e-6 < previous_end:
                    issues.append(
                        f"Scene {scene.scene_index} is not monotonic at '{word.text}'"
                    )
                    break
                if (word.end - word.start) > long_word_threshold:
                    issues.append(
                        f"Scene {scene.scene_index} has anomalously long word '{word.text}'"
                    )
                    break
                previous_end = max(previous_end, word.end)

        return issues

    @classmethod
    def _validate_manifest_against_script(
        cls,
        *,
        manifest: dict[str, Any],
        script_payload: dict[str, Any],
    ) -> dict[str, Any]:
        expected = cls.build_single_audio_manifest(script_payload=script_payload)
        expected_segments = expected.get("segments", [])
        actual_segments = manifest.get("segments", [])
        if len(actual_segments) != len(expected_segments):
            raise ForcedAlignmentError(
                "Alignment manifest does not match current script segment count",
                report={
                    "status": "error",
                    "mode": manifest.get("mode") or "unknown",
                    "segments": actual_segments,
                    "global_issues": [
                        "Manifest segment count does not match current script segment count"
                    ],
                },
            )

        validated_segments: list[dict[str, Any]] = []
        for actual, expected_segment in zip(actual_segments, expected_segments, strict=False):
            if (
                int(actual.get("id") or 0) != int(expected_segment["id"])
                or list(actual.get("scene_indices") or []) != list(expected_segment["scene_indices"])
                or str(actual.get("text") or "") != str(expected_segment["text"])
            ):
                raise ForcedAlignmentError(
                    "Alignment manifest does not match current script content",
                    report={
                        "status": "error",
                        "mode": manifest.get("mode") or "unknown",
                        "segments": actual_segments,
                        "global_issues": [
                            "Manifest segment content does not match current script"
                        ],
                    },
                )
            merged = dict(expected_segment)
            merged.update(actual)
            validated_segments.append(merged)

        validated_manifest = dict(expected)
        validated_manifest.update({
            "mode": manifest.get("mode") or expected.get("mode") or "single_audio",
            "language": manifest.get("language") or expected.get("language") or "fr",
            "segments": validated_segments,
        })
        return validated_manifest

    @classmethod
    def _align_audio_parts(
        cls,
        *,
        project_id: str,
        script_payload: dict[str, Any],
        reference_transcription: Transcription,
        manifest: dict[str, Any],
        segment_audio_paths: list[Path],
        align_language: str,
        output_dir: Path,
    ) -> ForcedAlignmentResult:
        segments = manifest.get("segments", [])
        if len(segments) != len(segment_audio_paths):
            raise ForcedAlignmentError(
                "Prepared audio parts do not match the alignment manifest",
                report={
                    "status": "error",
                    "mode": "audio_parts",
                    "segments": segments,
                    "global_issues": ["Prepared audio parts count does not match manifest"],
                },
            )

        alignment_model, alignment_metadata, align_device = cls._load_alignment_backend(
            align_language
        )

        project_dir = ProjectService.get_project_dir(project_id)
        runtime_segments: list[dict[str, Any]] = []
        aligned_segment_payloads: list[dict[str, Any]] = []
        timeline_offset = 0.0

        for segment, clip_path in zip(segments, segment_audio_paths, strict=False):
            aligned_words = cls._align_clip_exact(
                clip_path=clip_path,
                transcript_text=str(segment["text"]),
                align_language=align_language,
                alignment_model=alignment_model,
                alignment_metadata=alignment_metadata,
                align_device=align_device,
            )
            offset_words = [
                Word(
                    text=word.text,
                    start=word.start + timeline_offset,
                    end=word.end + timeline_offset,
                    confidence=word.confidence,
                )
                for word in aligned_words
            ]
            offset_words = cls._clamp_words_to_range(
                words=offset_words,
                start_sec=timeline_offset,
                end_sec=timeline_offset + cls._probe_wav_duration(clip_path),
            )
            segment_report, scene_payload = cls._map_segment_words_to_scenes(
                segment=segment,
                segment_words=offset_words,
                script_payload=script_payload,
            )
            segment_report["mode"] = "audio_parts"
            segment_report["timeline_start"] = round(timeline_offset, 6)
            timeline_offset += cls._probe_wav_duration(clip_path)
            segment_report["timeline_end"] = round(timeline_offset, 6)
            segment_report["audio_path"] = str(clip_path.relative_to(project_dir))
            runtime_segments.append(segment_report)
            aligned_segment_payloads.append(scene_payload)

        transcription, scene_issues = cls._build_transcription_from_segment_payloads(
            script_payload=script_payload,
            reference_transcription=reference_transcription,
            aligned_segment_payloads=aligned_segment_payloads,
        )
        cls._enforce_monotonic_words(transcription)
        global_issues = scene_issues + cls.validate_transcription_basics(transcription)
        report = cls._build_report(
            status="ok" if not global_issues and cls._segments_are_valid(runtime_segments) else "error",
            mode="audio_parts",
            language=manifest.get("language") or align_language,
            edited_audio_path=output_dir / "tts_edited.wav",
            segments=runtime_segments,
            global_issues=global_issues,
        )
        if report["status"] != "ok":
            raise ForcedAlignmentError(
                "Forced alignment quality check failed for audio parts",
                report=report,
            )
        return ForcedAlignmentResult(transcription=transcription, report=report, manifest=manifest)

    @classmethod
    def _align_single_audio(
        cls,
        *,
        project_id: str,
        script_payload: dict[str, Any],
        reference_transcription: Transcription,
        manifest: dict[str, Any],
        edited_audio_path: Path,
        align_language: str,
        coarse_model_size: str,
        output_dir: Path,
    ) -> ForcedAlignmentResult:
        coarse_words, coarse_language = TranscriberService._transcribe_sync(
            edited_audio_path,
            align_language,
            coarse_model_size,
        )
        align_language = cls._resolve_alignment_language(coarse_language or align_language)
        alignment_model, alignment_metadata, align_device = cls._load_alignment_backend(
            align_language
        )

        segments = manifest.get("segments", [])
        total_duration = cls._probe_wav_duration(edited_audio_path)
        windows = cls._build_segment_windows(
            segments=segments,
            coarse_words=coarse_words,
            total_duration=total_duration,
        )

        runtime_segments: list[dict[str, Any]] = []
        aligned_segment_payloads: list[dict[str, Any]] = []

        for idx, (segment, window) in enumerate(zip(segments, windows, strict=False)):
            best_report: dict[str, Any] | None = None
            lower_bound = windows[idx - 1]["end"] if idx > 0 else 0.0
            upper_bound = windows[idx + 1]["start"] if idx + 1 < len(windows) else total_duration
            for attempt_idx, padding in enumerate((0.0, cls.WINDOW_RETRY_PADDING_SEC), start=1):
                clip_start = max(lower_bound, window["start"] - padding)
                clip_end = min(upper_bound, window["end"] + padding)
                if clip_end <= clip_start:
                    clip_end = min(upper_bound, clip_start + cls.MIN_WINDOW_SEC)
                if clip_end <= clip_start:
                    clip_start = max(lower_bound, min(clip_start, upper_bound))
                    clip_end = max(clip_start, upper_bound)

                with tempfile.TemporaryDirectory() as tmp_dir:
                    clip_path = Path(tmp_dir) / f"segment_{int(segment['id']):04d}.wav"
                    cls._extract_wav_window(
                        input_path=edited_audio_path,
                        start_sec=clip_start,
                        end_sec=clip_end,
                        output_path=clip_path,
                    )
                    aligned_words = cls._align_clip_exact(
                        clip_path=clip_path,
                        transcript_text=str(segment["text"]),
                        align_language=align_language,
                        alignment_model=alignment_model,
                        alignment_metadata=alignment_metadata,
                        align_device=align_device,
                    )

                offset_words = [
                    Word(
                        text=word.text,
                        start=word.start + clip_start,
                        end=word.end + clip_start,
                        confidence=word.confidence,
                    )
                    for word in aligned_words
                ]
                offset_words = cls._clamp_words_to_range(
                    words=offset_words,
                    start_sec=clip_start,
                    end_sec=clip_end,
                )
                segment_report, scene_payload = cls._map_segment_words_to_scenes(
                    segment=segment,
                    segment_words=offset_words,
                    script_payload=script_payload,
                )
                segment_report["mode"] = "single_audio"
                segment_report["window_start"] = round(clip_start, 6)
                segment_report["window_end"] = round(clip_end, 6)
                segment_report["coarse_window_start"] = round(window["start"], 6)
                segment_report["coarse_window_end"] = round(window["end"], 6)
                segment_report["attempt"] = attempt_idx
                best_report = segment_report
                best_scene_payload = scene_payload
                if segment_report["coverage"] >= cls.COVERAGE_MIN and not segment_report["issues"]:
                    break

            assert best_report is not None
            runtime_segments.append(best_report)
            aligned_segment_payloads.append(best_scene_payload)

        transcription, scene_issues = cls._build_transcription_from_segment_payloads(
            script_payload=script_payload,
            reference_transcription=reference_transcription,
            aligned_segment_payloads=aligned_segment_payloads,
        )
        cls._enforce_monotonic_words(transcription)
        global_issues = scene_issues + cls.validate_transcription_basics(transcription)
        report = cls._build_report(
            status="ok" if not global_issues and cls._segments_are_valid(runtime_segments) else "error",
            mode="single_audio",
            language=align_language,
            edited_audio_path=edited_audio_path,
            segments=runtime_segments,
            global_issues=global_issues,
            coarse_word_count=len(coarse_words),
        )
        if report["status"] != "ok":
            raise ForcedAlignmentError(
                "Forced alignment quality check failed for single audio",
                report=report,
            )
        return ForcedAlignmentResult(transcription=transcription, report=report, manifest=manifest)

    @classmethod
    def _segments_are_valid(cls, segments: list[dict[str, Any]]) -> bool:
        return all(
            float(segment.get("coverage") or 0.0) >= cls.COVERAGE_MIN
            and not segment.get("issues")
            for segment in segments
        )

    @classmethod
    def _build_report(
        cls,
        *,
        status: str,
        mode: str,
        language: str,
        edited_audio_path: Path,
        segments: list[dict[str, Any]],
        global_issues: list[str],
        coarse_word_count: int | None = None,
    ) -> dict[str, Any]:
        report = {
            "status": status,
            "mode": mode,
            "language": language,
            "edited_audio_path": str(edited_audio_path),
            "segment_count": len(segments),
            "segments": segments,
            "global_issues": global_issues,
        }
        if coarse_word_count is not None:
            report["coarse_word_count"] = coarse_word_count
        return report

    @classmethod
    def _map_segment_words_to_scenes(
        cls,
        *,
        segment: dict[str, Any],
        segment_words: list[Word],
        script_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[int, list[Word]]]:
        scenes_by_index = {
            int(scene.get("scene_index")): scene
            for scene in script_payload.get("scenes", [])
        }

        expected_entries: list[dict[str, Any]] = []
        for scene_index in segment.get("scene_indices") or []:
            scene_data = scenes_by_index.get(int(scene_index))
            if not scene_data:
                continue
            for raw_word in str(scene_data.get("text") or "").split():
                norm = TranscriberService._normalize_token(raw_word)
                if not norm:
                    continue
                expected_entries.append({
                    "scene_index": int(scene_index),
                    "text": raw_word,
                    "norm": norm,
                })

        aligned_entries: list[dict[str, Any]] = []
        for word in segment_words:
            norm = TranscriberService._normalize_token(word.text)
            if not norm:
                continue
            aligned_entries.append({
                "word": word,
                "norm": norm,
            })

        mapping = TranscriberService._sequence_align(
            [entry["norm"] for entry in expected_entries],
            [entry["norm"] for entry in aligned_entries],
        )

        scene_word_map: dict[int, list[Word]] = {}
        matched_count = 0
        for idx, expected_entry in enumerate(expected_entries):
            mapped_idx = mapping[idx] if idx < len(mapping) else None
            if mapped_idx is None:
                continue
            aligned_entry = aligned_entries[mapped_idx]
            scene_word_map.setdefault(int(expected_entry["scene_index"]), []).append(
                Word(
                    text=str(expected_entry["text"]),
                    start=aligned_entry["word"].start,
                    end=aligned_entry["word"].end,
                    confidence=aligned_entry["word"].confidence,
                )
            )
            matched_count += 1

        durations = [
            word.end - word.start
            for words in scene_word_map.values()
            for word in words
            if word.end > word.start
        ]
        median_duration = cls._median_duration(durations)
        long_word_threshold = max(
            cls.LONG_WORD_ABS_MAX_SEC,
            cls.LONG_WORD_MEDIAN_FACTOR * median_duration,
        )

        issues: list[str] = []
        coverage = 1.0 if not expected_entries else matched_count / len(expected_entries)
        if coverage < cls.COVERAGE_MIN:
            issues.append(
                f"Lexical coverage below threshold ({coverage:.3f} < {cls.COVERAGE_MIN:.2f})"
            )

        for words in scene_word_map.values():
            for word in words:
                if (word.end - word.start) > long_word_threshold:
                    issues.append(f"Anomalously long word '{word.text}'")
                    break
            if issues:
                break

        report = {
            "id": int(segment["id"]),
            "scene_indices": [int(value) for value in segment.get("scene_indices", [])],
            "text": str(segment.get("text") or ""),
            "expected_word_count": len(expected_entries),
            "aligned_word_count": len(aligned_entries),
            "matched_word_count": matched_count,
            "coverage": round(coverage, 6),
            "issues": issues,
        }
        return report, scene_word_map

    @classmethod
    def _build_transcription_from_segment_payloads(
        cls,
        *,
        script_payload: dict[str, Any],
        reference_transcription: Transcription,
        aligned_segment_payloads: list[dict[int, list[Word]]],
    ) -> tuple[Transcription, list[str]]:
        scene_word_map: dict[int, list[Word]] = {}
        for payload in aligned_segment_payloads:
            for scene_index, words in payload.items():
                scene_word_map.setdefault(scene_index, []).extend(words)

        reference_by_index = {
            scene.scene_index: scene for scene in reference_transcription.scenes
        }
        scene_transcriptions: list[SceneTranscription] = []
        issues: list[str] = []

        for scene_data in script_payload.get("scenes", []):
            scene_index = int(scene_data.get("scene_index", 0))
            scene_text = str(scene_data.get("text") or "")
            words = scene_word_map.get(scene_index, [])
            reference_scene = reference_by_index.get(scene_index)
            is_raw = bool(scene_data.get("is_raw"))
            if reference_scene is not None:
                is_raw = is_raw or reference_scene.is_raw

            if words:
                words = sorted(words, key=lambda word: (word.start, word.end))
                start_time = words[0].start
                end_time = words[-1].end
            elif is_raw:
                if reference_scene is not None:
                    start_time = reference_scene.start_time
                    end_time = reference_scene.end_time
                else:
                    start_time = float(scene_data.get("start_time") or 0.0)
                    end_time = float(scene_data.get("end_time") or 0.0)
            else:
                start_time = 0.0
                end_time = 0.0
                if scene_text.strip():
                    issues.append(f"Scene {scene_index} has no aligned lexical words")

            scene_transcriptions.append(
                SceneTranscription(
                    scene_index=scene_index,
                    text=scene_text,
                    words=words,
                    start_time=start_time,
                    end_time=end_time,
                    is_raw=is_raw,
                )
            )

        transcription = Transcription(
            language=str(script_payload.get("language") or reference_transcription.language or "fr"),
            scenes=scene_transcriptions,
        )
        return transcription, issues

    @classmethod
    def _enforce_monotonic_words(cls, transcription: Transcription) -> None:
        """Adjust word timings to be strictly monotonic across non-raw scenes."""
        min_duration = 0.02
        previous_end = 0.0
        for scene in transcription.scenes:
            if scene.is_raw or not scene.words:
                continue
            for word in scene.words:
                if word.start < previous_end:
                    word.start = previous_end
                if word.end < word.start + min_duration:
                    word.end = word.start + min_duration
                previous_end = word.end
            # Update scene boundaries from adjusted words
            scene.start_time = scene.words[0].start
            scene.end_time = scene.words[-1].end

    @classmethod
    def _build_segment_windows(
        cls,
        *,
        segments: list[dict[str, Any]],
        coarse_words: list[dict[str, Any]],
        total_duration: float,
    ) -> list[dict[str, float]]:
        coarse_entries: list[dict[str, Any]] = []
        for word in coarse_words:
            norm = TranscriberService._normalize_token(str(word.get("text") or ""))
            if not norm:
                continue
            coarse_entries.append({
                "norm": norm,
                "start": float(word["start"]),
                "end": float(word["end"]),
            })

        expected_entries: list[dict[str, Any]] = []
        for segment in segments:
            for raw_word in str(segment.get("text") or "").split():
                norm = TranscriberService._normalize_token(raw_word)
                if not norm:
                    continue
                expected_entries.append({
                    "segment_id": int(segment["id"]),
                    "norm": norm,
                })

        mapping = TranscriberService._sequence_align(
            [entry["norm"] for entry in expected_entries],
            [entry["norm"] for entry in coarse_entries],
        )

        coarse_indices_by_segment: dict[int, list[int]] = {}
        for idx, entry in enumerate(expected_entries):
            mapped_idx = mapping[idx] if idx < len(mapping) else None
            if mapped_idx is None:
                continue
            coarse_indices_by_segment.setdefault(int(entry["segment_id"]), []).append(mapped_idx)

        base_windows: list[dict[str, float] | None] = []
        for segment in segments:
            coarse_indices = coarse_indices_by_segment.get(int(segment["id"]), [])
            if coarse_indices:
                start = max(
                    0.0,
                    coarse_entries[min(coarse_indices)]["start"] - cls.WINDOW_PADDING_SEC,
                )
                end = min(
                    total_duration,
                    coarse_entries[max(coarse_indices)]["end"] + cls.WINDOW_PADDING_SEC,
                )
                if end <= start:
                    end = min(total_duration, start + cls.MIN_WINDOW_SEC)
                base_windows.append({"start": start, "end": end})
            else:
                base_windows.append(None)

        filled_windows: list[dict[str, float]] = []
        current_idx = 0
        while current_idx < len(segments):
            if base_windows[current_idx] is not None:
                filled_windows.append(base_windows[current_idx] or {"start": 0.0, "end": cls.MIN_WINDOW_SEC})
                current_idx += 1
                continue

            run_start = current_idx
            while current_idx < len(segments) and base_windows[current_idx] is None:
                current_idx += 1
            run_end = current_idx

            prev_window = filled_windows[-1] if filled_windows else None
            next_window = base_windows[run_end] if run_end < len(base_windows) else None
            span_start = prev_window["end"] if prev_window is not None else 0.0
            span_end = next_window["start"] if next_window is not None else total_duration
            if span_end <= span_start:
                span_end = min(total_duration, span_start + cls.MIN_WINDOW_SEC * (run_end - run_start))

            group_segments = segments[run_start:run_end]
            weights = [
                max(int(segment.get("character_count") or 0), len(str(segment.get("text") or "")), 1)
                for segment in group_segments
            ]
            total_weight = sum(weights) or len(group_segments)
            cursor = span_start
            for segment, weight in zip(group_segments, weights, strict=False):
                portion = (span_end - span_start) * (weight / total_weight)
                start = cursor
                end = cursor + portion
                if end <= start:
                    end = min(total_duration, start + cls.MIN_WINDOW_SEC)
                filled_windows.append({"start": max(0.0, start), "end": min(total_duration, end)})
                cursor = end

        normalized: list[dict[str, float]] = []
        for window in filled_windows:
            start = max(0.0, float(window["start"]))
            end = min(total_duration, float(window["end"]))
            if end - start < cls.MIN_WINDOW_SEC:
                deficit = cls.MIN_WINDOW_SEC - (end - start)
                start = max(0.0, start - deficit / 2)
                end = min(total_duration, end + deficit / 2)
                if end - start < cls.MIN_WINDOW_SEC:
                    end = min(total_duration, start + cls.MIN_WINDOW_SEC)
            normalized.append({"start": start, "end": end})

        return cls._ensure_non_overlapping_windows(
            windows=normalized,
            total_duration=total_duration,
        )

    @classmethod
    def _ensure_non_overlapping_windows(
        cls,
        *,
        windows: list[dict[str, float]],
        total_duration: float,
    ) -> list[dict[str, float]]:
        if not windows:
            return []

        normalized = [{"start": window["start"], "end": window["end"]} for window in windows]
        for idx in range(len(normalized) - 1):
            current = normalized[idx]
            nxt = normalized[idx + 1]
            if current["end"] <= nxt["start"]:
                continue

            boundary = (current["end"] + nxt["start"]) / 2.0
            min_boundary = current["start"] + cls.MIN_WINDOW_SEC
            max_boundary = nxt["end"] - cls.MIN_WINDOW_SEC
            if min_boundary <= max_boundary:
                boundary = min(max(boundary, min_boundary), max_boundary)
            else:
                boundary = min(max(boundary, current["start"]), nxt["end"])

            boundary = max(0.0, min(boundary, total_duration))
            current["end"] = boundary
            nxt["start"] = boundary

        for window in normalized:
            window["start"] = max(0.0, min(float(window["start"]), total_duration))
            window["end"] = max(window["start"], min(float(window["end"]), total_duration))

        return normalized

    @staticmethod
    def _extract_words_preserving_untimed(segments: list[dict]) -> list[dict]:
        words: list[dict] = []
        for segment in segments:
            segment_words = segment.get("words") or []
            if not segment_words:
                words.extend(TranscriberService._segment_words_from_text(segment))
                continue
            for word in segment_words:
                text = (word.get("word") or word.get("text") or "").strip()
                if not text:
                    continue
                start = word.get("start")
                end = word.get("end")
                confidence = word.get("score") or word.get("confidence") or word.get("probability")
                if confidence is None:
                    confidence = 1.0
                words.append({
                    "text": text,
                    "start": float(start) if start is not None else None,
                    "end": float(end) if end is not None else None,
                    "confidence": float(confidence),
                })
        return words

    @classmethod
    def _align_clip_exact(
        cls,
        *,
        clip_path: Path,
        transcript_text: str,
        align_language: str,
        alignment_model,
        alignment_metadata: dict[str, Any],
        align_device: str,
    ) -> list[Word]:
        import whisperx

        clip_duration = cls._probe_wav_duration(clip_path)
        transcript = [{"start": 0.0, "end": clip_duration, "text": transcript_text}]

        try:
            aligned = whisperx.align(
                transcript,
                alignment_model,
                alignment_metadata,
                str(clip_path),
                align_device,
            )
        except Exception as exc:
            if align_device == "cuda" and TranscriberService._is_cuda_oom(exc):
                cpu_model, cpu_metadata = TranscriberService._load_align_model(align_language, "cpu")
                aligned = whisperx.align(
                    transcript,
                    cpu_model,
                    cpu_metadata,
                    str(clip_path),
                    "cpu",
                )
            else:
                raise

        raw_words = cls._extract_words_preserving_untimed(aligned.get("segments") or [])
        median_dur = TranscriberService._median_word_duration(raw_words)
        TranscriberService._fill_missing_word_times(raw_words, median_dur)
        return cls._clamp_words_to_clip(
            words=[
                Word(
                    text=str(word["text"]),
                    start=float(word["start"]),
                    end=float(word["end"]),
                    confidence=float(word.get("confidence", 1.0)),
                )
                for word in raw_words
            ],
            clip_duration=clip_duration,
        )

    @classmethod
    def _clamp_words_to_clip(
        cls,
        *,
        words: list[Word],
        clip_duration: float,
    ) -> list[Word]:
        clamped: list[Word] = []
        for word in words:
            start = max(0.0, min(word.start, clip_duration))
            end = max(start, min(word.end, clip_duration))
            clamped.append(
                Word(
                    text=word.text,
                    start=start,
                    end=end,
                    confidence=word.confidence,
                )
            )
        return clamped

    @classmethod
    def _clamp_words_to_range(
        cls,
        *,
        words: list[Word],
        start_sec: float,
        end_sec: float,
    ) -> list[Word]:
        if end_sec < start_sec:
            end_sec = start_sec

        clamped: list[Word] = []
        for word in words:
            start = max(start_sec, min(word.start, end_sec))
            end = max(start, min(word.end, end_sec))
            clamped.append(
                Word(
                    text=word.text,
                    start=start,
                    end=end,
                    confidence=word.confidence,
                )
            )
        return clamped

    @classmethod
    def _load_alignment_backend(
        cls,
        align_language: str,
    ) -> tuple[Any, dict[str, Any], str]:
        device = TranscriberService._get_device()
        try:
            model, metadata = TranscriberService._load_align_model(align_language, device)
            return model, metadata, device
        except Exception as exc:
            if device == "cuda" and TranscriberService._is_cuda_oom(exc):
                model, metadata = TranscriberService._load_align_model(align_language, "cpu")
                return model, metadata, "cpu"
            raise

    @staticmethod
    def _resolve_alignment_language(language: str | None) -> str:
        if isinstance(language, str):
            normalized = LANGUAGE_MAP.get(language.lower(), language.lower())
            if isinstance(normalized, str) and normalized:
                return normalized
        return "fr"

    @staticmethod
    def _probe_wav_duration(path: Path) -> float:
        with wave.open(str(path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
        if frame_rate <= 0:
            return 0.0
        return frame_count / float(frame_rate)

    @staticmethod
    def _concat_audio_files(paths: list[Path], output_path: Path) -> None:
        combined = AudioSegment.empty()
        for path in paths:
            combined += AudioSegment.from_file(str(path))
        combined.export(str(output_path), format="wav")

    @staticmethod
    def _extract_wav_window(
        *,
        input_path: Path,
        start_sec: float,
        end_sec: float,
        output_path: Path,
    ) -> None:
        with wave.open(str(input_path), "rb") as src:
            params = src.getparams()
            frame_rate = src.getframerate()
            frame_count = src.getnframes()
            start_frame = max(0, int(math.floor(start_sec * frame_rate)))
            end_frame = min(frame_count, int(math.ceil(end_sec * frame_rate)))
            if end_frame <= start_frame:
                end_frame = min(frame_count, start_frame + max(1, int(frame_rate * 0.1)))
            src.setpos(start_frame)
            frames = src.readframes(end_frame - start_frame)

        with wave.open(str(output_path), "wb") as dst:
            dst.setparams(params)
            dst.writeframes(frames)

    @staticmethod
    def _median_duration(durations: list[float]) -> float:
        valid = sorted(duration for duration in durations if duration > 0)
        if not valid:
            return 0.2
        mid = len(valid) // 2
        if len(valid) % 2 == 1:
            return valid[mid]
        return (valid[mid - 1] + valid[mid]) / 2.0
