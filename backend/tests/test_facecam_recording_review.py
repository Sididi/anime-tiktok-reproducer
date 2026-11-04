"""Explicit review gates, editable words, and resumable placement without duplicate work."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.facecam import ScriptBlock
from app.services.facecam_service import FacecamService as F, StaleFacecamRevision
from app.services.facecam_recording_review import FacecamRecordingReview as Review, suggestions
from app.services.facecam_placement_service import FacecamPlacementService as Placement
from app.services.facecam_visual_candidates import FacecamVisualCandidates as Candidates
from app.services.facecam_targeted_search import FacecamTargetedSearch as Search
from app.services.facecam_window_media import FacecamWindowMedia as Media
from app.services.facecam_media_service import FacecamMediaService
from app.services.facecam_visual_index import file_identity
from app.services.project_service import ProjectService
from test_facecam import source, draft, range_model


@pytest.fixture
def review_project(source, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'cache_dir', tmp_path / 'cache')
    project, state = draft(source)
    F.approve(project.id, state.revision)
    F.update(project.id, {'recording': {'fingerprint': 'take-1', 'transcript': state.spoken_text(), 'reviewed_revision': None}})
    return project, Review.ensure(project.id)


def validate(project, state):
    r = state.recording_review
    return Review.validate(project.id, state.revision, r.id, r.version)


def test_script_approval_does_not_assign_write_pipeline_or_call_model(source, monkeypatch):
    project, state = draft(source)
    monkeypatch.setattr(F, 'llm', lambda *a, **kw: pytest.fail('Approval must be free'))
    root = ProjectService.get_project_dir(project.id)
    before = {name: (root / name).read_bytes() for name in ('transcription.json', 'matches.json', 'scenes.json')}
    approved = F.approve(project.id, state.revision)
    assert approved.approved_revision == state.revision and not approved.assignment and not approved.montage_map
    assert F.approve(project.id, state.revision) == approved
    assert before == {name: (root / name).read_bytes() for name in before}
    assert not (root / 'new_script.json').exists()
    with pytest.raises(ValueError, match='texte enregistré'):
        F.assign_scenes(project.id, state.revision)
    with pytest.raises(ValueError, match='relecture'):
        Candidates.select(project.id)


def test_even_identical_transcription_requires_explicit_review(review_project, monkeypatch):
    project, state = review_project
    monkeypatch.setattr(F, 'llm', lambda *a, **kw: pytest.fail('Review must not call a model'))
    assert not state.recording_review.changes and state.recording['reviewed_revision'] is None
    with pytest.raises(ValueError, match='différences'):
        FacecamMediaService.prepare(project.id)
    checked = validate(project, state)
    assert checked.recording['reviewed_revision'] == checked.revision
    assert checked.placement.status == 'pending' and not checked.assignment
    assert validate(project, state) == checked


def test_acceptance_and_manual_edits_persist_but_only_commit_on_validation(review_project):
    project, state = review_project
    spoken = state.spoken_text().replace('Imagine un ninja.', 'Imagine un héros courageux.')
    F.update(project.id, {'recording': {**state.recording, 'transcript': spoken}, 'recording_review': None})
    state = Review.ensure(project.id)
    change = state.recording_review.changes[0]
    blocks = [b.model_copy(deep=True) for b in state.blocks]
    block = next(b for b in blocks if b.id == change.block_id)
    block.text = block.text[:change.start] + change.replacement + block.text[change.end:]
    blocks[1].text += ' Une précision ajoutée à la main.'
    review = state.recording_review
    saved = Review.save(project.id, state.revision, review.id, review.version, blocks, [change.id])
    assert saved.blocks == state.blocks and saved.assignment == [] and saved.placement is None
    assert saved.recording_review.version == 1 and not saved.recording_review.changes
    assert Review.ensure(project.id).recording_review.blocks == blocks
    checked = validate(project, saved)
    assert checked.blocks == blocks and checked.revision == state.revision + 1
    assert checked.approved_revision == checked.recording['reviewed_revision'] == checked.revision
    # A duplicate submission carries the pre-commit revision and remains idempotent.
    assert validate(project, saved) == checked


@pytest.mark.parametrize('original,spoken,accepted', [
    ('Le petit ninja court.', 'Le ninja court.', 'Le ninja court.'),
    ('Le ninja court.', 'Le ninja court vite.', 'Le ninja court vite.'),
    ('Le ninja court.', 'Le héros court.', 'Le héros court.'),
    ('😀 Le ninja court.', 'Le héros court.', '😀 Le héros court.'),
    ('L’anime arrive.', "l'anime arrive", 'L’anime arrive.'),
])
def test_exact_word_edits_preserve_spelling_punctuation_and_unicode(original, spoken, accepted):
    block = ScriptBlock(id='body', kind='body', text=original)
    changes = suggestions([block], spoken)
    for change in sorted(changes, key=lambda c: c.start, reverse=True):
        assert block.text[change.start:change.end] == change.expected
        block.text = block.text[:change.start] + change.replacement + block.text[change.end:]
    assert block.text == accepted


def test_cross_block_changes_keep_raw_boundaries():
    blocks = [ScriptBlock(id='a', kind='body', text='Un petit héros.'), ScriptBlock(id='raw', kind='raw'),
              ScriptBlock(id='b', kind='body', text='Il court vite.')]
    changes = suggestions(blocks, 'Un grand guerrier bondit partout.')
    for block in blocks:
        for c in sorted((c for c in changes if c.block_id == block.id), key=lambda c: c.start, reverse=True):
            block.text = block.text[:c.start] + c.replacement + block.text[c.end:]
    assert blocks[1].text == ''
    assert ' '.join(b.text for b in blocks).replace('.', '').split() == 'Un grand guerrier bondit partout'.split()


def test_unaccepted_suggestions_preserve_original_text_and_stale_saves_fail(review_project):
    project, state = review_project
    F.update(project.id, {'recording_review': None, 'recording': {**state.recording, 'transcript': 'Un autre texte.'}})
    state = Review.ensure(project.id)
    checked = validate(project, state)
    assert checked.blocks == state.blocks and checked.recording_review.changes
    r = state.recording_review
    F.update(project.id, {'recording': {**checked.recording, 'fingerprint': 'replacement'}})
    with pytest.raises(StaleFacecamRevision):
        Review.save(project.id, checked.revision, r.id, r.version, r.blocks, [])
    replacement = Review.ensure(project.id)
    assert replacement.recording_review.id != r.id


def test_review_cannot_modify_raw_or_move_sections(review_project):
    project, state = review_project
    r = state.recording_review
    blocks = [b.model_copy(deep=True) for b in r.blocks]
    next(b for b in blocks if b.kind == 'raw').text = 'Un dialogue réécrit.'
    with pytest.raises(ValueError, match='originaux'):
        Review.save(project.id, state.revision, r.id, r.version, blocks, [])
    with pytest.raises(ValueError, match='sections'):
        Review.save(project.id, state.revision, r.id, r.version, list(reversed(r.blocks)), [])


def test_review_allows_omitted_ending_and_shorter_body_without_inventing_words(review_project, monkeypatch):
    project, state = review_project
    r = state.recording_review
    blocks = [b.model_copy(deep=True) for b in r.blocks]
    for block in blocks:
        if block.kind in {'ending', 'opinion'}:
            block.text = ''
        elif block.kind == 'body':
            block.text = 'Naruto.'
    state = Review.save(project.id, state.revision, r.id, r.version, blocks, [])
    state = validate(project, state)
    monkeypatch.setattr(F, 'llm', range_model)
    assigned = F.assign_scenes(project.id, state.revision)
    assert ' '.join(row['text'] for row in assigned.assignment).split() == state.spoken_text().split()
    assert all(row['kind'] != 'outro' for row in assigned.montage_map)


@pytest.fixture
def placement_project(review_project, monkeypatch, tmp_path):
    project, state = review_project
    state = validate(project, state)
    calls = {'assign': 0, 'search': 0, 'prepare': 0, 'preview': 0}
    path = tmp_path / 'episode.mp4'; path.write_bytes(b'local episode')
    info = {'source': str(path), 'identity': file_identity(path), 'fingerprint': 'source', 'fps': 24, 'frame_count': 1000}
    selected = [{'shot_id': 'sourcef240-288', 'episode': str(path), 'episode_fingerprint': 'source', 'fps': 24,
                 'in_frame': 240, 'out_frame': 288, 'start': 10, 'end': 12, 'output_scene_index': 0, 'match_index': -1, 'reason': 'Visible'}]

    def assign(pid, revision):
        current = F.load(pid)
        if not current.placement.assignment_complete:
            calls['assign'] += 1
            return F.update(pid, {'assignment': [{'scene_index': 0, 'text': current.spoken_text()}],
                'montage_map': [{'output_scene_index': 0, 'source_scene_index': 0, 'kind': 'body'}],
                'placement': current.placement.model_copy(update={'assignment_complete': True})})
        return current

    def media(pid):
        calls['prepare'] += 1
        return F.load(pid)

    def preview(pid):
        calls['preview'] += 1
        return F.load(pid)

    def search(pid, **kwargs):
        calls['search'] += 1
        return selected

    monkeypatch.setattr(F, 'assign_scenes', assign)
    monkeypatch.setattr(FacecamMediaService, 'prepare', media)
    monkeypatch.setattr(FacecamMediaService, 'preview', preview)
    monkeypatch.setattr(Candidates, 'select', search)
    monkeypatch.setattr(Candidates, 'episodes', lambda _: [path])
    monkeypatch.setattr(Media, 'probe', lambda _: info)
    monkeypatch.setattr(Search, 'targets', lambda _: ({'scenes': [{'scene_index': 0, 'slots': 2}]}, []))
    return project, state, calls, selected


def test_concurrent_placement_and_exports_reuse_the_same_selection(placement_project, monkeypatch):
    project, state, calls, selected = placement_project
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: Placement.run(project.id, state.placement.key), range(2)))
    assert all(r.placement.status == 'complete' for r in results)
    assert calls == {'assign': 1, 'search': 1, 'prepare': 1, 'preview': 1}
    monkeypatch.setattr(Candidates, 'select', lambda _: pytest.fail('No new search for preview/export'))
    current = F.load(project.id)
    F.update(project.id, {'auto_editor': current.auto_editor.model_copy(update={'speed': 1.5})})
    assert Placement.candidates(project.id) == selected
    Placement.run(project.id, state.placement.key)
    assert calls['assign'] == 1


@pytest.mark.asyncio
async def test_audio_normalization_preserves_selection_but_other_source_changes_do_not(placement_project, monkeypatch):
    from app.services import processing
    project, state, calls, selected = placement_project
    Placement.run(project.id, state.placement.key)
    path = Path(selected[0]['episode'])
    probe = object()
    monkeypatch.setattr(processing.AnimeLibraryService, '_describe_premiere_audio_normalization_reason', lambda _: 'fixture')
    monkeypatch.setattr(processing.AnimeLibraryService, '_build_library_import_audio_normalize_cmd',
                        lambda source, target, **kw: ['fixture', str(target)])
    monkeypatch.setattr(processing.AnimeLibraryService, '_probe_media_sync', lambda _: probe)
    monkeypatch.setattr(processing.AnimeLibraryService, '_is_valid_prepared_library_probe', lambda *a, **kw: True)
    monkeypatch.setattr(processing.StorageBoxRepository, 'is_enabled', lambda: False)
    monkeypatch.setattr(Candidates, 'select', lambda _: pytest.fail('An audio repair must not repeat paid search'))

    async def normalize(command, **kw):
        Path(command[-1]).write_bytes(path.read_bytes() + b' normalized audio')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(processing, 'run_command', normalize)
    # Follow successive app-managed repairs while retaining the original selection.
    for _ in range(2):
        await processing.ProcessingService._fix_premiere_incompatible_audio_in_place(path, probe=probe)
        assert Placement.candidates(project.id) == selected
        Placement.run(project.id, state.placement.key)
    assert calls == {'assign': 1, 'search': 1, 'prepare': 1, 'preview': 1}
    path.write_bytes(b'a different episode replacing the same path')
    with pytest.raises(ValueError, match='épisodes ont changé'):
        Placement.candidates(project.id)


def test_failed_search_resumes_after_assignment_without_reassigning(placement_project, monkeypatch):
    project, state, calls, selected = placement_project
    def fail_once(pid, **kwargs):
        calls['search'] += 1
        if calls['search'] == 1:
            raise ValueError('provider unavailable')
        return selected
    monkeypatch.setattr(Candidates, 'select', fail_once)
    with pytest.raises(ValueError, match='provider unavailable'):
        Placement.run(project.id, state.placement.key)
    failed = F.load(project.id)
    assert failed.placement.status == 'error' and failed.placement.assignment_complete
    Placement.run(project.id, state.placement.key)
    assert calls['assign'] == 1 and calls['search'] == 2


def test_successful_assignment_blocks_are_reused_after_later_block_failure(source, monkeypatch):
    from app.models import Transcription, SceneTranscription, MatchList, SceneMatch, SceneList, Scene
    from facecam_workflow_helpers import validate_recording
    reference = Transcription(language='fr', scenes=[SceneTranscription(scene_index=i, text=f'Plan {i}',
        start_time=i * 2, end_time=i * 2 + 2, is_raw=i == 2) for i in range(5)])
    ProjectService.save_transcription(source.id, reference)
    ProjectService.save_scenes(source.id, SceneList(scenes=[Scene(index=i, start_time=i * 2, end_time=i * 2 + 2) for i in range(5)]))
    ProjectService.save_matches(source.id, MatchList(matches=[SceneMatch(scene_index=i, episode='episode.mp4',
        start_time=20 + i * 2, end_time=22 + i * 2, confidence=1, speed_ratio=1) for i in range(5)]))
    project, state = draft(source)
    validate_recording(project.id, state.revision)
    calls = [0, 0]
    failing = [True]
    def generate(pid, prompt, **kw):
        words = json.loads(prompt.splitlines()[-1])['words']
        block = 0 if words[0][1] == 'Imagine' else 1
        calls[block] += 1
        return {'boundaries': []} if block == 1 and failing[0] else range_model(pid, prompt)
    monkeypatch.setattr(F, 'llm', generate)
    with pytest.raises(ValueError, match='3 tentatives'):
        F.assign_scenes(project.id, state.revision)
    assert calls == [1, 3] and not F.load(project.id).placement.assignment_complete
    failing[0] = False
    F.assign_scenes(project.id, state.revision)
    assert calls == [1, 4] and F.load(project.id).placement.assignment_complete
    F.assign_scenes(project.id, state.revision)
    assert calls == [1, 4]


def test_preview_failure_cannot_discard_completed_paid_placement(placement_project, monkeypatch):
    project, state, calls, selected = placement_project
    def fail(pid):
        raise ValueError('Music unavailable')
    monkeypatch.setattr(FacecamMediaService, 'preview', fail)
    with pytest.raises(ValueError, match='Music unavailable'):
        Placement.run(project.id, state.placement.key)
    assert F.load(project.id).placement.status == 'complete'
    assert Placement.candidates(project.id) == selected
    Placement.run(project.id, state.placement.key)
    assert calls['search'] == calls['assign'] == 1


def test_duplicate_validation_requests_launch_only_one_job(review_project, monkeypatch):
    from app.api.routes import facecam
    project, state = review_project
    r = state.recording_review
    request = facecam.RecordingReviewRequest(revision=state.revision, review_id=r.id, version=r.version)
    launches = []

    async def exercise():
        release = asyncio.Event()
        async def finish():
            await release.wait()
            current = F.load(project.id)
            F.update(project.id, {'placement': current.placement.model_copy(update={'status': 'complete'})})
        async def job(pid, *args, **kwargs):
            launches.append(pid)
            facecam._jobs[pid] = asyncio.create_task(finish())
            return F.load(pid)
        monkeypatch.setattr(facecam, '_jobs', {})
        monkeypatch.setattr(facecam, '_job', job)
        first, second = await asyncio.gather(facecam.review_recording(project.id, request), facecam.review_recording(project.id, request))
        assert first.placement.key == second.placement.key and len(launches) == 1
        release.set(); await facecam._jobs[project.id]
        await facecam.review_recording(project.id, request)
        assert len(launches) == 1
    asyncio.run(exercise())
