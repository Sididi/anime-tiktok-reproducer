"""Story choice changes prose coverage and physical source bounds together."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.models import Project, ProjectPhase, SceneList, Scene, Transcription, SceneTranscription, MatchList, SceneMatch
from app.models.transcription import Word
from app.models.facecam import ENDING
from app.services.project_service import ProjectService
from app.services.facecam_service import FacecamService, StaleFacecamRevision
from app.services.facecam_story_service import FacecamStoryService
from app.services.facecam_media_service import FacecamMediaService


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'projects_dir', tmp_path)
    monkeypatch.setattr(ProjectService, 'sync_project_pin', lambda *a: None)
    source = Project(id='original', anime_name='A hero', phase=ProjectPhase.SCRIPT_RESTRUCTURE)
    (tmp_path / source.id).mkdir()
    ProjectService.save(source)
    scenes = []
    texts = ['Le héros revient.', 'Je te reconnais.', 'des gobelins. Mais elle refuse.', 'Les novices découvrent les traces.']
    for pos, (index, text) in enumerate(zip([5, 9, 42, 60], texts)):
        words = [Word(text=w, start=pos * 10 + i, end=pos * 10 + i + .8) for i, w in enumerate(text.split())]
        scenes.append(SceneTranscription(scene_index=index, text=text, words=words,
                                        start_time=pos * 10, end_time=pos * 10 + 10, is_raw=index == 9))
    ProjectService.save_transcription(source.id, Transcription(language='fr', scenes=scenes))
    ProjectService.save_scenes(source.id, SceneList(scenes=[Scene(index=s.scene_index, start_time=s.start_time, end_time=s.end_time) for s in scenes]))
    ProjectService.save_matches(source.id, MatchList(matches=[SceneMatch(scene_index=s.scene_index, episode='episode',
        start_time=100 + i * 20, end_time=120 + i * 20, confidence=1, speed_ratio=.5) for i, s in enumerate(scenes)]))
    return FacecamService.create(source.id)


def option(title='Le retour', indices=(5, 9, 42)):
    return {'title': title, 'premise': 'Un héros revient.', 'ending': 'Il demande une mission.',
            'steps': ['Il retrouve son compagnon.', 'Il repart comme aventurier.'], 'omitted': ['La mission des novices.'],
            'clips': [{'scene_index': i, 'start_word': 0 if i == 42 else None, 'end_word': 2 if i == 42 else None} for i in indices]}


def detect(project, monkeypatch, *options):
    monkeypatch.setattr(FacecamService, 'llm', lambda *a, **k: {'stories': list(options)})
    return FacecamService.analyze_stories(project.id, FacecamService.load(project.id).revision)


def write_draft(project):
    state = FacecamService.load(project.id)
    blocks = FacecamService.skeleton(project.id, include_opinion=False)
    for block in blocks:
        if block.kind in {'hook', 'body'}:
            block.text = 'Ce héros souhaite devenir un aventurier discret.'
    return FacecamService.save_draft(project.id, state.revision, blocks, blocks[0].text, '')


def ranges(pid, prompt, **kwargs):
    payload = json.loads(prompt.split('\n', 1)[1])
    n, scenes = len(payload['words']), payload['scenes']
    return {'boundaries': [i*n//len(scenes) for i in range(1, len(scenes))]}


def test_full_story_is_always_available_and_keeps_all_source_bounds(project, monkeypatch):
    monkeypatch.setattr(FacecamService, 'llm', lambda *a: pytest.fail('No analysis needed'))
    original = FacecamService.load(project.id)
    reference, matches = FacecamService.reference(project.id), FacecamService.source_matches(project.id)
    full = original.full_story
    assert full.id == 'full-story' and original.selected_story_id is None and not original.stories
    assert [c.scene_index for c in full.clips] == [5, 9, 42, 60]
    assert all(c.start_word is None and c.end_word is None for c in full.clips)
    assert full.raw_duration_seconds == 20  # Native episode duration, not the 10 s source clip.
    words = sum(len(s.text.split()) for s in reference.scenes if not s.is_raw) + len(ENDING.split())
    assert full.estimated_duration_seconds == round(words / 3.35 + 20 + 1, 2)
    state = FacecamService.select_story(project.id, original.revision, full.id)
    assert state.selected_story_id == full.id and state.revision == 1
    assert FacecamService.load(project.id) == state
    assert FacecamService.select_story(project.id, state.revision, full.id) == state
    selected, selected_matches, _ = FacecamService.montage_reference(project.id)
    assert selected == reference and selected_matches == matches
    with pytest.raises(StaleFacecamRevision):
        FacecamService.select_story(project.id, original.revision, full.id)


def test_full_story_with_long_dialogue_can_be_selected_and_generated_without_condensation(project, monkeypatch):
    matches = FacecamService.source_matches(project.id)
    matches.matches[1].end_time = matches.matches[1].start_time + 100
    (FacecamService.directory(project.id) / 'reference_matches.json').write_text(matches.model_dump_json())
    state = FacecamService.select_story(project.id, 0, 'full-story')
    assert state.full_story.estimated_duration_seconds > 100
    prompts = []
    def model(pid, prompt):
        prompts.append(prompt)
        payload = json.loads(prompt.splitlines()[-1])
        assert payload['preserve_full_story'] is True
        if len(prompts) == 1:
            assert [s['scene_index'] for s in payload['scenes']] == [5, 9, 42, 60]
            assert payload['scenes'][2]['text'] == 'des gobelins. Mais elle refuse.'
            return {'premise': 'Un héros revient.', 'protagonist': 'Le héros', 'uncertainties': [], 'sections': [
                {'id': 'body-5', 'facts': [{'scene_indices': [5], 'text': 'Le héros revient.'}]},
                {'id': 'body-42', 'facts': [{'scene_indices': [42, 60], 'text': 'Elle refuse, les novices découvrent les traces.'}]}]}
        assert 'body_budget' not in payload and 'budget' not in payload
        return {'blocks': [{'id': 'body-5', 'text': 'retour ' * 100}, {'id': 'body-42', 'text': 'suite ' * 300}]}
    monkeypatch.setattr(FacecamService, 'llm', model)
    generated = FacecamService.generate(project.id, 'Un héros revient.', '', state.revision)
    assert len(prompts) == 3  # Extraction, writing, oral edit; no duration-based retry.
    assert json.loads(prompts[-1].splitlines()[-1])['draft_blocks'][1]['text'] == 'suite ' * 300
    assert generated.estimated_duration_seconds > 200
    assert [b.scene_indices for b in generated.blocks if b.kind == 'body'] == [[5], [42, 60]]
    assert [b.scene_indices for b in generated.blocks if b.kind == 'raw'] == [[9]]
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    approved = FacecamService.approve(project.id, generated.revision)
    retained = [m for m in approved.montage_map if m['kind'] != 'outro']
    assert [m['source_scene_index'] for m in retained] == [5, 9, 42, 60]
    assert [(m['source_start'], m['source_end']) for m in retained] == [(0, 10), (10, 20), (20, 30), (30, 40)]
    assert approved.montage_map[-1]['kind'] == 'outro'


@pytest.mark.parametrize('with_draft', [False, True])
def test_analysis_preserves_explicit_full_story_choice(project, monkeypatch, with_draft):
    old = FacecamService.select_story(project.id, 0, 'full-story')
    if with_draft:
        old = write_draft(project)
    state = detect(project, monkeypatch, option())
    assert state.selected_story_id == 'full-story'
    assert state.blocks == old.blocks and state.revision == old.revision
    assert len(state.stories) == 1 and state.full_story


def test_full_story_remains_available_after_analysis_failure_and_switching(project, monkeypatch):
    with pytest.raises(ValueError):
        detect(project, monkeypatch, {**option(), 'clips': [{'scene_index': 999}]})
    assert FacecamService.load(project.id).full_story.id == 'full-story'
    state = detect(project, monkeypatch, option())
    old = write_draft(project)
    full = FacecamService.select_story(project.id, old.revision, 'full-story')
    assert not full.blocks and not full.selected_hook and full.stories == state.stories
    assert FacecamService.context(project.id)['scenes'][-1]['scene_index'] == 60
    short = FacecamService.select_story(project.id, full.revision, 'story-1')
    assert short.selected_story_id == 'story-1'
    assert FacecamService.context(project.id)['scenes'][-1]['text'] == 'des gobelins.'
    assert FacecamStoryService.budget(FacecamService.selected_story(project.id))['target_seconds'] == [65, 70]


def test_full_story_refresh_does_not_make_stale_analysis_current(project, monkeypatch):
    state = detect(project, monkeypatch, option())
    state = FacecamService.select_story(project.id, state.revision, 'full-story')
    reference = FacecamService.reference(project.id)
    reference.scenes[0].text = 'Le récit a changé.'
    (FacecamService.directory(project.id) / 'reference.json').write_text(reference.model_dump_json())
    with pytest.raises(ValueError, match='source a changé'):
        FacecamService.selected_story(project.id)
    refreshed = FacecamService.select_story(project.id, state.revision, 'full-story')
    assert refreshed.revision == state.revision + 1 and refreshed.stories == []
    assert FacecamService.selected_story(project.id).id == 'full-story'


def test_single_arc_is_auto_selected_persisted_and_analysis_is_idempotent(project, monkeypatch):
    state = detect(project, monkeypatch, option())
    assert len(state.stories) == 1 and state.selected_story_id == state.stories[0].id
    assert state.revision == 1 and state.stories[0].raw_duration_seconds == 20
    assert FacecamService.load(project.id).selected_story_id == state.selected_story_id
    monkeypatch.setattr(FacecamService, 'llm', lambda *a: pytest.fail('Must reuse existing analysis'))
    assert FacecamService.analyze_stories(project.id, state.revision) == state


def test_multiple_arcs_require_a_choice_before_hooks(project, monkeypatch):
    state = detect(project, monkeypatch, option(), option('Les novices', (42, 60)))
    assert state.selected_story_id is None and len(state.stories) == 2
    monkeypatch.setattr(FacecamService, 'llm', lambda *a: pytest.fail('Must choose first'))
    with pytest.raises(ValueError, match='choisissez'):
        FacecamService.candidates(project.id, 'hooks')
    chosen = FacecamService.select_story(project.id, state.revision, state.stories[1].id)
    assert chosen.selected_story_id == state.stories[1].id
    assert FacecamService.context(project.id)['scenes'][0]['scene_index'] == 42
    with pytest.raises(StaleFacecamRevision):
        FacecamService.select_story(project.id, state.revision, state.stories[0].id)


@pytest.mark.parametrize('start_word', [0, None])
def test_internal_cut_reaches_montage_without_reintroducing_omitted_events(project, monkeypatch, start_word):
    root = ProjectService.get_project_dir('original')
    original_files = {n: (root / n).read_bytes() for n in ['transcription.json', 'matches.json']}
    ref_before = FacecamService.reference(project.id).model_dump_json()
    matches_before = FacecamService.source_matches(project.id).model_dump_json()
    proposal = option()
    proposal['clips'][-1]['start_word'] = start_word
    detected = detect(project, monkeypatch, proposal)
    assert detected.stories[0].clips[-1].start_word == 0
    assert FacecamService.load(project.id).stories[0].clips[-1].start_word == 0
    context = FacecamService.context(project.id)
    assert [s['scene_index'] for s in context['scenes']] == [5, 9, 42]
    assert context['scenes'][-1]['text'] == 'des gobelins.'
    assert 'refuse' not in json.dumps(context)
    state = write_draft(project)
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    approved = FacecamService.approve(project.id, state.revision)
    reference = ProjectService.load_transcription(project.id)
    matches = ProjectService.load_matches(project.id).matches
    assert [s.scene_index for s in reference.scenes] == [0, 1, 2, 3]
    assert [m.scene_index for m in matches] == [0, 1, 2, 3]
    assert [(m.start_time, m.end_time) for m in matches] == [(100, 120), (120, 140), (140, 143.6), (140, 143.6)]
    assert reference.scenes[2].text == 'des gobelins.'
    assert reference.scenes[2].end_time == pytest.approx(21.8)
    assert [m['source_scene_index'] for m in approved.montage_map] == [5, 9, 42, 42]
    assert approved.montage_map[2]['source_end'] == 21.8
    assert [m['output_scene_index'] for m in approved.montage_map] == [0, 1, 2, 3]
    assert ' '.join(a['text'] for a in approved.assignment).split() == state.spoken_text().split()
    segments = FacecamMediaService.manifest({'scenes': approved.assignment})['segments']
    assert [s['scene_indices'] for s in segments] == [[0], [2, 3]]
    ProjectService.validate_transcription_scene_alignment(project.id, reference)
    assert FacecamService.reference(project.id).model_dump_json() == ref_before
    assert FacecamService.source_matches(project.id).model_dump_json() == matches_before
    assert {n: (root / n).read_bytes() for n in original_files} == original_files


def test_leading_raw_hook_and_outro_only_reuse_selected_footage(project, monkeypatch):
    detect(project, monkeypatch, option(indices=(9, 42)))
    state = write_draft(project)
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    approved = FacecamService.approve(project.id, state.revision)
    assert [m['kind'] for m in approved.montage_map] == ['hook', 'raw', 'body', 'outro']
    assert [m['source_scene_index'] for m in approved.montage_map] == [42, 9, 42, 42]
    assert all(m.end_time <= 143.6 for m in ProjectService.load_matches(project.id).matches)
    assert ' '.join(a['text'] for a in approved.assignment).split() == state.spoken_text().split()
    ProjectService.validate_transcription_scene_alignment(project.id, ProjectService.load_transcription(project.id))


def test_sparse_scene_ids_stay_in_source_order_after_assignment_retry(project, monkeypatch):
    detect(project, monkeypatch, option(indices=(5, 42, 60)))
    state = write_draft(project)
    calls = []
    def generate(pid, prompt, **kwargs):
        calls.append(json.loads(prompt.split('\n', 1)[1]))
        if len(calls) == 1:
            # The former protocol let the model reorder/omit immutable source IDs.
            return {'ranges': [{'scene_index': i, 'start': n*4, 'end': (n+1)*4}
                               for n, i in enumerate([42, 60, 5])]}
        return ranges(pid, prompt)
    monkeypatch.setattr(FacecamService, 'llm', generate)
    approved = FacecamService.approve(project.id, state.revision)
    assert len(calls) == 2 and calls[1]['correction']['error']
    assert [m['source_scene_index'] for m in approved.montage_map] == [5, 42, 60, 60]
    assert [a['scene_index'] for a in approved.assignment] == [0, 1, 2, 3]
    assert ' '.join(a['text'] for a in approved.assignment).split() == state.spoken_text().split()
    matches = ProjectService.load_matches(project.id).matches
    assert [(m.start_time, m.end_time) for m in matches] == [(100, 120), (140, 143.6), (160, 180), (160, 180)]
    ProjectService.validate_transcription_scene_alignment(project.id, ProjectService.load_transcription(project.id))


def test_switching_arc_clears_dependent_state_but_keeps_recording(project, monkeypatch):
    state = detect(project, monkeypatch, option(), option('Les novices', (42, 60)))
    FacecamService.select_story(project.id, state.revision, 'story-1')
    state = write_draft(project)
    FacecamService.update(project.id, {'hooks': [{'id': 'h', 'text': 'Old hook'}], 'opinions': [{'id': 'o', 'text': 'Old opinion'}],
        'approved_revision': state.revision, 'prepared': {'revision': state.revision}, 'processed_signature': 'old',
        'recording': {'transcript': state.spoken_text(), 'reviewed_revision': state.revision}, 'fact_revision': state.revision})
    changed = FacecamService.select_story(project.id, state.revision, 'story-2')
    assert changed.revision == state.revision + 1
    assert not changed.blocks and not changed.hooks and not changed.opinions and not changed.assignment and not changed.montage_map
    assert changed.selected_hook == changed.selected_opinion == ''
    assert changed.approved_revision is changed.prepared is changed.processed_signature is changed.fact_revision is None
    assert changed.recording['transcript'] == state.spoken_text() and changed.recording['reviewed_revision'] is None
    assert FacecamService.select_story(project.id, changed.revision, 'story-2') == changed


@pytest.mark.parametrize('clips', [[], [{'scene_index': 999}], [{'scene_index': True}],
    [{'scene_index': 42}, {'scene_index': 5}], [{'scene_index': 5}, {'scene_index': 5}],
    [{'scene_index': 42, 'start_word': 5}], [{'scene_index': 42, 'start_word': 3, 'end_word': 2}],
    [{'scene_index': 42, 'start_word': -1, 'end_word': 2}], [{'scene_index': 42, 'start_word': 0, 'end_word': 99}],
    [{'scene_index': 42, 'start_word': True, 'end_word': 2}], [{'scene_index': 9}],
    [{'scene_index': 5}, {'scene_index': 9, 'start_word': 0, 'end_word': 2}],
])
def test_invalid_plan_does_not_persist_partial_analysis(project, monkeypatch, clips):
    value = {**option(), 'clips': clips}
    with pytest.raises(ValueError):
        detect(project, monkeypatch, value)
    assert FacecamService.load(project.id).stories == []
    assert FacecamService.load(project.id).revision == 0


@pytest.mark.parametrize('end_word', [5, None])
def test_partial_start_uses_source_word_time_and_episode_scale(project, monkeypatch, end_word):
    value = option(indices=(42,))
    value['clips'][0].update(start_word=2, end_word=end_word)
    state = detect(project, monkeypatch, value)
    assert state.stories[0].clips[0].end_word == 5
    ref, matches, _ = FacecamService.montage_reference(project.id)
    assert ref.scenes[0].text == 'Mais elle refuse.'
    assert ref.scenes[0].start_time == 22 and ref.scenes[0].end_time == 30
    assert matches.matches[0].start_time == 144 and matches.matches[0].end_time == 160


def test_selection_rejects_changed_source_and_analysis_never_overwrites_concurrent_draft(project, monkeypatch):
    def changed_during_analysis(pid, prompt, **kwargs):
        FacecamService.update(pid, {'revision': 1})
        return {'stories': [option()]}
    monkeypatch.setattr(FacecamService, 'llm', changed_during_analysis)
    with pytest.raises(StaleFacecamRevision):
        FacecamService.analyze_stories(project.id, 0)
    assert FacecamService.load(project.id).stories == []
    detect(project, monkeypatch, option())
    path = FacecamService.directory(project.id) / 'reference.json'
    reference = FacecamService.reference(project.id)
    reference.scenes[0].text = 'Source modifiée.'
    path.write_text(reference.model_dump_json())
    with pytest.raises(ValueError, match='source a changé'):
        FacecamService.narrative_context(project.id)


def test_failed_reanalysis_keeps_existing_draft_selection_and_approval(project, monkeypatch):
    detect(project, monkeypatch, option())
    state = write_draft(project)
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    FacecamService.approve(project.id, state.revision)
    # A source update makes analysis necessary, but no failed proposal may be committed.
    reference = FacecamService.reference(project.id)
    reference.scenes[0].text = 'Le héros revient enfin.'
    (FacecamService.directory(project.id) / 'reference.json').write_text(reference.model_dump_json())
    previous = (FacecamService.directory(project.id) / 'state.json').read_bytes()
    with pytest.raises(ValueError, match='3 tentatives'):
        detect(project, monkeypatch, option(), {**option(), 'clips': [{'scene_index': 42, 'end_word': 99}]})
    assert (FacecamService.directory(project.id) / 'state.json').read_bytes() == previous


def test_source_change_during_corrective_retry_prevents_commit(project, monkeypatch):
    calls = []
    def generate(pid, prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return {'stories': [{**option(), 'clips': [{'scene_index': 42, 'end_word': 99}]}]}
        reference = FacecamService.reference(pid)
        reference.scenes[0].text = 'La source a changé.'
        (FacecamService.directory(pid) / 'reference.json').write_text(reference.model_dump_json())
        return {'stories': [option()]}
    monkeypatch.setattr(FacecamService, 'llm', generate)
    with pytest.raises(ValueError, match='source a changé pendant'):
        FacecamService.analyze_stories(project.id, 0)
    assert len(calls) == 2 and not FacecamService.load(project.id).stories


def test_selected_story_bounds_reach_writer_and_editor_without_duration_retry(project, monkeypatch):
    detect(project, monkeypatch, option(indices=(5, 42)))
    prompts = []
    def model(pid, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            payload = json.loads(prompt.splitlines()[-1])
            assert [s['scene_index'] for s in payload['scenes']] == [5, 42]
            assert payload['scenes'][-1]['text'] == 'des gobelins.'
            return {'premise': 'Le héros revient.', 'protagonist': 'Le héros', 'uncertainties': [],
                    'sections': [{'id': 'body-5', 'facts': [{'scene_indices': [5, 42], 'text': 'Le héros revient et demande une mission.'}]}]}
        assert 'La mission des novices.' in prompt  # Explicitly excluded branch, not source narration.
        if len(prompts) in {2, 3}:
            payload = json.loads(prompt.splitlines()[-1])
            assert payload['selected_story']['title'] == 'Le retour'
            assert payload['budget']['max_words'] == 231
            assert payload['body_budget']['advisory'] is True
            if len(prompts) == 3:
                assert payload['draft_blocks'][0]['text'] == 'récit ' * 300
            return {'blocks': [{'id': 'body-5', 'text': 'récit ' * 300}]}
        pytest.fail('An overlong edited draft must not trigger another paid writing call')
    monkeypatch.setattr(FacecamService, 'llm', model)
    state = FacecamService.generate(project.id, 'Un héros discret.', '', 1)
    assert len(prompts) == 3  # Brief extraction, writing and exactly one oral edit.
    assert state.estimated_duration_seconds > 70
    assert [b.scene_indices for b in state.blocks if b.kind == 'body'] == [[5, 42]]
    assert state.blocks[1].text == ('récit ' * 300).strip()
    assert state.blocks[-1].text == ENDING
    assert FacecamService.load(project.id) == state
    archive, = (FacecamService.directory(project.id) / 'generations').glob('*.json')
    saved = json.loads(archive.read_text())
    assert saved['revision'] == 1 and saved['hook'] == 'Un héros discret.'
    assert saved['stories_fingerprint'] == state.stories_fingerprint
    assert saved['response']['blocks'][0]['text'] == 'récit ' * 300
    # Duration remains advisory through approval, and updates after manual condensation.
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    approved = FacecamService.approve(project.id, state.revision)
    assert approved.approved_revision == state.revision
    state.blocks[1].text = 'récit ' * 215
    shorter = FacecamService.save_draft(project.id, state.revision, state.blocks, state.selected_hook, '')
    assert 65 < shorter.estimated_duration_seconds < 70
    assert shorter.approved_revision is None


def test_long_hook_and_opinion_do_not_block_writing_with_an_exhausted_budget(project, monkeypatch):
    selected = detect(project, monkeypatch, option(indices=(5, 42)))
    monkeypatch.setattr(FacecamService, 'narrative_context', lambda *a: {
        'budget': FacecamStoryService.budget(selected.stories[0]),
    })
    calls = []
    def generate(pid, prompt):
        calls.append(prompt)
        payload = json.loads(prompt.splitlines()[-1])
        assert payload['body_budget'] == {'min_words': 0, 'max_words': 0, 'advisory': True}
        return {'blocks': [{'id': 'body-5', 'text': 'Le héros revient et demande une mission.'}]}
    monkeypatch.setattr(FacecamService, 'llm', generate)
    state = FacecamService.generate(project.id, 'accroche ' * 200, 'opinion ' * 100, selected.revision)
    assert len(calls) == 2 and state.estimated_duration_seconds > 70
    assert len(state.selected_hook.split()) == 200 and len(state.selected_opinion.split()) == 100
    assert state.blocks[1].text == 'Le héros revient et demande une mission.'


def test_existing_draft_is_not_replaced_by_single_arc_detection(project, monkeypatch):
    old = write_draft(project)
    state = detect(project, monkeypatch, option())
    assert state.selected_story_id is None and state.blocks == old.blocks and state.revision == old.revision


def test_changed_source_cannot_keep_an_approved_selection_after_reanalysis(project, monkeypatch):
    detect(project, monkeypatch, option())
    state = write_draft(project)
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    state = FacecamService.approve(project.id, state.revision)
    FacecamService.update(project.id, {'prepared': {'revision': state.revision}, 'processed_signature': 'old'})
    reference = FacecamService.reference(project.id)
    reference.scenes[0].text = 'Le héros est de retour.'
    (FacecamService.directory(project.id) / 'reference.json').write_text(reference.model_dump_json())
    analyzed = detect(project, monkeypatch, option(), option('Novices', (42, 60)))
    assert analyzed.selected_story_id is None and analyzed.revision == state.revision + 1
    assert analyzed.approved_revision is analyzed.prepared is analyzed.processed_signature is None
    assert analyzed.blocks == state.blocks  # Still readable until the user chooses a replacement.
    with pytest.raises(ValueError, match='choisissez'):
        FacecamService.approve(project.id, analyzed.revision)


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', [True, False])
@pytest.mark.parametrize('extra_episode', [True, False])
async def test_processing_keeps_editorial_cuts_instead_of_extending_them(project, monkeypatch, tmp_path, selected, extra_episode):
    from app.services.processing import ProcessingService
    from app.services.gap_resolution import GapResolutionService
    detect(project, monkeypatch, option(indices=(42,)))
    state = write_draft(project)
    monkeypatch.setattr(FacecamService, 'llm', ranges)
    state = FacecamService.approve(project.id, state.revision)
    work = tmp_path / 'prepared'
    work.mkdir()
    (work / 'narration.wav').write_bytes(b'test voice copied before the checkpoint')
    transcription = ProjectService.load_transcription(project.id)
    for i, scene in enumerate(transcription.scenes):
        scene.start_time, scene.end_time = i * 5, (i + 1) * 5
        scene.words = [Word(text='Voix', start=i * 5, end=(i + 1) * 5)]
    (work / 'transcription.json').write_text(transcription.model_dump_json())
    from app.services.facecam_auto_editor import settings_signature
    FacecamService.update(project.id, {'selected_story_id': state.selected_story_id if selected else None,
        'recording': {'fingerprint': 'test'},
        'prepared': {'revision': state.revision, 'directory': str(work), 'fingerprint': 'test',
                     'auto_editor_signature': settings_signature(state.auto_editor)}})
    async def fps(*a, **k):
        return 24.0
    monkeypatch.setattr(ProcessingService, 'detect_first_source_fps', fps)
    monkeypatch.setattr(ProcessingService, '_resolve_source_reference', lambda episode, **k: (tmp_path / (episode + '.mp4'), episode + '.mp4'))
    if extra_episode:
        from app.services.facecam_editor_track import FacecamEditorTrackService
        # The original source ID can collide with a reindexed V3 scene; neither source may disappear.
        monkeypatch.setattr(FacecamEditorTrackService, 'selected_matches', lambda _: [
            SceneMatch(scene_index=0, episode='extra', start_time=30, end_time=32, confidence=1, speed_ratio=1)])
    detected = []
    monkeypatch.setattr(GapResolutionService, 'calculate_gaps', lambda *a, **k: detected.append(a) or [])
    reached_export = []
    def checkpoint(sources):
        assert {s.source_path.name for s in sources.values()} == ({'episode.mp4', 'extra.mp4'} if extra_episode else {'episode.mp4'})
        reached_export.append(True)
        raise ValueError('Test checkpoint before episode normalization')
    monkeypatch.setattr(ProcessingService, '_collect_required_source_paths', checkpoint)
    matches = ProjectService.load_matches(project.id).matches
    before = [m.model_dump() for m in matches]
    async for _ in ProcessingService.process(project, {'scenes': state.assignment}, work / 'narration.wav',
                                              matches, reference_transcription=transcription):
        pass
    assert reached_export == [True]
    assert len(detected) == (0 if selected else 1)
    assert [m.model_dump() for m in matches] == before
