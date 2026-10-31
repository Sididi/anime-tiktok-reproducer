"""Original writing must be independent and remain usable through the montage pipeline."""
import json

import pytest

from test_facecam import source, draft, range_model  # noqa: F401 -- shared project fixture
from app.models.facecam import ENDING, FacecamState
from app.services.facecam_service import FacecamService, StaleFacecamRevision
from facecam_workflow_helpers import approve_and_assign, validate_recording
from app.services.facecam_original_service import FacecamOriginalService
from app.services.openrouter_service import OpenRouterService
from app.services.project_service import ProjectService


EVIDENCE = {"text": "Un garçon veut être reconnu et apprendre les techniques de ninja.",
            "sources": [{"url": "https://example.org/anime", "title": "Synopsis officiel"}]}
BRIEF = {"premise": "Un garçon veut être reconnu.", "protagonist": "Naruto",
         "narrative_engine": "Un désir de reconnaissance contrarié.",
         "setup_facts": [{"text": EVIDENCE["text"], "source_urls": [EVIDENCE["sources"][0]["url"]]}],
         "spoiler_boundary": "La mise en place de son apprentissage seulement.", "uncertainties": []}
BODY = {"blocks": [{"id": "body-original", "text": "Il veut enfin être reconnu. Pour ça, il doit apprendre les techniques de ninja."}]}


def writers(monkeypatch, *, brief=None, edited=None):
    calls = []
    monkeypatch.setattr(OpenRouterService, 'research', lambda prompt, **kwargs: calls.append(('research', prompt)) or EVIDENCE)
    def llm(pid, prompt, **kwargs):
        calls.append(('llm', prompt))
        if kwargs.get('schema_name') == 'facecam_original_brief':
            return brief if brief is not None else BRIEF
        return edited if edited is not None and 'draft_blocks' in json.loads(prompt.splitlines()[-1]) else BODY
    monkeypatch.setattr(FacecamService, 'llm', llm)
    return calls


@pytest.mark.parametrize('opinion', ['', '  Mon avis retouché.  '])
def test_original_research_and_writing_never_receive_source_story_or_old_body(source, monkeypatch, opinion):
    project, state = draft(source)
    for block in state.blocks:
        if block.kind == 'hook':
            block.text = '  Mon accroche retouchée.  '
        if block.kind == 'body':
            block.text = 'ANCIEN_RECIT_A_NE_PAS_TRANSMETTRE'
        if block.kind == 'opinion':
            block.text = opinion
    state.blocks = [b for b in state.blocks if b.kind != 'opinion' or opinion]
    state = FacecamService.save_draft(project.id, state.revision, state.blocks, 'Ancien choix.', 'Ancien avis.')
    # Even an obsolete story fingerprint must have no role in original writing.
    FacecamService.update(project.id, {'selected_story_id': 'old-story', 'stories_fingerprint': 'obsolete'})
    calls = writers(monkeypatch)
    monkeypatch.setattr(FacecamService, 'narrative_context', lambda *a: pytest.fail('Source story was accessed'))
    result = FacecamOriginalService.generate(project.id, state.revision)
    assert len(calls) == 4  # Research, factual brief, draft, oral edit.
    for _, prompt in calls:
        assert 'ANCIEN_RECIT_A_NE_PAS_TRANSMETTRE' not in prompt
        assert 'Naruto est ici.' not in prompt and 'Dialogue original.' not in prompt
        assert 'selected_story' not in prompt and 'scene_indices' not in prompt
        payload = json.loads(prompt.splitlines()[-1])
        assert payload['hook'] == '  Mon accroche retouchée.  ' and payload['opinion'] == opinion
    assert result.generation_mode == 'original' and result.selected_story_id is None
    assert result.blocks[0].text == '  Mon accroche retouchée.  '
    assert [b.text for b in result.blocks if b.kind == 'opinion'] == ([opinion] if opinion else [])
    assert [b.kind for b in result.blocks] == ['hook', 'body', *(['opinion'] if opinion else []), 'ending']
    assert result.blocks[1].scene_indices == [0, 2] and result.blocks[-1].text == ENDING
    assert result.estimated_duration_seconds is not None
    assert result.fact_status == 'unchecked' and result.fact_revision is None and not result.findings
    assert result.original_brief['sources'] == EVIDENCE['sources']
    archive, = (FacecamService.directory(project.id) / 'generations').glob('original-*.json')
    saved = json.loads(archive.read_text())
    assert saved['previous_state']['blocks'][1]['text'] == 'ANCIEN_RECIT_A_NE_PAS_TRANSMETTRE'
    assert saved['response'] == BODY and saved['research'] == EVIDENCE


