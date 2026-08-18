from pydantic import BaseModel


class Word(BaseModel):
    """A transcribed word with timing."""

    text: str
    start: float
    end: float
    confidence: float = 1.0


class SceneTranscription(BaseModel):
    """Transcription for a single scene."""

    scene_index: int
    text: str
    words: list[Word] = []
    start_time: float
    end_time: float
    is_raw: bool = False

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time


class Transcription(BaseModel):
    """Full transcription for a project."""

    language: str
    scenes: list[SceneTranscription] = []
    # Speech spans (seconds) the ASR pipeline could not recover words for,
    # even after the repair ladder. Empty on healthy media. Scenes inside
    # these spans are empty because of an ASR failure, not because the
    # narrator was silent.
    unrecovered_gaps: list[tuple[float, float]] = []
