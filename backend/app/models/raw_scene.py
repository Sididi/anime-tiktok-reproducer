from typing import Literal

from pydantic import BaseModel


class DiarizationAnalysis(BaseModel):
    """Reusable model output; speaker labels here are never merged in place."""

    version: Literal[1] = 1
    audio_duration: float = 0
    segments: list[tuple[float, float, str]] = []
    centroids: dict[str, list[float]] = {}
    pitches: dict[str, float] = {}
    warning: str | None = None
    error: str | None = None


class RawSceneCandidate(BaseModel):
    """A detected raw (non-TTS) scene candidate."""

    scene_index: int
    start_time: float
    end_time: float
    confidence: float
    reason: str  # "no_speech" | "non_tts_speaker" | "empty_split_gap" | "empty_no_tts"
    was_split: bool = False
    original_scene_index: int | None = None


class RawSceneDetectionResult(BaseModel):
    """Result of raw scene detection via speaker diarization."""

    has_raw_scenes: bool
    candidates: list[RawSceneCandidate] = []
    tts_speaker_id: str = ""
    speaker_count: int = 0
    analysis_id: str | None = None
    selection_origin: Literal["automatic", "manual"] = "automatic"
    # Explicit narration voices (or the automatic winner), before same-voice merging.
    selected_narrator_ids: list[str] = []
    # Indexed by updated scene position; value is the pre-split parent scene index.
    scene_parent_indices: list[int] = []
    # Diarization clusters folded into the TTS speaker because their voice
    # embedding matched it (same narrator split by the diarizer).
    merged_speaker_ids: list[str] = []
    # Set when detection could not run (e.g. diarization download/auth
    # failure). Distinguishes "no raw scenes found" from "detection failed":
    # speaker_count=0 + error is a failure, never a clean result.
    error: str | None = None
