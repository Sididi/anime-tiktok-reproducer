from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.facecam_cut_service import validate_suggestions
from app.services.facecam_cut_timing import cached_cut_timing, pause_midpoint, recovery_windows, refine_cut_timing
from app.services.transcriber import ASRResult, TranscriberService


@pytest.fixture
def collapsed_take(tmp_path):
    timing = {"words": [
        {"text": "Avant.", "start": 0, "end": .3},
        {"text": "Il", "start": 2, "end": 2.3},
        {"text": "découvre", "start": 2.4, "end": 2.7},
        {"text": "un", "start": 2.8, "end": 3.1},
        {"text": "nouveau", "start": 3.2, "end": 3.5},
        {"text": "pouvoir", "start": 3.6, "end": 6.4},
        {"text": "incroyable.", "start": 6.5, "end": 6.9},
        {"text": "Après.", "start": 10, "end": 10.4},
    ], "uncertain_spans": [[11, 11.2]]}
    (tmp_path / 'speech_timing.json').write_text(json.dumps(timing))
    samples = np.full(12 * 48000, 8000, dtype='<i2')
    samples[round(4.1 * 48000):round(5.1 * 48000)] = 0
    with wave.open(str(tmp_path / 'voice.wav'), 'wb') as stream:
        stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.tobytes())
    return tmp_path, timing


def test_recovered_attempts_keep_source_clock_and_remain_unchecked(collapsed_take, monkeypatch):
    root, timing = collapsed_take
    source_timing, source_audio = (root / 'speech_timing.json').read_bytes(), (root / 'voice.wav').read_bytes()
    clips = []

    def transcribe(path, language, model):
        with wave.open(str(path), 'rb') as stream:
            clips.append(stream.getnframes() / stream.getframerate())
        # These are independent ASR results on either side of the real pause.
        first = len(clips) == 1
        text = 'Il découvre un nouveau pouvoir' if first else 'Il découvre un nouveau pouvoir incroyable'
        offset, step, length = (.2, .3, .22) if first else (.7, .2, .15)
        return ASRResult([{'text': text, 'start': offset + i * step, 'end': offset + i * step + length}
                          for i, text in enumerate(text.split())], 'fr')

    monkeypatch.setattr(TranscriberService, '_transcribe_sync', transcribe)
    recovered = refine_cut_timing(root, timing, 12)
    assert clips == pytest.approx([2.7, 2.4])
    assert recovered['words'][0] == timing['words'][0]
    assert recovered['words'][-1] == timing['words'][-1]
    assert recovered['words'][1]['start'] == pytest.approx(2.1)
    assert recovered['words'][6]['start'] == pytest.approx(5.3)
    assert [w['text'] for w in recovered['words']].count('Il') == 2
    assert recovered['uncertain_spans'][0] == [11, 11.2]
    suggestions = validate_suggestions({'suggestions': [{'start_word': 1, 'end_word': 6,
        'restart_word': 6, 'restart_end_word': 12, 'confidence': 'high'}]}, recovered['words'], 12,
        recovered['uncertain_spans'])
    assert len(suggestions) == 1
    assert suggestions[0].confidence == 'review' and not suggestions[0].enabled
    assert suggestions[0].end < recovered['words'][6]['start']
    assert (root / 'speech_timing.json').read_bytes() == source_timing
    assert (root / 'voice.wav').read_bytes() == source_audio
    assert cached_cut_timing(root, timing, 12) == json.loads(json.dumps(recovered))
    refine_cut_timing(root, timing, 12)
    assert len(clips) == 2  # Reopening/reanalyzing the same take does not load ASR again.
    assert cached_cut_timing(root, timing, 11) is None
    assert cached_cut_timing(root, {**timing, 'uncertain_spans': []}, 12) is None


def test_normal_timings_need_no_audio_or_extra_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(TranscriberService, '_transcribe_sync', lambda *a: pytest.fail('Unexpected ASR'))
    timing = {'words': [{'text': 'Simple.', 'start': 0, 'end': .5}]}
    assert refine_cut_timing(tmp_path, timing, 2) is timing
    assert not list(tmp_path.iterdir())


def test_failed_recovery_does_not_publish_partial_cache(collapsed_take, monkeypatch):
    root, timing = collapsed_take
    before = (root / 'speech_timing.json').read_bytes()

    def unavailable(*args):
        raise RuntimeError('ASR unavailable')

    monkeypatch.setattr(TranscriberService, '_transcribe_sync', unavailable)
    with pytest.raises(RuntimeError, match='ASR unavailable'):
        refine_cut_timing(root, timing, 12)
    assert not (root / 'cut_speech_timing.json').exists()
    assert (root / 'speech_timing.json').read_bytes() == before


def test_nearby_suspects_share_context_and_splits_require_real_quiet_audio(collapsed_take):
    root, timing = collapsed_take
    nearby = [*timing['words'][:-1], {'text': 'suite', 'start': 7, 'end': 9}, timing['words'][-1]]
    windows = recovery_windows(nearby, 12)
    assert len(windows) == 1 and len(windows[0]['suspects']) == 2
    with wave.open(str(root / 'voice.wav'), 'rb') as stream:
        assert pause_midpoint(stream, 3.6, 6.4) == pytest.approx(4.6)
        assert pause_midpoint(stream, 7, 9) is None


def test_invalid_local_timestamps_never_become_cut_proposals(collapsed_take, monkeypatch):
    root, timing = collapsed_take
    monkeypatch.setattr(TranscriberService, '_transcribe_sync', lambda *a: ASRResult([
        {'text': 'hallucination', 'start': 0, 'end': 90}], 'fr'))
    with pytest.raises(ValueError, match='horodatage local'):
        refine_cut_timing(root, timing, 12)
    assert not (root / 'cut_speech_timing.json').exists()
