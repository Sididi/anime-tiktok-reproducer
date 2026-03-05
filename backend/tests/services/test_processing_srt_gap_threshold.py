from app.models.transcription import SceneTranscription, Transcription, Word
from app.services.processing import ProcessingService


def _timestamp_to_seconds(raw: str) -> float:
    hh, mm, rest = raw.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


def _parse_srt_entries(srt_content: str) -> list[tuple[float, float, str]]:
    entries: list[tuple[float, float, str]] = []
    for block in srt_content.strip().split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        start_raw, end_raw = [part.strip() for part in lines[1].split("-->")]
        entries.append(
            (
                _timestamp_to_seconds(start_raw),
                _timestamp_to_seconds(end_raw),
                lines[2],
            )
        )
    return entries


def _build_transcription(words: list[Word]) -> Transcription:
    return Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=0,
                text=" ".join(word.text for word in words),
                words=words,
                start_time=words[0].start,
                end_time=words[-1].end,
            )
        ],
    )


def test_generate_srt_keeps_continuity_for_gap_below_obvious_silence_threshold() -> None:
    transcription = _build_transcription(
        [
            Word(text="Niveau", start=0.000, end=0.220, confidence=0.9),
            Word(text="cinquante", start=0.220, end=0.600, confidence=0.9),
            Word(text="puissance", start=1.061, end=1.481, confidence=0.9),
        ]
    )

    srt = ProcessingService.generate_srt(transcription, language="fr")
    entries = _parse_srt_entries(srt)

    assert entries[0][2] == "Niveau cinquante"
    assert entries[1][2] == "puissance"
    assert abs(entries[1][0] - entries[0][1]) < 1e-9


def test_generate_srt_preserves_gap_above_obvious_silence_threshold() -> None:
    transcription = _build_transcription(
        [
            Word(text="un", start=0.000, end=0.220, confidence=0.9),
            Word(text="suicide", start=0.220, end=0.700, confidence=0.9),
            Word(text="collectif", start=1.201, end=1.681, confidence=0.9),
        ]
    )

    srt = ProcessingService.generate_srt(transcription, language="fr")
    entries = _parse_srt_entries(srt)

    assert entries[0][2] == "un suicide"
    assert entries[1][2] == "collectif"
    assert entries[1][0] - entries[0][1] > 0.49
