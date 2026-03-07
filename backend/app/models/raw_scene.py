from pydantic import BaseModel


class RawSceneCandidate(BaseModel):
    """A detected raw (non-TTS) scene candidate."""

    scene_index: int
    start_time: float
    end_time: float
    confidence: float
    reason: str  # "no_speech" | "non_tts_speaker"
    was_split: bool = False
    original_scene_index: int | None = None


class RawSceneDetectionResult(BaseModel):
    """Result of raw scene detection via speaker diarization."""

    has_raw_scenes: bool
    candidates: list[RawSceneCandidate] = []
    tts_speaker_id: str = ""
    speaker_count: int = 0