def test_original_can_be_edited_reloaded_approved_and_generated_again(source, monkeypatch):
    project, old = draft(source)
    FacecamService.update(project.id, {'approved_revision': old.revision, 'prepared': {'revision': old.revision},
                                      'processed_signature': 'old-render', 'assignment': [{'scene_index': 0, 'text': 'old'}]})
    writers(monkeypatch)
    state = FacecamOriginalService.generate(project.id, old.revision)
    assert state.approved_revision is None and state.prepared is None and state.processed_signature is None
    assert state.assignment == [] and state.montage_map == []
    state.blocks[1].text += ' Une étape importante pour lui.'
    state = FacecamService.save_draft(project.id, state.revision, state.blocks, state.selected_hook, state.selected_opinion)
    assert FacecamService.load(project.id).generation_mode == 'original'
    monkeypatch.setattr(FacecamService, 'llm', range_model)
    approved = approve_and_assign(project.id, state.revision)
    assert approved.approved_revision == state.revision
    assert ' '.join(s['text'] for s in approved.assignment).split() == state.spoken_text().split()
    assert [m['source_scene_index'] for m in approved.montage_map] == [0, 2, 2]
    assert not any(s.is_raw for s in ProjectService.load_transcription(project.id).scenes)
    assert [s.scene_index for s in ProjectService.load_transcription(project.id).scenes] == [0, 1, 2]
    assert len(ProjectService.load_matches(project.id).matches) == 3
    assert FacecamService.reference(project.id).scenes[1].is_raw  # Immutable source remains intact.
    calls = writers(monkeypatch)
    newer = FacecamService.generate(project.id, 'Nouvel angle.', '', state.revision)
    assert len(calls) == 4 and newer.generation_mode == 'original'
    assert newer.selected_hook == 'Nouvel angle.' and not any(b.kind == 'opinion' for b in newer.blocks)
    restored = FacecamService.select_story(project.id, newer.revision, 'full-story')
    assert restored.generation_mode == 'story' and restored.original_brief is None
    assert not restored.blocks and any(b.kind == 'raw' for b in FacecamService.skeleton(project.id))


def test_short_original_script_can_use_illustrations_from_a_much_longer_source(source, monkeypatch):
    project, state = draft(source)
    reference, matches = FacecamService.reference(project.id), FacecamService.source_matches(project.id)
    reference.scenes = [reference.scenes[0].model_copy(update={'scene_index': i, 'start_time': i * 2,
                                                             'end_time': i * 2 + 2}) for i in range(80)]
    matches.matches = [matches.matches[0].model_copy(update={'scene_index': i, 'start_time': i * 2 + 20,
                                                            'end_time': i * 2 + 22}) for i in range(80)]
    root = FacecamService.directory(project.id)
    (root / 'reference.json').write_text(reference.model_dump_json())
    (root / 'reference_matches.json').write_text(matches.model_dump_json())
    writers(monkeypatch)
    new = FacecamOriginalService.generate(project.id, state.revision)
    approved = approve_and_assign(project.id, new.revision)
    assert approved.approved_revision == new.revision
    assert ' '.join(s['text'] for s in approved.assignment).split() == new.spoken_text().split()
    assert len(approved.assignment) < 80
    assert approved.montage_map[0]['source_scene_index'] == 0
    assert approved.montage_map[-2]['source_scene_index'] == 79


