"""Subtitle presentation may cover a short lead-in, never a raw/empty interval."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.premiere_subtitle_baker import PremiereSubtitleBakerService
from app.services.processing import ProcessingService, SrtEntry


def scene(index, start, end, *words, raw=False):
    return SceneTranscription(
        scene_index=index, start_time=start, end_time=end, is_raw=raw,
        text=" ".join(text for text, _, _ in words),
        words=[Word(text=text, start=begin, end=finish) for text, begin, finish in words],
    )


def transcript(*scenes):
    return Transcription(language="fr", scenes=list(scenes))


@pytest.mark.parametrize("lead_in", [0.0, 0.028, 0.061, 0.204, 0.5])
def test_opening_caption_covers_short_narration_lead_in_without_retiming_words(lead_in):
    transcription = transcript(scene(
        0, 0, 2,
        ("Bonjour", lead_in, 0.6), ("monde.", 0.6, 0.9),
        ("Encore", 1, 1.3), ("ici.", 1.3, 1.6),
    ))
    original = transcription.model_dump()

    entries = ProcessingService.generate_srt_entries(transcription)

    assert entries == [
        SrtEntry(start=0, end=1, text="Bonjour monde"),
        SrtEntry(start=1, end=1.6, text="Encore ici"),
    ]
    assert transcription.model_dump() == original
    assert ProcessingService.generate_srt_entries(transcription) == entries


@pytest.mark.parametrize("lead_in", [0.5001, 1.2])
@pytest.mark.parametrize("narration_start", [0, 3])
def test_long_initial_silence_stays_blank(lead_in, narration_start):
    scenes = [scene(0, 0, narration_start, raw=True)] if narration_start else []
    scenes.append(scene(
        1, narration_start, narration_start + 3,
        ("Bonjour", narration_start + lead_in, narration_start + 2),
    ))

    entries = ProcessingService.generate_srt_entries(transcript(*scenes))

    assert entries[0].start == narration_start + lead_in


def test_consecutive_raw_introduction_never_receives_narration_captions():
    transcription = transcript(
        scene(0, 0, 1, ("Dialogue", 0.1, 0.8), raw=True),
        scene(1, 1, 3, raw=True),
        scene(2, 3, 4, ("Bonjour", 3.1, 3.6)),
    )

    assert ProcessingService.generate_srt_entries(transcription) == [
        SrtEntry(start=3, end=3.6, text="Bonjour"),
    ]


@pytest.mark.parametrize("empty_words", [(), (("...", 0.05, 0.1),)])
def test_leading_scene_without_renderable_words_is_not_a_narration_lead_in(empty_words):
    transcription = transcript(
        scene(0, 0, 0.2, *empty_words),
        scene(1, 0.2, 1, ("...", 0.21, 0.23), ("Bonjour", 0.26, 0.6)),
    )

    assert ProcessingService.generate_srt_entries(transcription) == [
        SrtEntry(start=0.2, end=0.6, text="Bonjour"),
    ]


@pytest.mark.parametrize("barrier", ["raw", "empty", "gap"])
def test_caption_blocks_stop_at_raw_empty_scenes_and_timeline_gaps(barrier):
    scenes = [scene(0, 0, 0.4, ("Avant", 0.06, 0.3), ("...", 0.31, 0.35))]
    if barrier != "gap":
        scenes.append(scene(1, 0.4, 0.7, raw=barrier == "raw"))
    scenes.append(scene(2, 0.7, 1.2, ("Après", 0.8, 1.1)))

    assert ProcessingService.generate_srt_entries(transcript(*scenes)) == [
        SrtEntry(start=0, end=0.3, text="Avant"),
        SrtEntry(start=0.7, end=1.1, text="Après"),
    ]


def test_adjacent_narration_shots_keep_existing_caption_grouping():
    transcription = transcript(
        scene(0, 0, 0.4, ("Bonjour", 0.1, 0.3)),
        scene(1, 0.4, 1, ("monde", 0.5, 0.9)),
    )

    assert ProcessingService.generate_srt_entries(transcription) == [
        SrtEntry(start=0, end=0.9, text="Bonjour monde"),
    ]


def test_later_shot_does_not_advance_its_caption_to_the_scene_cut():
    transcription = transcript(
        scene(0, 0, 0.4, ("Bonjour.", 0.1, 0.3)),
        scene(1, 0.4, 1, ("Encore.", 0.5, 0.9)),
    )

    assert ProcessingService.generate_srt_entries(transcription) == [
        SrtEntry(start=0, end=0.5, text="Bonjour"),
        SrtEntry(start=0.5, end=0.9, text="Encore"),
    ]


def test_narration_start_is_not_assumed_to_be_sequence_zero():
    transcription = transcript(scene(0, 1.5, 2.5, ("Bonjour", 1.6, 2)))

    assert ProcessingService.generate_srt_entries(transcription)[0].start == 1.5


@pytest.mark.parametrize("scenes", [[], [scene(0, 0, 3, raw=True)], [scene(0, 0, 3)]])
def test_no_narration_produces_no_narration_subtitles(scenes):
    assert ProcessingService.generate_srt(transcript(*scenes)) == ""


@pytest.mark.parametrize("preserve_audio_start", [False, True], ids=["faceless", "facecam"])
@pytest.mark.parametrize("raw_duration", [0, 3])
def test_playback_to_srt_to_mogrt_timings_share_the_narration_boundary(
    tmp_path, preserve_audio_start, raw_duration,
):
    scenes = [scene(0, 0, raw_duration, raw=True)] if raw_duration else []
    scenes.append(scene(1, 0.125, 1.5, ("Bonjour.", 0.125, 0.75)))
    final, playback = ProcessingService.build_authoritative_playback_timeline(
        transcript(*scenes), contiguous_audio_duration=1.5,
        preserve_audio_start=preserve_audio_start,
    )
    original = final.model_dump()
    audio = next(segment for segment in playback if segment.kind == "audio")
    assert audio.source_start == (0 if preserve_audio_start else 0.125)

    timing_srt = ProcessingService.render_srt_entries(ProcessingService.generate_srt_entries(final))
    path = tmp_path / "subtitle_timings.srt"
    path.write_text(timing_srt, encoding="utf-8")
    baked_entries = PremiereSubtitleBakerService.parse_srt_entries(path)

    assert baked_entries[0].start == raw_duration
    assert final.model_dump() == original
    extra = [SrtEntry(start=0.25, end=0.5, text="Dialogue")] if raw_duration else []
    combined = tmp_path / "combined.srt"
    combined.write_text(ProcessingService.generate_srt(final, extra_entries=extra), encoding="utf-8")
    combined_entries = PremiereSubtitleBakerService.parse_srt_entries(combined)
    assert combined_entries[-1].start == baked_entries[0].start
    assert combined_entries[-1].end == baked_entries[0].end
    if raw_duration:
        assert (combined_entries[0].start, combined_entries[0].end) == (0.25, 0.5)
