from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.facecam import RecordingCut, RecordingEdit
from app.services.facecam_cut_service import FacecamCutService, cut_frames, keep_frames, validate_suggestions, waveform
from app.services.facecam_media_service import FacecamMediaService, ffmpeg, probe
from app.services.facecam_service import FacecamService, StaleFacecamRevision
from test_facecam import source, draft  # noqa: F401 — shared isolated project fixture


def cut(start=1, end=2, id="manual-1", enabled=True):
    return RecordingCut(id=id, start=start, end=end, enabled=enabled)


def test_overlapping_cuts_are_unioned_and_quantized_inward():
    removed = cut_frames([cut(1.001, 2.999), cut(2, 4, "b"), cut(5, 6, "c", False)], 8)
    assert removed == [(61, 240)]
    assert keep_frames(removed, 8) == [(0, 61), (240, 480)]
    assert cut_frames([], 8) == []


@pytest.mark.parametrize("cuts", [[cut(3, 2)], [cut(1, 9)], [cut(0, 8)], [cut(1, 1.001)], [cut(), cut()]])
def test_invalid_or_empty_montages_are_rejected(cuts):
    with pytest.raises(ValueError):
        cut_frames(cuts, 8)


@pytest.mark.parametrize("value", [float('nan'), float('inf'), -1])
def test_nonfinite_or_negative_bounds_are_rejected(value):
    with pytest.raises(ValidationError):
        cut(value, 2)


def words(text, start=0):
    return [{"word": word, "start": start + i * .4, "end": start + i * .4 + .3} for i, word in enumerate(text.split())]


def proposal(confidence="high", **changes):
    return {"start_word": 0, "end_word": 5, "restart_word": 5, "restart_end_word": 12,
        "confidence": confidence, "reason": "Début de phrase abandonné puis repris.", **changes}


def test_only_strong_repeated_phrase_evidence_can_be_preselected():
    timed = words("Il découvre un nouveau pouvoir") + words("Il découvre un nouveau pouvoir vraiment incroyable", start=2.5)
    suggestions = validate_suggestions({"suggestions": [proposal()]}, timed, 6)
    assert len(suggestions) == 1
    assert suggestions[0].enabled and suggestions[0].confidence == 'high'
    assert suggestions[0].end < timed[5]['start']
    assert suggestions[0].removed_text == 'Il découvre un nouveau pouvoir'
    cautious = validate_suggestions({"suggestions": [proposal('review')]}, timed, 6)
    assert not cautious[0].enabled
    assert cautious[0].confidence == 'review'


def test_word_variations_and_invalid_indices_never_become_automatic_cuts():
    timed = words("pouvoir puissance")
    assert validate_suggestions({'suggestions': [proposal(end_word=1, restart_word=1, restart_end_word=2)]}, timed, 2) == []
    timed = words("Il découvre un nouveau pouvoir") + words("La météo sera belle demain", start=3)
    assert validate_suggestions({'suggestions': [proposal(restart_end_word=10)]}, timed, 6) == []
    for changes in ({'start_word': True}, {'end_word': 999}, {'restart_word': 2}, {'confidence': 'certain'}):
        assert validate_suggestions({'suggestions': [proposal(**changes)]}, timed, 6) == []


def test_short_ambiguous_repeat_is_proposed_but_never_preselected():
    timed = words("Il devient puissant") + words("Il devient puissant et rentre chez lui", start=1.5)
    result = validate_suggestions({'suggestions': [proposal(end_word=3, restart_word=3, restart_end_word=10)]}, timed, 5)
    assert len(result) == 1 and result[0].confidence == 'review' and not result[0].enabled


def test_explicit_restart_comments_do_not_hide_evidence_but_uncertain_audio_disables_preselection():
    timed = words('Il découvre un nouveau pouvoir pardon je recommence') + words('Il découvre un nouveau pouvoir incroyable', start=3.5)
    payload = {'suggestions': [proposal(end_word=8, restart_word=8, restart_end_word=14)]}
    result = validate_suggestions(payload, timed, 7)
    assert result[0].enabled
    assert not validate_suggestions(payload, timed, 7, [(1, 2)])[0].enabled


