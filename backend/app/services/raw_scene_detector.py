"""Raw scene detection using pyannote speaker diarization.

Identifies segments where TTS narration stops and raw anime audio plays
(character dialogue, music, sound effects).
"""

import bisect
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
# Minimum duration of a sub-scene produced by splitting (seconds)
MIN_SUBSCENE_DURATION = 0.3
# How far a diarization boundary may be moved to match word timings (seconds).
# pyannote turn boundaries are typically off by up to a few hundred ms,
# with onsets biased late — which is what clips the narrator's first words.
BOUNDARY_SNAP_WINDOW = 0.6
# Safety margin kept between narration words and raw region edges (seconds)
RAW_EDGE_MARGIN = 0.08
# Words below this confidence (e.g. interpolated timings from
# _fill_missing_word_times) are not precise enough to move boundaries
MIN_SNAP_WORD_CONFIDENCE = 0.3
# WhisperX artifact guard: words with implausibly long durations carry
# trailing silence, so their start time is the only reliable anchor
MAX_WORD_DURATION = 1.0


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

    @staticmethod
    def _snap_regions_to_words(
        raw_regions: list[tuple[float, float]],
        words: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Snap raw region boundaries to transcribed word timings.

        Diarization boundaries are only accurate to a few hundred
        milliseconds, while WhisperX word timestamps are near-exact for
        narration. A raw region must never overlap narration, so each
        boundary is corrected against nearby words:

        - end: a word starting shortly before the region end means the
          narrator resumed earlier than diarization noticed — pull the
          end back to just before that word.
        - start: a word ending shortly after the region start means the
          narrator was still speaking — push the start to just after it.

        Corrections are bounded by BOUNDARY_SNAP_WINDOW around the original
        boundary, so words deep inside a region (e.g. anime dialogue picked
        up by WhisperX) cannot collapse it.
        """
        if not words:
            return raw_regions

        snapped: list[tuple[float, float]] = []
        for orig_start, orig_end in raw_regions:
            r_start, r_end = orig_start, orig_end
            for w_start, w_end in words:
                if orig_start < w_end <= orig_start + BOUNDARY_SNAP_WINDOW:
                    r_start = max(r_start, w_end + RAW_EDGE_MARGIN)
                if orig_end - BOUNDARY_SNAP_WINDOW <= w_start < orig_end:
                    r_end = min(r_end, w_start - RAW_EDGE_MARGIN)
            if r_end - r_start >= MIN_RAW_DURATION:
                snapped.append((r_start, r_end))
        return snapped

    @staticmethod
    def _pick_split_points(
        s_start: float,
        s_end: float,
        raw_regions: list[tuple[float, float]],
    ) -> list[float]:
        """Collect raw region boundaries falling inside a scene.

        Keeps only points that leave every resulting sub-scene at least
        MIN_SUBSCENE_DURATION long.
        """
        points: list[float] = []
        for r_start, r_end in raw_regions:
            for point in (r_start, r_end):
                if s_start < point < s_end:
                    points.append(point)
        points.sort()

        kept: list[float] = []
        prev = s_start
        for point in points:
            if point - prev >= MIN_SUBSCENE_DURATION and s_end - point >= MIN_SUBSCENE_DURATION:
                kept.append(point)
                prev = point
        return kept

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

        def _raw_fraction(start: float, end: float) -> float:
            dur = end - start
            if dur <= 0:
                return 0.0
            return sum(
                _overlap(start, end, r_start, r_end)
                for r_start, r_end in raw_regions
            ) / dur

        new_scenes: list[SceneTranscription] = []
        scene_parent_indices: list[int] = []
        # Candidates paired with their scene object; scene_index is assigned
        # after the final re-indexing pass.
        pending_candidates: list[tuple[RawSceneCandidate, SceneTranscription]] = []

        def _add_scene(scene_obj: SceneTranscription, parent_index: int) -> None:
            new_scenes.append(scene_obj)
            scene_parent_indices.append(parent_index)

        def _add_candidate(
            scene_obj: SceneTranscription,
            parent_index: int,
            confidence: float,
            reason: str,
            was_split: bool,
        ) -> None:
            pending_candidates.append((
                RawSceneCandidate(
                    scene_index=0,  # assigned after re-indexing
                    start_time=scene_obj.start_time,
                    end_time=scene_obj.end_time,
                    confidence=round(confidence, 3),
                    reason=reason,
                    was_split=was_split,
                    original_scene_index=parent_index,
                ),
                scene_obj,
            ))

        for scene in scenes:
            s_start = scene.start_time
            s_end = scene.end_time
            s_dur = s_end - s_start
            parent_scene_index = scene.scene_index

            if s_dur <= 0:
                _add_scene(scene.model_copy(deep=True), parent_scene_index)
                continue

            raw_fraction = _raw_fraction(s_start, s_end)

            if raw_fraction < 0.1:
                # Less than 10% raw — treat as TTS scene
                _add_scene(scene.model_copy(deep=True), parent_scene_index)
                continue

            if raw_fraction > 0.8 and not scene.words:
                # Almost entirely raw with no narration words to protect —
                # take the whole scene. Scenes that DO carry words fall
                # through to the split path so trailing narration (the
                # narrator resuming) is never swallowed into a raw scene.
                copied = scene.model_copy(deep=True)
                _add_scene(copied, parent_scene_index)

                # Compute confidence from non-TTS speaker segments in this range
                non_tts_dur = sum(
                    _overlap(s_start, s_end, start, end)
                    for start, end, spk in segments
                    if spk != tts_speaker
                )
                confidence = min(1.0, non_tts_dur / s_dur)
                _add_candidate(
                    copied,
                    parent_scene_index,
                    confidence,
                    "non_tts_speaker" if non_tts_dur > 0 else "no_speech",
                    was_split=False,
                )
                continue

            # Mixed scene — split at every raw boundary inside it, so a
            # TTS→raw→TTS scene yields three sub-scenes instead of lumping
            # the resumed narration in with the raw part.
            split_points = cls._pick_split_points(s_start, s_end, raw_regions)

            if not split_points:
                # Can't split meaningfully — classify based on majority
                copied = scene.model_copy(deep=True)
                _add_scene(copied, parent_scene_index)
                if raw_fraction > 0.5:
                    _add_candidate(
                        copied,
                        parent_scene_index,
                        raw_fraction,
                        "non_tts_speaker",
                        was_split=False,
                    )
                continue

            boundaries = [s_start, *split_points, s_end]
            sub_words: list[list] = [[] for _ in range(len(boundaries) - 1)]
            for word in scene.words:
                # Assign by midpoint, except abnormally long words (WhisperX
                # artifact with trailing silence) where start is reliable.
                if word.end - word.start > MAX_WORD_DURATION:
                    anchor = word.start
                else:
                    anchor = (word.start + word.end) / 2
                slot = bisect.bisect_right(boundaries, anchor) - 1
                slot = min(max(slot, 0), len(sub_words) - 1)
                sub_words[slot].append(word.model_copy(deep=True))

            for i in range(len(boundaries) - 1):
                sub_start, sub_end = boundaries[i], boundaries[i + 1]
                sub_scene = SceneTranscription(
                    scene_index=0,  # re-indexed later
                    text=" ".join(w.text for w in sub_words[i]),
                    words=sub_words[i],
                    start_time=sub_start,
                    end_time=sub_end,
                    is_raw=scene.is_raw,
                )
                _add_scene(sub_scene, parent_scene_index)

                sub_fraction = _raw_fraction(sub_start, sub_end)
                if sub_fraction > 0.5:
                    _add_candidate(
                        sub_scene,
                        parent_scene_index,
                        sub_fraction,
                        "non_tts_speaker",
                        was_split=True,
                    )
                elif not sub_scene.text.strip():
                    # Empty sub-scenes from a split are gaps in TTS that
                    # may not meet the >50% raw overlap threshold.
                    _add_candidate(
                        sub_scene,
                        parent_scene_index,
                        0.0,
                        "empty_split_gap",
                        was_split=True,
                    )

        # Re-index all scenes sequentially
        for idx, s in enumerate(new_scenes):
            s.scene_index = idx

        candidates: list[RawSceneCandidate] = []
        scenes_with_candidates: set[int] = set()
        for cand, scene_obj in pending_candidates:
            cand.scene_index = scene_obj.scene_index
            candidates.append(cand)
            scenes_with_candidates.add(id(scene_obj))

        # Fallback: scenes with no text/words are definitively not TTS,
        # even if diarization didn't produce a raw region covering them
        # (e.g. trailing silence/music at the end of the audio).
        for s in new_scenes:
            if id(s) not in scenes_with_candidates and not s.text.strip() and not s.words:
                candidates.append(RawSceneCandidate(
                    scene_index=s.scene_index,
                    start_time=s.start_time,
                    end_time=s.end_time,
                    confidence=0.0,
                    reason="empty_no_tts",
                    was_split=False,
                    original_scene_index=scene_parent_indices[s.scene_index],
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

        # Correct imprecise diarization boundaries against WhisperX word
        # timings so raw regions never clip narration (the narrator's first
        # words after a raw scene were getting swallowed by late pyannote
        # onsets).
        narration_words = sorted(
            (word.start, word.end)
            for scene in scenes
            for word in scene.words
            if word.confidence >= MIN_SNAP_WORD_CONFIDENCE and word.end > word.start
        )
        raw_regions = cls._snap_regions_to_words(raw_regions, narration_words)

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
