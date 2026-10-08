from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import SceneTranscription, Transcription, Word
from app.models.facecam import FacecamAutoEditorSettings, FacecamState
from app.services.facecam_auto_editor import (
    keep_intervals, prepared_is_current, protected_spans, settings_signature, verify_speech_retained,
)


def test_quiet_words_and_consonants_survive_aggressive_silence_detection():
    words = [{'start': .501, 'end': .607}, {'start': 1.243, 'end': 1.591}]
    speech = protected_spans(words)
    settings = FacecamAutoEditorSettings(noise_threshold_db=-15, margin_before_ms=0, margin_after_ms=0, min_silence_ms=80)
    # Even if the entire file is detected as silence, both quiet words AND their
    # guards survive. Frame rounding must expand, never erode, that protection.
    keep = keep_intervals(3, [(0, 3)], settings, speech)
    assert keep == [(28, 40), (72, 99)]
    verify_speech_retained(keep, speech, 3)
    with pytest.raises(ValueError, match='coupe touche'):
        verify_speech_retained([(29, 40), (72, 99)], speech, 3)


def test_small_pause_is_cut_but_no_orphan_silence_at_file_edges():
    keep = keep_intervals(3, [(0, .5), (1, 1.2), (2.5, 3)])
    assert keep == [(28, 63), (70, 153)]
    assert keep_intervals(3, [(1, 1.1)]) == [(0, 180)]
    assert keep_intervals(3, [(0, 3)]) == []


def test_overlapping_silences_are_merged_and_uncertain_speech_is_protected():
    speech = protected_spans([{'start': .8, 'end': 1.1}], [(1, 2)])
    assert speech == [(.77, 2.05)]
    keep = keep_intervals(3, [(0, 2.3), (1.5, 3)], speech=speech)
    assert keep == [(46, 123)]
    verify_speech_retained(keep, speech, 3)


def test_last_partial_frame_retains_speech_instead_of_truncating_it():
    speech = protected_spans([{'start': 1.9, 'end': 2.007}])
    assert keep_intervals(2.007, [], speech=speech) == [(0, 121)]
    assert keep_intervals(2.007, [(0, 2.007)]) == []
    verify_speech_retained([(0, 121)], speech, 2.007)
    with pytest.raises(ValueError, match='coupe touche'):
        verify_speech_retained([(0, 120)], speech, 2.007)


def test_facecam_audio_preserves_every_sample_through_raw_insert_and_scene_joins(tmp_path):
    import array
    import wave
    from app.services.processing import PlaybackAudioSegment, ProcessingService
    source, output = tmp_path / 'source.wav', tmp_path / 'narration.wav'
    samples = array.array('h', range(16000))
    with wave.open(str(source), 'wb') as wav:
        wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(samples.tobytes())
    boundary = 2000.4 / 48000  # Exposes the old floor-start / ceil-end overlap.
    segments = [
        PlaybackAudioSegment(scene_index=0, kind='audio', source_start=0, source_end=boundary, duration=boundary),
        PlaybackAudioSegment(scene_index=1, kind='silence', duration=.5),
        PlaybackAudioSegment(scene_index=2, kind='audio', source_start=boundary, source_end=1/3, duration=1/3-boundary),
    ]
    ProcessingService.rebuild_tts_audio_with_playback_segments(source, output, segments, apply_tail_fade=False, exact_sample_boundaries=True)
    with wave.open(str(output)) as wav:
        actual = wav.readframes(wav.getnframes())
    assert actual == samples[:2000].tobytes() + bytes(24000 * 2) + samples[2000:].tobytes()


@pytest.mark.parametrize('field,value', [
    ('noise_threshold_db', -61), ('noise_threshold_db', -14), ('noise_threshold_db', float('nan')),
    ('noise_threshold_db', float('inf')), ('min_silence_ms', 79), ('min_silence_ms', 1001),
    ('margin_before_ms', -1), ('margin_after_ms', 501), ('protect_words', False),
    ('speed', .89), ('speed', 1.51), ('speed', float('nan')), ('speed', float('inf')),
])
def test_invalid_tuning_is_rejected(field, value):
    with pytest.raises(ValidationError):
        FacecamAutoEditorSettings.model_validate({field: value})


