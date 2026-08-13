from pydantic import BaseModel


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
    # Indexed by updated scene position; value is the pre-split parent scene index.
    scene_parent_indices: list[int] = []
    # Set when detection could not run (e.g. diarization download/auth
    # failure). Distinguishes "no raw scenes found" from "detection failed":
    # speaker_count=0 + error is a failure, never a clean result.
    error: str | None = None