@pytest.fixture
def recording(source):
    project, state = draft(source)
    root = FacecamService.directory(project.id) / 'recordings' / 'source'
    root.mkdir(parents=True)
    # A ramp makes every audio sample identifiable; the image changes at one second.
    samples = (np.arange(3 * 48000, dtype=np.int32) % 16000 - 8000).astype('<i2')
    with wave.open(str(root / 'voice.wav'), 'wb') as stream:
        stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.tobytes())
    ffmpeg(['-f', 'lavfi', '-i', 'color=c=black:s=64x96:r=60:d=3', '-vf',
        "drawbox=color=white:t=fill:enable='gte(t,1)'", '-an', '-c:v', 'libx264', str(root / 'facecam.mp4')])
    (root / 'original.mov').write_bytes(b'preserved original bytes')
    record = {'fingerprint': 'source', 'directory': str(root), 'original_name': 'original.mov',
        'scale': 100, 'duration': 3, 'transcript': '', 'differences': [], 'reviewed_revision': None}
    edit = FacecamCutService.new_edit(record)
    FacecamService.update(project.id, {'approved_revision': state.revision, 'recording': record, 'recording_edit': edit})
    return project, root, edit


def test_media_edits_share_exact_camera_and_sample_boundaries_and_are_cached(recording, monkeypatch):
    project, source_root, edit = recording
    original = (source_root / 'voice.wav').read_bytes()
    root, fingerprint, duration = FacecamCutService.render(edit, [cut(.5, 1.5)])
    assert duration == 2 and root != source_root and fingerprint != 'source'
    with wave.open(str(root / 'voice.wav'), 'rb') as stream:
        actual = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')
    with wave.open(str(source_root / 'voice.wav'), 'rb') as stream:
        source_samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')
    np.testing.assert_array_equal(actual, np.concatenate([source_samples[:24000], source_samples[72000:]]))
    assert (source_root / 'voice.wav').read_bytes() == original
    assert (source_root / 'original.mov').read_bytes() == b'preserved original bytes'
    camera = probe(root / 'facecam.mp4')['streams']
    assert len(camera) == 1 and int(camera[0]['nb_frames']) == 120
    assert camera[0]['r_frame_rate'] == '60/1'
    images = ffmpeg(['-i', str(root / 'facecam.mp4'), '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-']).stdout
    assert max(images[:30]) < 30 and min(images[30:]) > 220  # Picture jumps at the same .5 s audio join.
    review = probe(root / 'review.mp4')['streams']
    assert {s['codec_type'] for s in review} == {'video', 'audio'}
    assert all(abs(float(s['duration']) - 2) < .03 for s in review)
    def forbidden(*args, **kwargs):
        pytest.fail('The same applied cuts must reuse encoded assets')
    monkeypatch.setattr('app.services.facecam_media_service.ffmpeg', forbidden)
    assert FacecamCutService.render(edit, [cut(.5, 1.5)]) == (root, fingerprint, duration)
    assert FacecamCutService.render(edit, []) == (source_root, 'source', 3)


def test_final_comparison_uses_edited_audio_and_stale_or_unvalidated_edits_cannot_run(recording, monkeypatch):
    project, root, edit = recording
    state = FacecamService.load(project.id)
    FacecamService.update(project.id, {'prepared': {'old': True}, 'processed_signature': 'old'})
    calls = []
    def speech(directory):
        calls.append(directory)
        assert directory != root
        return {'words': [{'word': state.spoken_text(), 'start': 0, 'end': 2}]}
    monkeypatch.setattr(FacecamMediaService, 'speech_timing', speech)
    with pytest.raises(ValueError, match='coupes'):
        FacecamMediaService.review(project.id, state.revision)
    with pytest.raises(ValueError, match='coupes'):
        FacecamMediaService.prepare(project.id)
    for revision, identity, version in ((999, edit.id, 0), (state.revision, 'other', 0), (state.revision, edit.id, 1)):
        with pytest.raises(StaleFacecamRevision):
            FacecamCutService.apply(project.id, revision, identity, version, [cut()])
    assert not calls
    result = FacecamCutService.apply(project.id, state.revision, edit.id, 0, [cut(.5, 1.5)])
    assert result.recording_edit.applied and result.recording_edit.version == 1
    assert result.recording['reviewed_revision'] == state.revision
    assert result.recording['differences'] == []
    assert result.recording['duration'] == 2
    assert result.recording['source_directory'] == str(root)
    assert result.prepared is None and result.processed_signature is None
    with pytest.raises(StaleFacecamRevision):
        FacecamCutService.apply(project.id, state.revision, edit.id, 0, [])