@pytest.mark.parametrize('failure', ['research', 'brief', 'draft', 'edit'])
def test_failures_leave_previous_script_mode_and_approval_intact(source, monkeypatch, failure):
    project, state = draft(source)
    state = FacecamService.update(project.id, {'approved_revision': state.revision, 'prepared': {'revision': state.revision}})
    def research(*a, **kw):
        if failure == 'research':
            raise RuntimeError('Recherche indisponible')
        return EVIDENCE
    def llm(pid, prompt, **kw):
        stage = 'brief' if kw.get('schema_name') else 'edit' if 'draft_blocks' in json.loads(prompt.splitlines()[-1]) else 'draft'
        if stage == failure:
            return {'blocks': []}
        return BRIEF if stage == 'brief' else BODY
    monkeypatch.setattr(OpenRouterService, 'research', research)
    monkeypatch.setattr(FacecamService, 'llm', llm)
    with pytest.raises((ValueError, RuntimeError)):
        FacecamOriginalService.generate(project.id, state.revision)
    retained = FacecamService.load(project.id)
    assert retained.blocks == state.blocks and retained.revision == state.revision
    assert retained.approved_revision == state.revision and retained.prepared == state.prepared
    assert retained.generation_mode == 'story' and retained.original_brief is None


@pytest.mark.parametrize('stage', ['research', 'edit'])
def test_concurrent_edit_prevents_original_replacement(source, monkeypatch, stage):
    project, state = draft(source)
    def edit():
        state.blocks[1].text = 'Une correction pendant la recherche.'
        FacecamService.save_draft(project.id, state.revision, state.blocks, state.selected_hook, state.selected_opinion)
    def research(*a, **kw):
        if stage == 'research':
            edit()
        return EVIDENCE
    def llm(pid, prompt, **kw):
        if kw.get('schema_name'):
            return BRIEF
        if 'draft_blocks' in json.loads(prompt.splitlines()[-1]):
            edit()
        return BODY
    monkeypatch.setattr(OpenRouterService, 'research', research)
    monkeypatch.setattr(FacecamService, 'llm', llm)
    with pytest.raises(StaleFacecamRevision):
        FacecamOriginalService.generate(project.id, state.revision)
    assert FacecamService.load(project.id).blocks[1].text == 'Une correction pendant la recherche.'
    assert FacecamService.load(project.id).generation_mode == 'story'


def test_missing_script_and_stale_revision_fail_before_research(source, monkeypatch):
    monkeypatch.setattr(OpenRouterService, 'research', lambda *a, **kw: pytest.fail('Unexpected paid call'))
    project = FacecamService.create(source.id)
    with pytest.raises(ValueError, match="d'abord"):
        FacecamOriginalService.generate(project.id, 0)
    project, state = draft(source)
    with pytest.raises(StaleFacecamRevision):
        FacecamOriginalService.generate(project.id, state.revision - 1)


@pytest.mark.parametrize('facts', [[], [{'text': 'Une invention.', 'source_urls': ['https://invented.example/']}],
                                   [{'text': 'Une invention.', 'source_urls': []}]])
def test_brief_requires_cited_setup_facts(facts):
    with pytest.raises(ValueError):
        FacecamOriginalService.validate_brief({**BRIEF, 'setup_facts': facts}, EVIDENCE)


def test_legacy_states_keep_story_mode():
    state = FacecamState.model_validate({'revision': 4})
    assert state.generation_mode == 'story' and state.original_brief is None


def test_direct_original_selection_does_not_call_models_and_first_script_reuses_brief(source, monkeypatch):
    project = FacecamService.create(source.id)
    calls = writers(monkeypatch)
    state = FacecamService.select_story(project.id, 0, 'original')
    assert state.generation_mode == 'original' and state.selected_story_id is None
    assert not state.blocks and not calls
    assert FacecamService.select_story(project.id, state.revision, 'original').revision == state.revision
    write = FacecamService.llm
    def llm(pid, prompt, **kwargs):
        if 'huit' in prompt and not kwargs:
            return {'candidates': [f'Angle {i}' for i in range(8)]}
        return write(pid, prompt, **kwargs)
    monkeypatch.setattr(FacecamService, 'llm', llm)
    FacecamService.candidates(project.id, 'hooks')
    FacecamService.candidates(project.id, 'opinions')
    assert [kind for kind, _ in calls] == ['research', 'llm']
    assert not FacecamService.load(project.id).blocks
    result = FacecamService.generate(project.id, 'Une accroche choisie.', '', state.revision)
    assert [kind for kind, _ in calls] == ['research', 'llm', 'llm', 'llm']
    assert result.generation_mode == 'original' and result.blocks[1].id == 'body-original'
    assert result.blocks[0].text == 'Une accroche choisie.'
    assert not any(b.kind in {'raw', 'opinion'} for b in result.blocks)