def current_state():
    settings = FacecamAutoEditorSettings()
    return FacecamState(revision=3, approved_revision=3, recording={'fingerprint': 'original'},
        prepared={'revision': 3, 'fingerprint': 'original', 'auto_editor_signature': settings_signature(settings)})


@pytest.mark.parametrize('field,value', [('noise_threshold_db', -32), ('min_silence_ms', 200), ('margin_before_ms', 80), ('margin_after_ms', 140), ('speed', 1.15)])
def test_every_setting_invalidates_prepared_audio(field, value):
    state = current_state()
    assert prepared_is_current(state)
    state.auto_editor = FacecamAutoEditorSettings.model_validate({field: value})
    assert not prepared_is_current(state)


def test_legacy_preview_recording_changes_and_revision_changes_are_stale():
    state = current_state()
    del state.prepared['auto_editor_signature']
    assert not prepared_is_current(state)
    state = current_state()
    state.recording['fingerprint'] = 'new'
    assert not prepared_is_current(state)
    state = current_state()
    state.revision += 1
    assert not prepared_is_current(state)


def test_adding_speed_at_one_keeps_existing_preview_signature():
    import hashlib
    import json
    settings = FacecamAutoEditorSettings()
    assert settings.speed == 1.0
    legacy_payload = {'version': 2, 'noise_threshold_db': -26.0, 'min_silence_ms': 120,
                      'margin_before_ms': 30, 'margin_after_ms': 50}
    legacy_signature = hashlib.sha256(json.dumps(legacy_payload, sort_keys=True).encode()).hexdigest()[:16]
    assert settings_signature(settings) == legacy_signature
    assert settings_signature(FacecamAutoEditorSettings.model_validate_json(settings.model_dump_json())) == legacy_signature


def test_speed_one_bypasses_audio_video_and_file_operations(monkeypatch, tmp_path):
    from app.services import facecam_media_service as media
    from app.services.audio_speed_service import AudioSpeedService
    from app.services.processing import ProcessingService
    def forbidden(*args, **kwargs):
        pytest.fail('1.0x must never call a media transform or probe')
    monkeypatch.setattr(AudioSpeedService, 'apply_speed', forbidden)
    monkeypatch.setattr(media, 'ffmpeg', forbidden)
    monkeypatch.setattr(ProcessingService, '_probe_wav_duration', forbidden)
    # Nonexistent paths are intentional: even copying/probing is forbidden here.
    clean, camera = tmp_path / 'voice.wav', tmp_path / 'facecam.mp4'
    assert media.FacecamMediaService.apply_speed(clean, camera, tmp_path, 1.0) == (clean, camera, 1.0)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('speed', [.9, 1.2, 1.5])
def test_retimed_audio_camera_and_word_clock_share_actual_duration(tmp_path, speed):
    from types import SimpleNamespace
    from app.services.facecam_media_service import FacecamMediaService, ffmpeg, probe, camera_segments
    from app.services.processing import ProcessingService
    clean, camera = tmp_path / 'voice_clean.wav', tmp_path / 'original.mp4'
    ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2', str(clean)])
    ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=64x96:r=60:d=3', '-an', '-c:v', 'libx264', str(camera)])
    voice, retimed_camera, scale = FacecamMediaService.apply_speed(clean, camera, tmp_path, speed)
    voice_duration = ProcessingService._probe_wav_duration(voice)
    assert abs(voice_duration - 2 / speed) < .05
    assert scale == voice_duration / 2
    streams = probe(retimed_camera)['streams']
    assert len(streams) == 1 and streams[0]['codec_type'] == 'video'
    assert streams[0]['r_frame_rate'] == '60/1'
    assert abs(float(streams[0]['duration']) - 3 * scale) <= 1 / 60
    # A one-second gap from the anime remains one second at every voice speed.
    playback = [SimpleNamespace(kind='audio', source_start=0, source_end=scale, duration=scale),
                SimpleNamespace(kind='silence', duration=1),
                SimpleNamespace(kind='audio', source_start=scale, source_end=2 * scale, duration=scale)]
    clips = camera_segments([(0, 60), (120, 180)], playback, scale)
    assert clips == [
        {'start_frame': 0, 'end_frame': round(scale * 60), 'source_frame': 0},
        {'start_frame': round((scale + 1) * 60), 'end_frame': round((2 * scale + 1) * 60), 'source_frame': round(2 * scale * 60)},
    ]