def test_failed_final_transcription_preserves_previous_take_and_saved_edit(recording, monkeypatch):
    project, root, edit = recording
    before = FacecamService.load(project.id)
    def failed(*args):
        raise RuntimeError('ASR unavailable')
    monkeypatch.setattr(FacecamMediaService, 'speech_timing', failed)
    with pytest.raises(RuntimeError):
        FacecamCutService.apply(project.id, before.revision, edit.id, 0, [cut()])
    after = FacecamService.load(project.id)
    assert after.recording == before.recording
    assert after.recording_edit == before.recording_edit


def test_analysis_failure_still_exposes_waveform_and_manual_editing(recording, monkeypatch):
    project, root, edit = recording
    monkeypatch.setattr(FacecamMediaService, 'speech_timing', lambda *a: {'words': words('Il est vraiment fort')})
    def unavailable(*args):
        raise RuntimeError('LLM unavailable')
    monkeypatch.setattr(FacecamService, 'llm', unavailable)
    state = FacecamCutService.analyze(project.id)
    assert state.recording_edit.analysis_status == 'error'
    assert not state.recording_edit.applied and not state.recording['transcript']
    data = FacecamCutService.data(project.id, edit.id)
    assert len(data['peaks']) == 600
    assert max(data['peaks']) > .2
    assert waveform(root, 3) == data['peaks']
    with pytest.raises(StaleFacecamRevision):
        FacecamCutService.data(project.id, 'other-take')


def test_legacy_editor_waits_for_compatible_preview_and_preserves_previous_results(recording, monkeypatch):
    project, root, edit = recording
    FacecamService.update(project.id, {'recording_edit': None, 'prepared': {'previous': True}})
    original_preview = FacecamCutService.source_preview
    observed = []
    def preview(directory):
        observed.append(FacecamService.load(project.id).recording_edit is not None)
        return original_preview(directory)
    monkeypatch.setattr(FacecamCutService, 'source_preview', preview)
    monkeypatch.setattr(FacecamMediaService, 'speech_timing', lambda *a: {'words': words('Il est vraiment fort')})
    monkeypatch.setattr(FacecamService, 'llm', lambda *a: {'suggestions': []})
    state = FacecamCutService.analyze(project.id)
    assert observed == [False, True]
    assert (root / 'source_preview.mp4').is_file()
    assert state.recording_edit.applied and state.recording_edit.analysis_status == 'ready'
    assert state.prepared == {'previous': True}


def test_cut_routes_reject_stale_or_busy_apply_and_serve_the_right_original(recording, monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import facecam
    project, root, edit = recording
    state = FacecamService.load(project.id)
    stale = facecam.RecordingCutsRequest(revision=state.revision, edit_id='other', version=0, cuts=[])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(facecam.apply_recording_cuts(project.id, stale))
    assert exc.value.status_code == 409
    monkeypatch.setattr(FacecamService, '_processing', {project.id})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(facecam.apply_recording_cuts(project.id, stale.model_copy(update={'edit_id': edit.id})))
    assert exc.value.status_code == 409
    response = asyncio.run(facecam.recording_source(project.id, edit.id))
    assert response.path == root / 'original.mov'
    with pytest.raises(HTTPException) as exc:
        asyncio.run(facecam.recording_source(project.id, 'new-take'))
    assert exc.value.status_code == 409
