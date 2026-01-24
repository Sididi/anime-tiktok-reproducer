from pydantic import BaseModel


class MatchCandidate(BaseModel):
    """A candidate match from anime_searcher."""

    episode: str
    timestamp: float
    similarity: float
    series: str


class AlternativeMatch(BaseModel):
    """A pre-computed alternative match for quick selection."""

    episode: str
    start_time: float
    end_time: float
    confidence: float
    speed_ratio: float
    # Number of frame positions that voted for this episode
    vote_count: int = 0


class SceneMatch(BaseModel):
    """A confirmed match for a scene."""

    scene_index: int
    episode: str
    start_time: float  # in source episode
    end_time: float  # in source episode
    confidence: float
    speed_ratio: float  # tiktok_duration / source_duration
    confirmed: bool = False

    # Top 5 alternative matches for quick selection (Weighted Voting algorithm)
    alternatives: list[AlternativeMatch] = []

    # Candidates used for matching (for debugging/manual override)
    start_candidates: list[MatchCandidate] = []
    middle_candidates: list[MatchCandidate] = []
    end_candidates: list[MatchCandidate] = []


class MatchList(BaseModel):
    """List of matches for a project."""

    matches: list[SceneMatch] = []