def test_facecam_timeline_keeps_first_consonant_margin_and_entire_clean_audio():
    from app.services.processing import ProcessingService
    transcription = Transcription(language='fr', scenes=[
        SceneTranscription(scene_index=0, text='Bonjour', words=[Word(text='Bonjour', start=.08, end=.4)], start_time=.08, end_time=.4),
        SceneTranscription(scene_index=1, text='Extrait', words=[], start_time=1, end_time=2, is_raw=True),
        SceneTranscription(scene_index=2, text='Salut', words=[Word(text='Salut', start=.55, end=.9)], start_time=.55, end_time=.9),
    ])
    final, segments = ProcessingService.build_authoritative_playback_timeline(transcription, contiguous_audio_duration=1, preserve_audio_start=True)
    assert segments[0].source_start == 0
    assert segments[0].source_end == segments[2].source_start == .55
    assert segments[2].source_end == 1
    assert sum(s.duration for s in segments if s.kind == 'audio') == 1
    assert final.scenes[0].words[0].start == .08
    assert final.scenes[-1].end_time == 2


def test_source_word_timings_are_cached_across_tuning(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from app.services.facecam_media_service import FacecamMediaService
    from app.services.transcriber import TranscriberService
    calls = []
    def transcribe(*args):
        calls.append(args)
        return ([{'text': 'Bonjour', 'start': .1, 'end': .7}], 'fr')
    monkeypatch.setattr(TranscriberService, '_transcribe_sync', transcribe)
    monkeypatch.setattr(TranscriberService, 'model_session', lambda: nullcontext())
    monkeypatch.setattr(TranscriberService, '_last_unrecovered_gaps', [(.8, 1)])
    first = FacecamMediaService.speech_timing(tmp_path)
    assert FacecamMediaService.speech_timing(tmp_path) == first
    assert len(calls) == 1
    assert first['uncertain_spans'] == [[.8, 1]]


def test_preview_saves_tuning_and_rejects_stale_or_busy_requests(monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import facecam
    from app.services.facecam_service import FacecamService
    state = current_state()
    invalidations = []
    jobs = []
    monkeypatch.setattr(FacecamService, 'load', lambda pid: state)
    def update(pid, values, revision=None):
        for name, value in values.items():
            setattr(state, name, value)
        return state
    monkeypatch.setattr(FacecamService, 'update', update)
    monkeypatch.setattr(FacecamService, 'invalidate_output', lambda pid: invalidations.append(pid))
    async def job(*args, **kwargs):
        jobs.append(args)
        return state
    monkeypatch.setattr(facecam, '_job', job)
    request = facecam.PreviewRequest(revision=3, auto_editor=FacecamAutoEditorSettings(min_silence_ms=220))
    asyncio.run(facecam.preview('test-preview', request))
    assert state.auto_editor.min_silence_ms == 220
    assert state.revision == state.approved_revision == 3
    assert invalidations == ['test-preview']
    assert len(jobs) == 1
    request.revision = 2
    with pytest.raises(HTTPException) as stale:
        asyncio.run(facecam.preview('test-preview', request))
    assert stale.value.status_code == 409
    monkeypatch.setattr(FacecamService, '_processing', {'test-preview'})
    with pytest.raises(HTTPException) as busy:
        asyncio.run(facecam.preview('test-preview', request))
    assert busy.value.status_code == 409
    assert len(jobs) == 1
