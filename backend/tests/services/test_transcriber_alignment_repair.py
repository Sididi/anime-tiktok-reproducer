from app.models import Scene, SceneList
from app.services.transcriber import TranscriberService


def _artifact_segment() -> dict:
    return {
        "start": 305.198,
        "end": 322.867,
        "text": " I'm a house teacher.",
        "words": [
            {"word": "I'm", "start": 305.198, "end": 306.42, "score": 0.265},
            {"word": "a", "start": 306.44, "end": 306.58, "score": 0.322},
            {"word": "house", "start": 306.64, "end": 308.083, "score": 0.302},
            {"word": "teacher.", "start": 308.684, "end": 322.887, "score": 0.463},
        ],
    }


def _repaired_segment() -> dict:
    words = [
        ("30", 305.24, 305.38),
        ("floors,", 305.40, 305.68),
        ("he", 305.86, 305.94),
        ("climbed", 306.00, 306.26),
        ("all", 306.34, 306.44),
        ("the", 306.48, 306.58),
        ("way", 306.64, 306.74),
        ("up.", 306.80, 306.92),
        ("The", 307.02, 307.16),
        ("most", 307.18, 307.32),
        ("ridiculous", 307.36, 307.82),
        ("part", 307.86, 308.04),
        ("was,", 308.08, 308.26),
        ("he", 308.40, 308.50),
        ("actually", 308.56, 308.88),
        ("beat", 308.92, 309.10),
        ("the", 309.14, 309.22),
        ("elevator.", 309.28, 309.56),
        ("As", 309.78, 309.88),
        ("soon", 309.94, 310.10),
        ("as", 310.14, 310.22),
        ("the", 310.24, 310.34),
        ("door", 310.40, 310.58),
        ("opened,", 310.62, 310.90),
        ("he", 310.98, 311.06),
        ("ran", 311.12, 311.28),
        ("straight", 311.34, 311.60),
        ("into", 311.66, 311.82),
        ("Jenny.", 311.88, 312.12),
        ("She", 312.20, 312.30),
        ("asked", 312.36, 312.54),
        ("him,", 312.58, 312.68),
        ("confused,", 312.78, 313.20),
        ("what", 313.28, 313.38),
        ("he", 313.42, 313.50),
        ("was", 313.54, 313.66),
        ("doing", 313.72, 313.92),
        ("there.", 313.98, 314.18),
        ("Without", 314.38, 314.62),
        ("giving", 314.68, 314.90),
        ("him", 314.96, 315.04),
        ("a", 315.08, 315.12),
        ("chance", 315.16, 315.36),
        ("to", 315.42, 315.50),
        ("explain,", 315.58, 315.90),
        ("she", 315.98, 316.08),
        ("turned", 316.16, 316.36),
        ("to", 316.42, 316.50),
        ("leave,", 316.56, 316.74),
        ("saying", 316.92, 317.14),
        ("her", 317.18, 317.28),
        ("tutor", 317.34, 317.58),
        ("would", 317.62, 317.76),
        ("arrive", 317.80, 318.04),
        ("soon.", 318.08, 318.28),
        ("At", 318.42, 318.50),
        ("that", 318.56, 318.68),
        ("moment,", 318.74, 319.02),
        ("Jack", 319.12, 319.34),
        ("finally", 319.40, 319.68),
        ("came", 319.72, 319.90),
        ("clean.", 319.94, 320.18),
    ]
    payload = [
        {"word": text, "start": start, "end": end, "score": 0.91}
        for text, start, end in words
    ]
    return {
        "start": 305.198,
        "end": 322.867,
        "text": " ".join(text for text, _, _ in words),
        "words": payload,
    }


def test_segment_needs_alignment_repair_flags_sparse_whisperx_artifact():
    assert TranscriberService._segment_needs_alignment_repair(_artifact_segment()) is True


def test_should_use_repaired_segment_accepts_dense_repair():
    assert TranscriberService._should_use_repaired_segment(
        _artifact_segment(),
        _repaired_segment(),
    ) is True


def test_should_use_repaired_segment_rejects_non_improving_candidate():
    weak_candidate = {
        "start": 305.198,
        "end": 322.867,
        "text": "I'm a teacher now",
        "words": [
            {"word": "I'm", "start": 305.198, "end": 305.9, "score": 0.62},
            {"word": "a", "start": 306.1, "end": 306.2, "score": 0.65},
            {"word": "teacher", "start": 308.2, "end": 311.8, "score": 0.58},
            {"word": "now", "start": 312.0, "end": 312.2, "score": 0.61},
        ],
    }

    assert TranscriberService._should_use_repaired_segment(
        _artifact_segment(),
        weak_candidate,
    ) is False


def test_repair_pass_replaces_only_suspect_segments(monkeypatch):
    healthy_segment = {
        "start": 10.0,
        "end": 11.0,
        "text": "healthy segment",
        "words": [
            {"word": "healthy", "start": 10.0, "end": 10.3, "score": 0.95},
            {"word": "segment", "start": 10.35, "end": 10.7, "score": 0.95},
        ],
    }

    repaired = _repaired_segment()

    def _fake_repair(cls, **kwargs):
        return repaired, kwargs["alignment_model"], kwargs["alignment_metadata"], kwargs["align_device"]

    monkeypatch.setattr(
        TranscriberService,
        "_repair_suspect_segment",
        classmethod(_fake_repair),
    )

    updated_segments, _, _, _ = TranscriberService._repair_degenerate_aligned_segments(
        segments=[_artifact_segment(), healthy_segment],
        audio=[0.0] * 100,
        model=object(),
        batch_size=1,
        detected_language="en",
        alignment_model=None,
        alignment_metadata=None,
        align_device="cpu",
    )

    assert updated_segments[0]["text"] == repaired["text"]
    assert updated_segments[1]["text"] == healthy_segment["text"]


def test_assign_words_to_scenes_populates_regression_window_after_repair():
    repaired_words = TranscriberService._extract_words_from_segments([_repaired_segment()])
    scenes = SceneList(
        scenes=[
            Scene(index=104, start_time=305.1, end_time=306.93333333333334),
            Scene(index=105, start_time=306.93333333333334, end_time=308.3666666666667),
            Scene(index=106, start_time=308.3666666666667, end_time=309.6666666666667),
            Scene(index=107, start_time=309.6666666666667, end_time=312.03333333333336),
            Scene(index=108, start_time=312.03333333333336, end_time=314.26666666666665),
            Scene(index=109, start_time=314.26666666666665, end_time=316.76666666666665),
            Scene(index=110, start_time=316.76666666666665, end_time=320.4),
            Scene(index=111, start_time=320.4, end_time=321.71909375),
        ]
    )

    scene_transcriptions = TranscriberService._assign_words_to_scenes(repaired_words, scenes)
    by_index = {scene.scene_index: scene for scene in scene_transcriptions}

    assert by_index[105].text
    assert by_index[106].text
    assert by_index[107].text
    assert by_index[108].text
    assert by_index[109].text
    assert by_index[110].text
