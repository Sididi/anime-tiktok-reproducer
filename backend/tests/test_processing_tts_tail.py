"""The final TTS audio window must not stop at the aligner's last-word end.

Every scene except the last takes its audio window up to the next scene's first
word, so nothing is ever dropped mid-script. The last scene had no successor and
fell back to ``words[-1].end``, which discarded the real release of the closing
word (measured median 200 ms over 118 projects) and left a hard step into
digital silence — the audible "voice cut off on the last word".
"""

from __future__ import annotations

import sys
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.processing import PlaybackAudioSegment, ProcessingService


def _scene(index: int, words: list[tuple[float, float]], *, is_raw: bool = False) -> SceneTranscription:
    return SceneTranscription(
        scene_index=index,
        text=f"scene {index}",
        words=[
            Word(text=f"w{i}", start=start, end=end, confidence=0.9)
            for i, (start, end) in enumerate(words)
        ],
        start_time=words[0][0] if words else 0.0,
        end_time=words[-1][1] if words else 0.0,
        is_raw=is_raw,
    )


def _write_wav(path: Path, samples: list[int], *, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array("h", samples).tobytes())


def _read_samples(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    values = array("h")
    values.frombytes(raw)
    return list(values)


def test_last_scene_audio_window_reaches_end_of_contiguous_audio() -> None:
    transcription = Transcription(
        language="fr",
        scenes=[
            _scene(0, [(0.0, 0.8)]),
            _scene(1, [(1.2, 2.0)]),
        ],
    )

    _, segments = ProcessingService.build_authoritative_playback_timeline(
        transcription,
        contiguous_audio_duration=2.2,
    )

    assert segments[-1].source_end == 2.2


def test_earlier_scenes_keep_the_next_scene_start_rule() -> None:
    transcription = Transcription(
        language="fr",
        scenes=[
            _scene(0, [(0.0, 0.8)]),
            _scene(1, [(1.2, 2.0)]),
        ],
    )

    _, segments = ProcessingService.build_authoritative_playback_timeline(
        transcription,
        contiguous_audio_duration=2.2,
    )

    assert segments[0].source_end == 1.2


def test_last_scene_window_is_never_shortened_by_a_smaller_audio_duration() -> None:
    transcription = Transcription(
        language="fr",
        scenes=[_scene(0, [(0.0, 2.0)])],
    )

    _, segments = ProcessingService.build_authoritative_playback_timeline(
        transcription,
        contiguous_audio_duration=1.5,
    )

    assert segments[-1].source_end == 2.0


def test_trailing_raw_scene_does_not_take_the_extension() -> None:
    transcription = Transcription(
        language="fr",
        scenes=[
            _scene(0, [(0.0, 2.0)]),
            _scene(1, [], is_raw=True),
        ],
    )
    transcription.scenes[1].start_time = 0.0
    transcription.scenes[1].end_time = 3.0

    _, segments = ProcessingService.build_authoritative_playback_timeline(
        transcription,
        contiguous_audio_duration=2.2,
    )

    audio_segments = [segment for segment in segments if segment.kind == "audio"]
    assert audio_segments[-1].source_end == 2.2
    assert segments[-1].kind == "silence"


def test_final_audio_segment_is_faded_out(tmp_path: Path) -> None:
    rate = 44100
    source = tmp_path / "contiguous.wav"
    _write_wav(source, [10000] * rate, rate=rate)

    output = tmp_path / "final.wav"
    ProcessingService.rebuild_tts_audio_with_playback_segments(
        source,
        output,
        [
            PlaybackAudioSegment(
                scene_index=0,
                kind="audio",
                duration=1.0,
                source_start=0.0,
                source_end=1.0,
            )
        ],
    )

    samples = _read_samples(output)
    assert len(samples) == rate
    assert abs(samples[-1]) <= 200, "audio must ramp to silence instead of a hard step"
    assert samples[rate // 2] == 10000, "only the tail may be attenuated"


def test_fade_applies_before_trailing_silence(tmp_path: Path) -> None:
    rate = 44100
    source = tmp_path / "contiguous.wav"
    _write_wav(source, [10000] * rate, rate=rate)

    output = tmp_path / "final.wav"
    ProcessingService.rebuild_tts_audio_with_playback_segments(
        source,
        output,
        [
            PlaybackAudioSegment(
                scene_index=0,
                kind="audio",
                duration=1.0,
                source_start=0.0,
                source_end=1.0,
            ),
            PlaybackAudioSegment(
                scene_index=1,
                kind="silence",
                duration=0.5,
            ),
        ],
    )

    samples = _read_samples(output)
    assert abs(samples[rate - 1]) <= 200, "the spoken tail must fade before the silence"
    assert samples[rate // 2] == 10000
