"""Tests for raw scene detection region building, boundary snapping and
scene mapping.

The regression scenario throughout: pyannote turn boundaries are imprecise
(onsets biased late), which used to swallow the narrator's first resumed
words into the end of raw scenes.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.transcription import SceneTranscription, Word
from app.services.raw_scene_detector import (
    RAW_EDGE_MARGIN,
    RawSceneDetectorService,
)

TTS = "SPEAKER_00"
OTHER = "SPEAKER_01"


def _word(text: str, start: float, end: float, confidence: float = 1.0) -> Word:
    return Word(text=text, start=start, end=end, confidence=confidence)


def _scene(
    index: int,
    start: float,
    end: float,
    words: list[Word] | None = None,
) -> SceneTranscription:
    words = words or []
    return SceneTranscription(
        scene_index=index,
        text=" ".join(w.text for w in words),
        words=words,
        start_time=start,
        end_time=end,
    )


# ---------------------------------------------------------------------------
# _build_raw_regions
# ---------------------------------------------------------------------------

class TestBuildRawRegions:
    def test_gaps_between_tts_become_raw_regions(self):
        segments = [(0.0, 5.0, TTS), (10.0, 15.0, TTS)]
        regions = RawSceneDetectorService._build_raw_regions(segments, TTS, 15.0)
        assert regions == [(5.0, 10.0)]

    def test_trailing_region_after_last_tts(self):
        segments = [(0.0, 5.0, TTS)]
        regions = RawSceneDetectorService._build_raw_regions(segments, TTS, 12.0)
        assert regions == [(5.0, 12.0)]

    def test_short_gaps_are_ignored(self):
        segments = [(0.0, 5.0, TTS), (5.3, 10.0, TTS)]
        regions = RawSceneDetectorService._build_raw_regions(segments, TTS, 10.0)
        assert regions == []

    def test_non_tts_segments_do_not_shrink_raw_regions(self):
        segments = [(0.0, 5.0, TTS), (6.0, 9.0, OTHER), (10.0, 15.0, TTS)]
        regions = RawSceneDetectorService._build_raw_regions(segments, TTS, 15.0)
        assert regions == [(5.0, 10.0)]


# ---------------------------------------------------------------------------
# _snap_regions_to_words
# ---------------------------------------------------------------------------

class TestSnapRegionsToWords:
    def test_late_end_pulled_back_to_resumed_word(self):
        # Diarization noticed the narrator 0.4s late; the resumed word
        # starts at 15.0 and must not be inside the raw region.
        regions = [(10.0, 15.4)]
        words = [(15.0, 15.3)]
        snapped = RawSceneDetectorService._snap_regions_to_words(regions, words)
        assert snapped == [(10.0, 15.0 - RAW_EDGE_MARGIN)]

    def test_word_straddling_region_end_is_excluded(self):
        regions = [(10.0, 15.4)]
        words = [(15.2, 15.7)]
        snapped = RawSceneDetectorService._snap_regions_to_words(regions, words)
        assert snapped == [(10.0, 15.2 - RAW_EDGE_MARGIN)]

    def test_early_start_pushed_past_trailing_word(self):
        # Diarization cut the TTS turn early; the narrator's last word
        # ends at 10.3, inside the region start.
        regions = [(10.0, 15.0)]
        words = [(9.8, 10.3)]
        snapped = RawSceneDetectorService._snap_regions_to_words(regions, words)
        assert snapped == [(10.3 + RAW_EDGE_MARGIN, 15.0)]

    def test_word_deep_inside_region_does_not_collapse_it(self):
        # e.g. anime dialogue picked up by WhisperX mid-region: outside the
        # snap window, so the region is untouched.
        regions = [(10.0, 15.0)]
        words = [(12.0, 12.4)]
        snapped = RawSceneDetectorService._snap_regions_to_words(regions, words)
        assert snapped == [(10.0, 15.0)]

    def test_words_outside_region_are_ignored(self):
        regions = [(10.0, 15.0)]
        words = [(8.0, 9.5), (16.0, 16.4)]
        snapped = RawSceneDetectorService._snap_regions_to_words(regions, words)
        assert snapped == [(10.0, 15.0)]

    def test_region_dropped_when_too_short_after_snapping(self):
        regions = [(10.0, 10.8)]
        words = [(9.9, 10.5)]
        snapped = RawSceneDetectorService._snap_regions_to_words(regions, words)
        assert snapped == []

    def test_no_words_returns_regions_unchanged(self):
        regions = [(10.0, 15.0)]
        assert RawSceneDetectorService._snap_regions_to_words(regions, []) == regions


# ---------------------------------------------------------------------------
# _pick_split_points
# ---------------------------------------------------------------------------

class TestPickSplitPoints:
    def test_boundaries_inside_scene_are_kept_in_order(self):
        points = RawSceneDetectorService._pick_split_points(
            0.0, 10.0, [(2.0, 8.0)],
        )
        assert points == [2.0, 8.0]

    def test_points_too_close_to_scene_edges_are_dropped(self):
        points = RawSceneDetectorService._pick_split_points(
            0.0, 10.0, [(0.1, 9.95)],
        )
        assert points == []

    def test_points_too_close_together_are_thinned(self):
        points = RawSceneDetectorService._pick_split_points(
            0.0, 10.0, [(2.0, 2.1), (5.0, 8.0)],
        )
        assert points == [2.0, 5.0, 8.0]


# ---------------------------------------------------------------------------
# _map_raw_regions_to_scenes
# ---------------------------------------------------------------------------

class TestMapRawRegionsToScenes:
    def test_tts_raw_tts_scene_splits_into_three(self):
        words = [
            _word("The", 0.2, 0.5),
            _word("hero", 0.6, 1.4),
            _word("Then", 8.2, 8.6),
            _word("suddenly", 8.7, 9.5),
        ]
        scenes = [_scene(0, 0.0, 10.0, words)]
        segments = [(0.0, 2.0, TTS), (8.0, 10.0, TTS)]
        raw_regions = [(2.0, 8.0)]

        new_scenes, candidates, parents = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, segments,
            )
        )

        assert len(new_scenes) == 3
        assert [s.scene_index for s in new_scenes] == [0, 1, 2]
        assert parents == [0, 0, 0]

        first, middle, last = new_scenes
        assert first.text == "The hero"
        assert middle.text == ""
        assert last.text == "Then suddenly"

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.scene_index == 1
        assert cand.was_split is True
        assert cand.reason == "non_tts_speaker"
        assert (cand.start_time, cand.end_time) == (2.0, 8.0)

    def test_mostly_raw_scene_with_trailing_narration_is_split(self):
        # Regression: a >80% raw scene used to be marked raw wholesale,
        # swallowing the narrator's first resumed words at its end.
        words = [
            _word("Then", 9.1, 9.4),
            _word("suddenly", 9.5, 9.9),
        ]
        scenes = [_scene(0, 0.0, 10.0, words)]
        segments = [(9.0, 10.0, TTS)]
        raw_regions = [(0.0, 9.0)]

        new_scenes, candidates, _ = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, segments,
            )
        )

        assert len(new_scenes) == 2
        raw_part, tts_part = new_scenes
        assert raw_part.text == ""
        assert tts_part.text == "Then suddenly"

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.scene_index == 0
        assert cand.end_time == 9.0  # narration words stay outside the raw scene

    def test_wordless_fully_raw_scene_taken_whole(self):
        scenes = [_scene(0, 5.0, 10.0)]
        segments = [(0.0, 5.0, TTS)]
        raw_regions = [(5.0, 10.0)]

        new_scenes, candidates, _ = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, segments,
            )
        )

        assert len(new_scenes) == 1
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.was_split is False
        assert cand.reason == "no_speech"
        assert (cand.start_time, cand.end_time) == (5.0, 10.0)

    def test_non_tts_speaker_raises_confidence(self):
        scenes = [_scene(0, 5.0, 10.0)]
        segments = [(0.0, 5.0, TTS), (5.5, 9.5, OTHER)]
        raw_regions = [(5.0, 10.0)]

        _, candidates, _ = RawSceneDetectorService._map_raw_regions_to_scenes(
            scenes, raw_regions, TTS, segments,
        )

        assert candidates[0].reason == "non_tts_speaker"
        assert candidates[0].confidence == 0.8  # 4s of OTHER over 5s

    def test_mostly_tts_scene_untouched(self):
        words = [_word("hello", 0.5, 1.0)]
        scenes = [_scene(0, 0.0, 10.0, words)]
        raw_regions = [(9.5, 10.0)]

        new_scenes, candidates, parents = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, [(0.0, 9.5, TTS)],
            )
        )

        assert len(new_scenes) == 1
        assert new_scenes[0].text == "hello"
        assert candidates == []
        assert parents == [0]

    def test_no_raw_regions_passthrough(self):
        scenes = [_scene(3, 0.0, 5.0, [_word("a", 0.1, 0.4)])]
        new_scenes, candidates, parents = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, [], TTS, [],
            )
        )
        assert len(new_scenes) == 1
        assert candidates == []
        assert parents == [3]

    def test_empty_split_gap_candidate_for_low_overlap_gap(self):
        # Sub-scene [2.0, 3.0] has no words and no raw overlap: it must
        # still surface as an empty_split_gap candidate.
        words = [_word("intro", 0.2, 1.0)]
        scenes = [_scene(0, 0.0, 3.0, words)]
        raw_regions = [(1.2, 2.0)]

        new_scenes, candidates, _ = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, [(0.0, 1.2, TTS)],
            )
        )

        assert len(new_scenes) == 3
        reasons = {c.scene_index: c.reason for c in candidates}
        assert reasons[1] == "non_tts_speaker"
        assert reasons[2] == "empty_split_gap"

    def test_reindexing_across_multiple_scenes_with_split(self):
        words_a = [_word("one", 0.2, 0.8)]
        words_b = [
            _word("two", 10.2, 10.8),
            _word("three", 18.5, 19.0),
        ]
        scenes = [
            _scene(0, 0.0, 10.0, words_a),
            _scene(1, 10.0, 20.0, words_b),
        ]
        raw_regions = [(11.0, 18.0)]
        segments = [(0.0, 11.0, TTS), (18.0, 20.0, TTS)]

        new_scenes, candidates, parents = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, segments,
            )
        )

        # Scene 0 untouched, scene 1 split into three
        assert [s.scene_index for s in new_scenes] == [0, 1, 2, 3]
        assert parents == [0, 1, 1, 1]
        assert len(candidates) == 1
        assert candidates[0].scene_index == 2
        assert candidates[0].original_scene_index == 1

    def test_unsplittable_majority_raw_scene_still_flagged(self):
        # Raw region covers the scene entirely: no boundary inside, so the
        # scene can't be split — majority classification applies.
        words = [_word("ghost", 5.1, 5.4, confidence=0.1)]
        scenes = [_scene(0, 5.0, 10.0, words)]
        raw_regions = [(4.0, 11.0)]

        new_scenes, candidates, _ = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, [],
            )
        )

        assert len(new_scenes) == 1
        assert len(candidates) == 1
        assert candidates[0].was_split is False

    def test_trailing_empty_scene_without_region_flagged_empty_no_tts(self):
        scenes = [
            _scene(0, 0.0, 5.0, [_word("talk", 0.2, 4.8)]),
            _scene(1, 5.0, 9.0),  # no words, not covered by any raw region
        ]
        raw_regions = [(9.0, 12.0)]

        _, candidates, _ = RawSceneDetectorService._map_raw_regions_to_scenes(
            scenes, raw_regions, TTS, [(0.0, 9.0, TTS)],
        )

        empties = [c for c in candidates if c.reason == "empty_no_tts"]
        assert len(empties) == 1
        assert empties[0].scene_index == 1


# ---------------------------------------------------------------------------
# End-to-end symptom regression: snap + map together
# ---------------------------------------------------------------------------

class TestNarratorResumeRegression:
    def test_resumed_words_never_land_in_raw_candidate(self):
        """Diarization reports the narrator's resume 0.35s late; the raw
        candidate must still end before the first resumed word."""
        words = [
            _word("Before", 10.2, 10.9),
            _word("After", 15.0, 15.4),   # narrator resumes here
            _word("that", 15.5, 15.9),
        ]
        scenes = [_scene(0, 10.0, 20.0, words)]

        # Diarization: TTS turn resumes at 15.35 (0.35s late)
        segments = [(0.0, 11.0, TTS), (15.35, 20.0, TTS)]
        raw_regions = RawSceneDetectorService._build_raw_regions(
            segments, TTS, 20.0,
        )
        assert raw_regions == [(11.0, 15.35)]

        narration_words = [(w.start, w.end) for w in words]
        raw_regions = RawSceneDetectorService._snap_regions_to_words(
            raw_regions, narration_words,
        )
        assert raw_regions == [(11.0, 15.0 - RAW_EDGE_MARGIN)]

        new_scenes, candidates, _ = (
            RawSceneDetectorService._map_raw_regions_to_scenes(
                scenes, raw_regions, TTS, segments,
            )
        )

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.end_time <= 15.0
        # Resumed words live in a non-raw sub-scene
        raw_scene = next(s for s in new_scenes if s.scene_index == cand.scene_index)
        assert raw_scene.text == ""
        resumed = next(s for s in new_scenes if "After" in s.text)
        assert resumed.scene_index != cand.scene_index
        assert resumed.text == "After that"
