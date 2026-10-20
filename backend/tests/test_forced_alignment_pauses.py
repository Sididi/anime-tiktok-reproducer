from __future__ import annotations

import copy
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import SceneTranscription, Transcription, Word
from app.services.facecam_media_service import FacecamMediaService
from app.services.forced_alignment import ForcedAlignmentError, ForcedAlignmentService as Alignment


def manifest(scenes):
    return FacecamMediaService.manifest({"scenes": scenes})


def timed(text, start):
    return {"text": text, "start": start, "end": start + .2}


def fragments(value):
    return [(fragment["scene_index"], word)
            for segment in value["segments"] for fragment in segment["scene_fragments"]
            for word in fragment["text"].split()]


def test_split_keeps_exact_words_punctuation_scene_ownership_and_raw_boundaries():
    original = manifest([
        {"scene_index": 5, "text": "Un dragon : noir. Puis"},
        {"scene_index": 21, "text": "elle revient."},
        {"scene_index": 36, "text": ""},
        {"scene_index": 42, "text": "Un dragon noir. Puis elle revient."},
    ])
    before = copy.deepcopy(original)
    words = [timed(word, start) for word, start in [
        ("Un", .5), ("dragon", .8), ("noir.", 1.1), ("Puis", 9), ("elle", 9.3), ("revient.", 9.6),
        ("Un", 12), ("dragon", 12.3), ("noir.", 12.6), ("Puis", 17), ("elle", 17.3), ("revient.", 17.6),
    ]]
    result = Alignment.split_manifest_at_pauses(original, words)
    assert original == before
    assert fragments(result) == fragments(original)
    assert [s["text"] for s in result["segments"]] == [
        "Un dragon : noir.", "Puis elle revient.", "Un dragon noir.", "Puis elle revient."]
    assert [s["scene_indices"] for s in result["segments"]] == [[5], [5, 21], [42], [42]]
    assert [s["id"] for s in result["segments"]] == [0, 1, 2, 3]
    assert [s["source_segment_id"] for s in result["segments"]] == [0, 0, 1, 1]


@pytest.mark.parametrize("script,words,uncertain", [
    ("Un dragon noir. Puis revient.", [
        timed("Un", .5), timed("dragon", .8), timed("noir.", 1.1), timed("Puis", 9), timed("revient.", 9.3),
    ], [(4, 6)]),
    # A word absent from either sequence prevents an ungrounded split.
    ("Un dragon noir disparaît. Puis revient.", [
        timed("Un", .5), timed("dragon", .8), timed("noir", 1.1), timed("Puis", 9), timed("revient.", 9.3),
    ], []),
    ("Un dragon noir. Puis revient.", [
        timed("Un", .5), timed("dragon", .8), timed("noir.", 1.1), timed("disparaît", 4),
        timed("Puis", 9), timed("revient.", 9.3),
    ], []),
    ("Un dragon noir. Puis revient.", [
        timed("Un", .5), timed("dragon", .8), timed("noir.", 1.1), timed("Puis", 1.6), timed("revient.", 1.9),
    ], []),
    ("Un dragon noir. Puis revient.", [], []),
])
def test_uncertain_unmatched_and_short_pauses_do_not_create_windows(script, words, uncertain):
    original = manifest([{"scene_index": 7, "text": script}])
    result = Alignment.split_manifest_at_pauses(original, words, uncertain)
    assert len(result["segments"]) == 1
    assert fragments(result) == fragments(original)


def test_transformed_text_and_audio_parts_are_not_split_using_wrong_fragments():
    original = manifest([{"scene_index": 7, "text": "Un dragon noir. Puis revient."}])
    words = [timed("Un", .5), timed("dragon", .8), timed("noir.", 1.1), timed("Puis", 9), timed("revient.", 9.3)]
    original["segments"][0]["text"] = "Un dragon noir. Puis disparaît."
    assert len(Alignment.split_manifest_at_pauses(original, words)["segments"]) == 1
    original["mode"] = "audio_parts"
    assert Alignment.split_manifest_at_pauses(original, words) == original


def test_real_alignment_pipeline_recovers_long_pause_without_relaxing_quality(tmp_path, monkeypatch):
    text = "Un dragon noir. Puis elle revient."
    original = manifest([{"scene_index": 7, "text": text}])
    words = [timed(word, start) for word, start in [
        ("Un", .5), ("dragon", .8), ("noir.", 1.1), ("Puis", 9), ("elle", 9.3), ("revient.", 9.6)]]
    source = tmp_path / "voice.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(11 * 16000 * 2))
    reference = Transcription(language="fr", scenes=[
        SceneTranscription(scene_index=7, text=text, start_time=0, end_time=11)])
    windows = {}
    monkeypatch.setattr(Alignment, "_load_alignment_backend", lambda *args: (None, {}, "cpu"))
    def extract(**kwargs):
        windows[kwargs["output_path"]] = (kwargs["start_sec"], kwargs["end_sec"])
    monkeypatch.setattr(Alignment, "_extract_wav_window", extract)
    def align(**kwargs):
        start, end = windows[kwargs["clip_path"]]
        tokens = kwargs["transcript_text"].split()
        result = []
        for token in tokens:
            word = next(w for w in words if w["text"] == token)
            # Model the observed CTC artifact: the last word before a long pause
            # extends until the following word or the end of its alignment clip.
            word_end = min(end, 9) if token == "noir." else word["end"]
            result.append(Word(text=token, start=word["start"] - start, end=word_end - start))
        return result
    monkeypatch.setattr(Alignment, "_align_clip_exact", align)
    kwargs = dict(project_id="test", script_payload={"language": "fr", "scenes": [
        {"scene_index": 7, "text": text}]}, reference_transcription=reference,
        edited_audio_path=source, align_language="fr", coarse_model_size="large-v3",
        output_dir=tmp_path, coarse_words=words)
    with pytest.raises(ForcedAlignmentError) as before:
        Alignment._align_single_audio(manifest=original, **kwargs)
    assert before.value.report["segments"][0]["coverage"] == 1
    assert "Anomalously long word 'noir.'" in before.value.report["segments"][0]["issues"]

    split = Alignment.split_manifest_at_pauses(original, words)
    result = Alignment._align_single_audio(manifest=split, **kwargs)
    assert result.report["status"] == "ok"
    assert len(result.report["segments"]) == 2
    assert [s["source_segment_id"] for s in result.report["segments"]] == [0, 0]
    actual = result.transcription.scenes[0].words
    assert [w.text for w in actual] == text.split()
    assert actual[2].end < 2 and actual[3].start == pytest.approx(9)
    assert not Alignment.validate_transcription_basics(result.transcription)
    # Unresolved speech forbids splitting; the original quality guard still fails.
    uncertain = Alignment.split_manifest_at_pauses(original, words, [(2, 8)])
    with pytest.raises(ForcedAlignmentError):
        Alignment._align_single_audio(manifest=uncertain, **kwargs)
