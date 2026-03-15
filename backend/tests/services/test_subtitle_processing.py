from app.models.transcription import Word
from app.services.forced_alignment import ForcedAlignmentService
from app.services.processing import ProcessingService
from app.services.script_automation_service import ScriptAutomationService


def test_normalize_external_subtitle_text_strips_markup_and_ass_controls():
    markup = '<font face="Trebuchet MS" size="22"><b>Je l\'ai rejoint,</b></font>'
    assert ProcessingService._normalize_external_subtitle_text(markup) == "Je l'ai rejoint,"

    ass_markup = r"{\an8}Texte\Nsuite &amp; fin"
    assert ProcessingService._normalize_external_subtitle_text(ass_markup) == "Texte suite & fin"


def test_tts_segmentation_preserves_scene_fragments_for_hard_split(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 20)

    scene_text = "forces, he was ready to pay the price. After this crazy race,"
    payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 35,
                "text": scene_text,
            }
        ],
    }

    segments = ScriptAutomationService._segment_scenes_for_tts_payload(payload)

    assert len(segments) == 2
    assert segments[0]["scene_indices"] == [35]
    assert segments[1]["scene_indices"] == [35]
    assert segments[0]["scene_fragments"][0]["scene_index"] == 35
    assert segments[1]["scene_fragments"][0]["scene_index"] == 35

    reconstructed = "".join(
        fragment["text"]
        for segment in segments
        for fragment in segment["scene_fragments"]
        if fragment["scene_index"] == 35
    )
    assert reconstructed == scene_text


def test_fragment_based_alignment_maps_one_scene_across_adjacent_segments(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 40)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 20)

    scene_text = "forces, he was ready to pay the price. After this crazy race,"
    script_payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 35,
                "text": scene_text,
            }
        ],
    }

    segments = ScriptAutomationService._segment_scenes_for_tts_payload(script_payload)
    combined_scene_words: list[Word] = []
    current_time = 0.0

    for segment in segments:
        segment_words: list[Word] = []
        for token in str(segment["text"]).split():
            segment_words.append(
                Word(
                    text=token,
                    start=current_time,
                    end=current_time + 0.1,
                    confidence=1.0,
                )
            )
            current_time += 0.12

        report, scene_payload = ForcedAlignmentService._map_segment_words_to_scenes(
            segment=segment,
            segment_words=segment_words,
            script_payload=script_payload,
        )

        assert report["coverage"] == 1.0
        assert report["scene_fragments"] == segment["scene_fragments"]
        combined_scene_words.extend(scene_payload[35])

    assert [word.text for word in combined_scene_words] == scene_text.split()


def test_validate_manifest_upgrades_legacy_segment_indices_when_text_matches(monkeypatch):
    monkeypatch.setattr(ScriptAutomationService, "TTS_HARD_MAX", 35)
    monkeypatch.setattr(ScriptAutomationService, "TTS_SOFT_MAX", 35)
    monkeypatch.setattr(ScriptAutomationService, "TTS_MIN", 1)
    monkeypatch.setattr(ScriptAutomationService, "TTS_TARGET", 18)

    script_payload = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 1,
                "text": "Intro line",
            },
            {
                "scene_index": 2,
                "text": "Second scene keeps going well past the segment boundary.",
            },
        ],
    }

    expected_manifest = ForcedAlignmentService.build_single_audio_manifest(script_payload=script_payload)
    legacy_manifest = {
        "version": 1,
        "mode": "audio_parts",
        "language": "fr",
        "segments": [
            {
                **segment,
                "scene_indices": [1] if segment["id"] == 1 else list(segment["scene_indices"]),
            }
            for segment in expected_manifest["segments"]
        ],
    }
    for segment in legacy_manifest["segments"]:
        segment.pop("scene_fragments", None)

    validated = ForcedAlignmentService._validate_manifest_against_script(
        manifest=legacy_manifest,
        script_payload=script_payload,
    )

    assert validated["segments"][0]["scene_indices"] == [1, 2]
    assert any(
        fragment["scene_index"] == 2
        for fragment in validated["segments"][0]["scene_fragments"]
    )