def test_direct_original_can_start_with_typed_hook_and_retry_without_research(source, monkeypatch):
    project = FacecamService.create(source.id)
    state = FacecamService.select_story(project.id, 0, 'original')
    calls = writers(monkeypatch, edited={'blocks': []})
    with pytest.raises(ValueError):
        FacecamService.generate(project.id, 'Mon angle.', 'Mon avis.', state.revision)
    current = FacecamService.load(project.id)
    assert current.revision == state.revision and not current.blocks and current.original_brief
    assert sum(kind == 'research' for kind, _ in calls) == 1
    calls = writers(monkeypatch)
    result = FacecamService.generate(project.id, 'Mon angle.', 'Mon avis.', state.revision)
    assert [kind for kind, _ in calls] == ['llm', 'llm']
    assert result.selected_opinion == 'Mon avis.'


def test_direct_original_brief_cannot_be_saved_after_switching_story(source, monkeypatch):
    project = FacecamService.create(source.id)
    state = FacecamService.select_story(project.id, 0, 'original')
    def research(*args, **kwargs):
        FacecamService.select_story(project.id, state.revision, 'full-story')
        return EVIDENCE
    monkeypatch.setattr(OpenRouterService, 'research', research)
    with pytest.raises(StaleFacecamRevision):
        FacecamOriginalService.ensure_brief(project.id, state.revision)
    current = FacecamService.load(project.id)
    assert current.generation_mode == 'story' and current.original_brief is None


def test_fact_check_uses_dedicated_light_model_for_both_stages(source, monkeypatch):
    from app.services.llm_config_service import LLMConfigService
    project, state = draft(source)
    calls = []
    entry = LLMConfigService.fact_check_entry()
    assert entry.openrouter_id == 'google/gemini-3.8-flash'
    assert entry.max_output_tokens == 4096
    def research(prompt, **kwargs):
        calls.append(kwargs)
        return EVIDENCE
    def annotate(prompt, **kwargs):
        calls.append(kwargs)
        return {'findings': []}
    monkeypatch.setattr(OpenRouterService, 'research', research)
    monkeypatch.setattr(OpenRouterService, 'generate_json_value_with_entry', annotate)
    monkeypatch.setattr(FacecamService, 'llm', lambda *a, **kw: pytest.fail('Writing model must not check facts'))
    result = FacecamService.fact_check(project.id)
    assert result.fact_status == 'complete' and result.fact_revision == state.revision
    assert calls == [{'entry': entry}, {'entry': entry}]


def test_original_fact_context_excludes_source_narration_and_raw_inserts(source, monkeypatch):
    from app.services.facecam_fact_context import FacecamFactContext
    from app.services.anime_library import AnimeLibraryService
    project, state = draft(source)
    writers(monkeypatch)
    FacecamOriginalService.generate(project.id, state.revision)
    monkeypatch.setattr(AnimeLibraryService, 'resolve_episode_path', lambda *a, **kw: None)
    context = FacecamFactContext.build(project.id)
    scenes = [s for e in context['episodes'] for s in e['matched_scenes']]
    assert [s['scene_index'] for s in scenes] == [0, 2]
    assert all(s['original_text'] == '' for s in scenes)


@pytest.mark.asyncio
async def test_original_endpoint_checks_the_new_script_in_a_detached_job(source, monkeypatch):
    from app.api.routes import facecam
    project, state = draft(source)
    writers(monkeypatch)
    checked = []
    monkeypatch.setattr(FacecamService, 'fact_check', lambda pid: checked.append(FacecamService.load(pid).generation_mode))
    try:
        pending = await facecam.generate_original(project.id, facecam.ActionRequest(revision=state.revision))
        assert pending.job['status'] == 'processing'
        await facecam._jobs[project.id]
        assert checked == ['original']
        assert FacecamService.load(project.id).job['status'] == 'complete'
    finally:
        facecam._jobs.pop(project.id, None)
