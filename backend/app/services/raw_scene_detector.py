"""Raw scene detection using pyannote speaker diarization.

Identifies segments where TTS narration stops and raw anime audio plays
(character dialogue, music, sound effects).
"""

import gc
import logging
from contextlib import suppress
from pathlib import Path

from ..config import settings
from ..models.raw_scene import RawSceneCandidate, RawSceneDetectionResult
from ..models.transcription import SceneTranscription

logger = logging.getLogger(__name__)

# Minimum duration for a raw region to be considered (seconds)
MIN_RAW_DURATION = 0.5
# Maximum gap between adjacent raw regions to merge them (seconds)
MERGE_GAP_THRESHOLD = 0.3


class RawSceneDetectorService:
    """Detect raw (non-TTS) scenes using pyannote speaker diarization."""

    @staticmethod
    def _get_device() -> str:
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    @classmethod
    def _run_diarization(cls, wav_path: Path) -> list[tuple[float, float, str]]:
        """Run pyannote diarization and return (start, end, speaker) segments."""
        import os
        import torch.serialization
        # pyannote/speechbrain model checkpoints from HuggingFace use pickle-based
        # serialization. PyTorch 2.8+ defaults to weights_only=True which blocks
        # these trusted model weights. This matches the pattern used by WhisperX
        # in TranscriberService._ensure_unsafe_torch_load_env().
        os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")

        from pyannote.audio import Pipeline

        hf_token = settings.hf_token
        if not hf_token:
            raise RuntimeError(
                "HF_TOKEN is required for raw scene detection. "
                "Set it in .env and accept pyannote model licenses on HuggingFace."
            )

        device = cls._get_device()
        logger.info("Loading pyannote diarization pipeline on %s", device)

        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )

        import torch
        if device == "cuda":
            pipeline.to(torch.device("cuda"))

        logger.info("Running diarization on %s", wav_path.name)
        diarization = pipeline(str(wav_path))

        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append((turn.start, turn.end, speaker))

        # Unload pipeline and free GPU memory
        del pipeline
        del diarization
        gc.collect()
        gc.collect()
        with suppress(Exception):
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        logger.info("Diarization complete: %d segments found", len(segments))
        return segments

    @staticmethod
    def _identify_tts_speaker(
        segments: list[tuple[float, float, str]],
    ) -> tuple[str, dict[str, float]]:
        """Identify the TTS speaker as the one with the most total duration."""
        speaker_durations: dict[str, float] = {}
        for start, end, speaker in segments:
            speaker_durations[speaker] = speaker_durations.get(speaker, 0.0) + (end - start)

        if not speaker_durations:
            return "", {}

        tts_speaker = max(speaker_durations, key=speaker_durations.get)  # type: ignore[arg-type]
        return tts_speaker, speaker_durations

    @staticmethod
    def _build_raw_regions(
        segments: list[tuple[float, float, str]],
        tts_speaker: str,
        audio_duration: float,
    ) -> list[tuple[float, float]]:
        """Build raw (non-TTS) regions from diarization segments.

        Returns list of (start, end) tuples for regions where TTS is NOT speaking.
        """
        if not segments or not tts_speaker:
            return []

        # Collect TTS speaker intervals
        tts_intervals: list[tuple[float, float]] = []
        for start, end, speaker in segments:
            if speaker == tts_speaker:
                tts_intervals.append((start, end))

        # Merge overlapping TTS intervals
        tts_intervals.sort()
        merged_tts: list[tuple[float, float]] = []
        for start, end in tts_intervals:
            if merged_tts and start <= merged_tts[-1][1] + MERGE_GAP_THRESHOLD:
                merged_tts[-1] = (merged_tts[-1][0], max(merged_tts[-1][1], end))
            else:
                merged_tts.append((start, end))

        # Raw regions = gaps between TTS intervals
        raw_regions: list[tuple[float, float]] = []
        prev_end = 0.0
        for tts_start, tts_end in merged_tts:
            if tts_start - prev_end >= MIN_RAW_DURATION:
                raw_regions.append((prev_end, tts_start))
            prev_end = tts_end
        # Trailing region after last TTS
        if audio_duration - prev_end >= MIN_RAW_DURATION:
            raw_regions.append((prev_end, audio_duration))

        # Merge adjacent raw regions separated by small gaps
        merged_raw: list[tuple[float, float]] = []
        for start, end in raw_regions:
            if merged_raw and start - merged_raw[-1][1] < MERGE_GAP_THRESHOLD:
                merged_raw[-1] = (merged_raw[-1][0], end)
            else:
                merged_raw.append((start, end))

        # Filter out very short raw regions
        return [(s, e) for s, e in merged_raw if e - s >= MIN_RAW_DURATION]

    @classmethod
    def _map_raw_regions_to_scenes(
        cls,
        scenes: list[SceneTranscription],
        raw_regions: list[tuple[float, float]],
        tts_speaker: str,
        segments: list[tuple[float, float, str]],
    ) -> tuple[list[SceneTranscription], list[RawSceneCandidate], list[int]]:
        """Map raw regions onto scene boundaries, splitting scenes as needed.

        Returns (updated_scenes, raw_candidates, scene_parent_indices).
        """
        if not raw_regions:
            return (
                [scene.model_copy(deep=True) for scene in scenes],
                [],
                [scene.scene_index for scene in scenes],
            )

        def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
            """Calculate overlap duration between two intervals."""
            return max(0.0, min(a_end, b_end) - max(a_start, b_start))

        new_scenes: list[SceneTranscription] = []
        scene_parent_indices: list[int] = []
        candidates: list[RawSceneCandidate] = []

        for scene in scenes:
            s_start = scene.start_time
            s_end = scene.end_time
            s_dur = s_end - s_start
            parent_scene_index = scene.scene_index

            if s_dur <= 0:
                new_scenes.append(scene.model_copy(deep=True))
                scene_parent_indices.append(parent_scene_index)
                continue

            # Calculate total overlap with raw regions
            total_raw_overlap = sum(
                _overlap(s_start, s_end, r_start, r_end)
                for r_start, r_end in raw_regions
            )
            raw_fraction = total_raw_overlap / s_dur

            if raw_fraction < 0.1:
                # Less than 10% raw — treat as TTS scene
                new_scenes.append(scene.model_copy(deep=True))
                scene_parent_indices.append(parent_scene_index)
            elif raw_fraction > 0.8:
                # More than 80% raw — whole scene is raw
                new_scenes.append(scene.model_copy(deep=True))
                scene_parent_indices.append(parent_scene_index)

                # Compute confidence from non-TTS speaker segments in this range
                non_tts_dur = sum(
                    min(end, s_end) - max(start, s_start)
                    for start, end, spk in segments
                    if spk != tts_speaker and _overlap(s_start, s_end, start, end) > 0
                )
                confidence = min(1.0, non_tts_dur / s_dur) if s_dur > 0 else 0.5

                candidates.append(RawSceneCandidate(
                    scene_index=scene.scene_index,  # will be re-indexed later
                    start_time=s_start,
                    end_time=s_end,
                    confidence=round(confidence, 3),
                    reason="non_tts_speaker" if non_tts_dur > 0 else "no_speech",
                    was_split=False,
                    original_scene_index=parent_scene_index,
                ))
            else:
                # Partial overlap — split at first raw boundary
                split_point = None
                for r_start, r_end in raw_regions:
                    # Find a raw region boundary inside this scene
                    if s_start < r_start < s_end:
                        split_point = r_start
                        break
                    if s_start < r_end < s_end:
                        split_point = r_end
                        break

                if split_point is None or (split_point - s_start) < 0.3 or (s_end - split_point) < 0.3:
                    # Can't split meaningfully — classify based on majority
                    new_scenes.append(scene.model_copy(deep=True))
                    scene_parent_indices.append(parent_scene_index)
                    if raw_fraction > 0.5:
                        candidates.append(RawSceneCandidate(
                            scene_index=scene.scene_index,
                            start_time=s_start,
                            end_time=s_end,
                            confidence=round(raw_fraction, 3),
                            reason="non_tts_speaker",
                            was_split=False,
                            original_scene_index=parent_scene_index,
                        ))
                else:
                    # Split into two sub-scenes
                    # First part: s_start → split_point
                    first_words = [w for w in scene.words if w.start < split_point]
                    first_text = " ".join(w.text for w in first_words)
                    scene_first = SceneTranscription(
                        scene_index=scene.scene_index,  # temp, re-indexed later
                        text=first_text,
                        words=[word.model_copy(deep=True) for word in first_words],
                        start_time=s_start,
                        end_time=split_point,
                        is_raw=scene.is_raw,
                    )

                    # Second part: split_point → s_end
                    second_words = [w for w in scene.words if w.start >= split_point]
                    second_text = " ".join(w.text for w in second_words)
                    scene_second = SceneTranscription(
                        scene_index=scene.scene_index + 1,  # temp
                        text=second_text,
                        words=[word.model_copy(deep=True) for word in second_words],
                        start_time=split_point,
                        end_time=s_end,
                        is_raw=scene.is_raw,
                    )

                    new_scenes.append(scene_first)
                    new_scenes.append(scene_second)
                    scene_parent_indices.append(parent_scene_index)
                    scene_parent_indices.append(parent_scene_index)

                    # Determine which sub-scene is raw
                    added_as_candidate: set[int] = set()
                    for sub_scene in [scene_first, scene_second]:
                        sub_raw_overlap = sum(
                            _overlap(sub_scene.start_time, sub_scene.end_time, r_start, r_end)
                            for r_start, r_end in raw_regions
                        )
                        sub_dur = sub_scene.end_time - sub_scene.start_time
                        if sub_dur > 0 and sub_raw_overlap / sub_dur > 0.5:
                            candidates.append(RawSceneCandidate(
                                scene_index=sub_scene.scene_index,
                                start_time=sub_scene.start_time,
                                end_time=sub_scene.end_time,
                                confidence=round(sub_raw_overlap / sub_dur, 3),
                                reason="non_tts_speaker",
                                was_split=True,
                                original_scene_index=parent_scene_index,
                            ))
                            added_as_candidate.add(id(sub_scene))

                    # Fallback: empty sub-scenes from a split are gaps in TTS
                    # that may not meet the >50% raw overlap threshold.
                    for sub_scene in [scene_first, scene_second]:
                        if id(sub_scene) not in added_as_candidate and not sub_scene.text.strip():
                            candidates.append(RawSceneCandidate(
                                scene_index=sub_scene.scene_index,
                                start_time=sub_scene.start_time,
                                end_time=sub_scene.end_time,
                                confidence=0.0,
                                reason="empty_split_gap",
                                was_split=True,
                                original_scene_index=parent_scene_index,
                            ))

        # Re-index all scenes sequentially
        for idx, s in enumerate(new_scenes):
            s.scene_index = idx

        # Update candidate scene_index to match new indices
        candidate_times: set[tuple[float, float]] = set()
        for cand in candidates:
            for s in new_scenes:
                if (abs(s.start_time - cand.start_time) < 0.01
                        and abs(s.end_time - cand.end_time) < 0.01):
                    cand.scene_index = s.scene_index
                    break
            candidate_times.add((round(cand.start_time, 2), round(cand.end_time, 2)))

        # Fallback: scenes with no text/words are definitively not TTS,
        # even if diarization didn't produce a raw region covering them
        # (e.g. trailing silence/music at the end of the audio).
        for s in new_scenes:
            key = (round(s.start_time, 2), round(s.end_time, 2))
            if key not in candidate_times and not s.text.strip() and not s.words:
                parent_scene_index = scene_parent_indices[s.scene_index]
                candidates.append(RawSceneCandidate(
                    scene_index=s.scene_index,
                    start_time=s.start_time,
                    end_time=s.end_time,
                    confidence=0.0,
                    reason="empty_no_tts",
                    was_split=False,
                    original_scene_index=parent_scene_index,
                ))

        return new_scenes, candidates, scene_parent_indices

    @classmethod
    def _get_audio_duration(cls, wav_path: Path) -> float:
        """Get audio duration in seconds."""
        import wave
        with wave.open(str(wav_path), "rb") as wf:
            return wf.getnframes() / wf.getframerate()

    @classmethod
    def detect(
        cls,
        wav_path: Path,
        scenes: list[SceneTranscription],
    ) -> tuple[list[SceneTranscription], RawSceneDetectionResult]:
        """Run raw scene detection on a WAV file.

        Args:
            wav_path: Path to 16kHz mono WAV (same as WhisperX input)
            scenes: Current scene transcriptions

        Returns:
            (updated_scenes, detection_result)
        """
        if not settings.hf_token:
            logger.info("HF_TOKEN not set, skipping raw scene detection")
            return scenes, RawSceneDetectionResult(has_raw_scenes=False)

        try:
            audio_duration = cls._get_audio_duration(wav_path)
        except Exception as exc:
            logger.warning("Failed to get audio duration: %s", exc)
            return scenes, RawSceneDetectionResult(has_raw_scenes=False)

        try:
            segments = cls._run_diarization(wav_path)
        except Exception as exc:
            logger.error("Diarization failed: %s", exc)
            return scenes, RawSceneDetectionResult(has_raw_scenes=False)

        if not segments:
            return scenes, RawSceneDetectionResult(has_raw_scenes=False)

        tts_speaker, speaker_durations = cls._identify_tts_speaker(segments)
        if not tts_speaker:
            return scenes, RawSceneDetectionResult(has_raw_scenes=False)

        raw_regions = cls._build_raw_regions(segments, tts_speaker, audio_duration)
        if not raw_regions:
            return scenes, RawSceneDetectionResult(
                has_raw_scenes=False,
                tts_speaker_id=tts_speaker,
                speaker_count=len(speaker_durations),
            )

        updated_scenes, candidates, scene_parent_indices = cls._map_raw_regions_to_scenes(
            scenes, raw_regions, tts_speaker, segments,
        )

        result = RawSceneDetectionResult(
            has_raw_scenes=len(candidates) > 0,
            candidates=candidates,
            tts_speaker_id=tts_speaker,
            speaker_count=len(speaker_durations),
            scene_parent_indices=scene_parent_indices,
        )

        return updated_scenes, result
