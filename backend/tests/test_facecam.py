from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.models import Project, ProjectPhase, Scene, SceneList, MatchList, SceneMatch, Transcription, SceneTranscription
from app.models.facecam import ENDING
from app.services.project_service import ProjectService
from app.services.facecam_service import FacecamService, StaleFacecamRevision
from app.services.facecam_media_service import compare_words, keep_intervals, camera_segments, FacecamMediaService


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'projects_dir', tmp_path)
    monkeypatch.setattr(ProjectService, 'sync_project_pin', lambda *a: None)
    project = Project(id='original', anime_name='Naruto', phase=ProjectPhase.SCRIPT_RESTRUCTURE,
        drive_folder_id='private-folder', scheduled_account_id='old-account', mother_project_id='family')
    (tmp_path / project.id).mkdir()
    video = tmp_path / project.id / 'reference.mp4'
    video.write_bytes(b'original-reference')
    project.video_path = str(video)
    ProjectService.save(project)
    scenes = [SceneTranscription(scene_index=i, text=text, start_time=i*2, end_time=(i+1)*2, is_raw=raw)
        for i, (text, raw) in enumerate([('Naruto est ici.', False), ('Dialogue original.', True), ('Il devient ninja.', False)])]
    ProjectService.save_transcription(project.id, Transcription(language='en', scenes=scenes))
    ProjectService.save_scenes(project.id, SceneList(scenes=[Scene(index=i,start_time=i*2,end_time=(i+1)*2) for i in range(3)]))
    ProjectService.save_matches(project.id, MatchList(matches=[SceneMatch(scene_index=i, episode='episode.mp4',
        start_time=i*2+20, end_time=i*2+22, confidence=1, speed_ratio=1) for i in range(3)]))
    (tmp_path / project.id / 'new_script.json').write_text('old generated text')
    return project


def draft(source):
    project = FacecamService.create(source.id)
    blocks = FacecamService.skeleton(project.id)
    for block in blocks:
        if block.kind not in {'raw','ending'}:
            block.text = {'hook':'Imagine un ninja.', 'body':'Naruto poursuit son rêve.', 'opinion':'Je trouve ce choix intéressant.'}[block.kind]
    state = FacecamService.save_draft(project.id, 0, blocks, blocks[0].text, blocks[-2].text)
    return project, state


def range_model(pid, prompt, **kwargs):
    payload = json.loads(prompt.split('\n',1)[1])
    count = len(payload['words'])
    scenes = len(payload['scenes'])
    return {'boundaries': [pos*count//scenes for pos in range(1, scenes)]}


def narrative_brief():
    return {'premise': 'Un jeune garçon souhaite devenir ninja.', 'protagonist': 'Naruto',
        'sections': [{'id': 'body-0', 'facts': [{'scene_indices': [0], 'text': 'Naruto arrive.'}]},
                     {'id': 'body-2', 'facts': [{'scene_indices': [2], 'text': 'Naruto devient ninja.'}]}],
        'uncertainties': []}


def test_narrative_brief_is_shared_but_source_prose_never_reaches_writer(source, monkeypatch):
    project, initial = draft(source)  # A pre-selection draft remains compatible.
    prompts = []
    def generate(pid, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            assert 'Naruto est ici.' in prompt
            return narrative_brief()
        assert 'Naruto est ici.' not in prompt
        assert 'Dialogue original.' in prompt  # The protected insert remains context.
        if len(prompts) in {2, 3}:
            return {'candidates': [f'Proposition distincte {i}.' for i in range(8)]}
        return {'blocks': [{'id': 'body-0', 'text': 'Naruto arrive avec un objectif.'},
                           {'id': 'body-2', 'text': 'Il devient finalement ninja.'}]}
    monkeypatch.setattr(FacecamService, 'llm', generate)
    hooks = FacecamService.candidates(project.id, 'hooks')
    assert len(hooks.hooks) == 8 and not any(h.original for h in hooks.hooks)
    FacecamService.update(project.id, {'selected_hook': 'Une accroche choisie.'})
    FacecamService.candidates(project.id, 'opinions')
    assert 'Une accroche choisie.' in prompts[-1]
    state = FacecamService.generate(project.id, 'Une accroche choisie.', 'Un avis précis.', initial.revision)
    assert len(prompts) == 4  # One extraction shared by all three writing steps.
    assert [b.id for b in state.blocks] == ['hook', 'body-0', 'raw-1', 'body-2', 'opinion', 'ending']
    assert state.blocks[2].text == '' and state.blocks[-1].text == ENDING
    assert state.blocks[0].text == 'Une accroche choisie.'


@pytest.mark.parametrize('opinion', ['', '   '])
def test_optional_opinion_can_be_generated_edited_approved_and_restored(source, monkeypatch, opinion):
    project = FacecamService.create(source.id)
    monkeypatch.setattr(FacecamService, 'narrative_context', lambda *a: narrative_brief())
    prompts = []
    def write_body(pid, prompt):
        prompts.append(prompt)
        return {'blocks': [{'id': 'body-0', 'text': 'Naruto arrive avec un objectif.'},
                           {'id': 'body-2', 'text': 'Il devient finalement ninja.'}]}
    monkeypatch.setattr(FacecamService, 'llm', write_body)
    state = FacecamService.generate(project.id, 'Une accroche choisie.', opinion, 0)
    assert json.loads(prompts[-1].splitlines()[-1])['opinion'] == ''
    assert [b.id for b in state.blocks] == ['hook', 'body-0', 'raw-1', 'body-2', 'ending']
    assert FacecamService.load(project.id).selected_opinion == ''
    state.blocks[1].text = 'Naruto poursuit son objectif.'
    state = FacecamService.save_draft(project.id, state.revision, state.blocks, state.selected_hook, '')
    # Skipping an opinion must never allow an original audio marker to disappear.
    with pytest.raises(ValueError, match='repères'):
        FacecamService.save_draft(project.id, state.revision,
            [b for b in state.blocks if b.kind != 'raw'], state.selected_hook, '')
    monkeypatch.setattr(FacecamService, 'llm', range_model)
    state = FacecamService.approve(project.id, state.revision)
    assert state.approved_revision == state.revision
    assert state.assignment[-1]['text'] == ENDING
    assert ProjectService.load_transcription(project.id).scenes[-1].text == ENDING
    monkeypatch.setattr(FacecamService, 'llm', write_body)
    state = FacecamService.generate(project.id, state.selected_hook, 'Un avis précis.', state.revision)
    assert state.blocks[-2].kind == 'opinion' and state.blocks[-2].text == 'Un avis précis.'


@pytest.mark.parametrize('indices', [[0, 1], [2], [0, 0], [True]])
def test_narrative_brief_rejects_crossed_raw_boundaries_missing_or_duplicate_plans(source, monkeypatch, indices):
    project, initial = draft(source)
    value = narrative_brief()
    value['sections'][0]['facts'][0]['scene_indices'] = indices
    monkeypatch.setattr(FacecamService, 'llm', lambda *a: value)
    with pytest.raises(ValueError):
        FacecamService.generate(project.id, 'Accroche.', 'Opinion.', initial.revision)
    assert FacecamService.load(project.id).revision == initial.revision
    assert not (FacecamService.directory(project.id) / 'narrative_brief.json').exists()


def test_narrative_brief_cache_invalidates_on_reference_change_and_corruption(source, monkeypatch):
    project, _ = draft(source)
    calls = []
    monkeypatch.setattr(FacecamService, 'llm', lambda pid, prompt: calls.append(prompt) or narrative_brief())
    FacecamService.narrative_context(project.id)
    FacecamService.narrative_context(project.id)
    assert len(calls) == 1
    path = FacecamService.directory(project.id) / 'reference.json'
    reference = FacecamService.reference(project.id)
    reference.scenes[0].text = 'Naruto arrive au village.'
    path.write_text(reference.model_dump_json())
    FacecamService.narrative_context(project.id)
    assert len(calls) == 2 and 'Naruto arrive au village.' in calls[-1]
    (path.parent / 'narrative_brief.json').write_text('{partial')
    FacecamService.narrative_context(project.id)
    assert len(calls) == 3


@pytest.mark.parametrize('result', [None, [], {'blocks': None}, {'blocks': ['bad']},
    {'blocks': [{'id': 'body-0', 'text': 12}]},
    {'blocks': [{'id': 'body-2', 'text': 'Fin'}, {'id': 'body-0', 'text': 'Début'}]}])
def test_malformed_narration_does_not_replace_draft(source, monkeypatch, result):
    project, state = draft(source)
    monkeypatch.setattr(FacecamService, 'narrative_context', lambda *a: narrative_brief())
    monkeypatch.setattr(FacecamService, 'llm', lambda *a: result)
    with pytest.raises(ValueError):
        FacecamService.generate(project.id, 'Accroche.', 'Opinion.', state.revision)
    assert FacecamService.load(project.id).blocks == state.blocks


def test_stale_generation_is_rejected_before_calling_model(source, monkeypatch):
    project, state = draft(source)
    def unexpected(*args):
        raise AssertionError('Stale generation must not call the model')
    monkeypatch.setattr(FacecamService, 'llm', unexpected)
    with pytest.raises(StaleFacecamRevision):
        FacecamService.generate(project.id, 'Accroche.', 'Opinion.', state.revision - 1)


def test_copy_is_independent_french_minimal_and_resets_integrations(source):
    project = FacecamService.create(source.id)
    assert (project.project_mode, project.publishing_type, project.library_type.value) == ('facecam','facecam','anime')
    assert (project.output_language, project.template, project.llm_preset) == ('fr','zoomed','gpt')
    assert project.source_project_id == source.id and project.mother_project_id is None
    assert project.drive_folder_id is None and project.scheduled_account_id is None
    assert Path(project.video_path).parent == ProjectService.get_project_dir(project.id)
    assert not (ProjectService.get_project_dir(project.id)/'new_script.json').exists()
    assert FacecamService.load(project.id).revision == 0
    assert ProjectService.load(source.id).drive_folder_id == 'private-folder'


@pytest.mark.parametrize('leading_raw,trailing_raw', [(False,False),(True,False),(False,True),(True,True)])
def test_approval_preserves_every_word_order_and_raw_boundaries(source,monkeypatch,leading_raw,trailing_raw):
    reference = ProjectService.load_transcription(source.id)
    if leading_raw: reference.scenes[0].is_raw = True
    if trailing_raw: reference.scenes[-1].is_raw = True
    if leading_raw and trailing_raw: reference.scenes[1].is_raw = False
    ProjectService.save_transcription(source.id, reference)
    project,state = draft(source)
    monkeypatch.setattr(FacecamService,'llm',range_model)
    result = FacecamService.approve(project.id,state.revision)
    assert ' '.join(r['text'] for r in result.assignment).split() == state.spoken_text().split()
    current = ProjectService.load_transcription(project.id)
    assert [r['text'] for r,s in zip(result.assignment,current.scenes) if s.is_raw] == [''] * sum(s.is_raw for s in reference.scenes)
    assert current.scenes[-1].text.endswith(ENDING)
    assert not current.scenes[-1].is_raw
    ProjectService.validate_transcription_scene_alignment(project.id,current)
    assert len(ProjectService.load_matches(project.id).matches) == len(current.scenes)
    assert FacecamService.reference(project.id) == reference


def test_invalid_assignment_keeps_approved_text_unchanged(source,monkeypatch):
    reference = ProjectService.load_transcription(source.id)
    reference.scenes[1].is_raw = False
    ProjectService.save_transcription(source.id, reference)
    project,state=draft(source)
    root = ProjectService.get_project_dir(project.id)
    files = ['facecam/state.json', 'facecam/reference.json', 'facecam/reference_matches.json',
             'transcription.json', 'matches.json', 'scenes.json']
    before = {name: (root / name).read_bytes() for name in files}
    monkeypatch.setattr(FacecamService,'llm',lambda *a, **kw: {'boundaries': []})
    with pytest.raises(ValueError, match='3 tentatives'):
        FacecamService.approve(project.id,state.revision)
    assert FacecamService.load(project.id).blocks == state.blocks
    assert FacecamService.load(project.id).approved_revision is None
    assert {name: (root / name).read_bytes() for name in files} == before
    assert not (root / 'new_script.json').exists()


def test_single_scene_blocks_need_no_model_during_approval(source, monkeypatch):
    project, state = draft(source)
    monkeypatch.setattr(FacecamService, 'llm', lambda *a, **kw: pytest.fail('No choice to make'))
    approved = FacecamService.approve(project.id, state.revision)
    assert approved.approved_revision == state.revision
    assert ' '.join(a['text'] for a in approved.assignment).split() == state.spoken_text().split()


@pytest.mark.parametrize('fail_second_block', [False, True])
def test_assignment_keeps_raw_separator_and_commits_only_after_all_blocks(source, monkeypatch, fail_second_block):
    reference = Transcription(language='fr', scenes=[
        SceneTranscription(scene_index=i, text=f'Une action dans le plan {i}.',
                           start_time=i*2, end_time=(i+1)*2, is_raw=i == 2) for i in range(5)])
    ProjectService.save_transcription(source.id, reference)
    ProjectService.save_scenes(source.id, SceneList(scenes=[
        Scene(index=i, start_time=i*2, end_time=(i+1)*2) for i in range(5)]))
    ProjectService.save_matches(source.id, MatchList(matches=[
        SceneMatch(scene_index=i, episode='episode.mp4', start_time=20+i*2, end_time=22+i*2,
                   confidence=1, speed_ratio=1) for i in range(5)]))
    project, state = draft(source)
    root = ProjectService.get_project_dir(project.id)
    files = ['facecam/state.json', 'transcription.json', 'matches.json', 'scenes.json']
    before = {name: (root / name).read_bytes() for name in files}
    calls = []
    def generate(pid, prompt, **kwargs):
        payload = json.loads(prompt.split('\n', 1)[1])
        calls.append(payload)
        if fail_second_block and len(calls) > 1:
            return {'boundaries': []}
        return range_model(pid, prompt)
    monkeypatch.setattr(FacecamService, 'llm', generate)
    if fail_second_block:
        with pytest.raises(ValueError, match='3 tentatives'):
            FacecamService.approve(project.id, state.revision)
        assert len(calls) == 4  # First block succeeded; second exhausted its budget.
        assert {name: (root / name).read_bytes() for name in files} == before
        assert not (root / 'new_script.json').exists()
    else:
        approved = FacecamService.approve(project.id, state.revision)
        assert len(calls) == 2
        assert [pair[1] for pair in calls[0]['words']] == (state.blocks[0].text + ' ' + state.blocks[1].text).split()
        assert [pair[1] for pair in calls[1]['words']] == state.blocks[3].text.split()
        assert approved.assignment[2] == {'scene_index': 2, 'text': ''}
        assert ' '.join(a['text'] for a in approved.assignment).split() == state.spoken_text().split()
        assert ProjectService.load_matches(project.id).matches[2].start_time == 24
        assert [s.is_raw for s in ProjectService.load_transcription(project.id).scenes] == [False, False, True, False, False, False]
        ProjectService.validate_transcription_scene_alignment(project.id, ProjectService.load_transcription(project.id))


def test_approval_rejects_stale_layout_before_model_call(source, monkeypatch):
    project, state = draft(source)
    state.blocks[1].scene_indices = [42]
    FacecamService.update(project.id, {'blocks': state.blocks})
    monkeypatch.setattr(FacecamService, 'llm', lambda *a, **kw: pytest.fail('Invalid layout'))
    with pytest.raises(ValueError, match='plans du script'):
        FacecamService.approve(project.id, state.revision)
    assert FacecamService.load(project.id).approved_revision is None


@pytest.mark.parametrize('valid_response', [True, False])
def test_edit_during_assignment_prevents_commit_and_retry(source, monkeypatch, valid_response):
    reference = ProjectService.load_transcription(source.id)
    reference.scenes[1].is_raw = False
    ProjectService.save_transcription(source.id, reference)
    project, state = draft(source)
    calls = []
    def generate(pid, prompt, **kwargs):
        calls.append(prompt)
        state.blocks[1].text = 'Un texte modifié pendant la répartition.'
        FacecamService.save_draft(pid, state.revision, state.blocks, state.selected_hook, state.selected_opinion)
        return range_model(pid, prompt) if valid_response else {'boundaries': []}
    monkeypatch.setattr(FacecamService, 'llm', generate)
    with pytest.raises(StaleFacecamRevision):
        FacecamService.approve(project.id, state.revision)
    assert len(calls) == 1
    current = FacecamService.load(project.id)
    assert current.revision == state.revision + 1 and current.approved_revision is None
    assert current.blocks[1].text == state.blocks[1].text
    assert not (ProjectService.get_project_dir(project.id) / 'new_script.json').exists()


def test_failed_source_alignment_saves_report_without_caching_invalid_words(source, monkeypatch, caplog):
    import contextlib
    from app.services.forced_alignment import ForcedAlignmentError, ForcedAlignmentService
    from app.services.transcriber import TranscriberService

    project, state = draft(source)
    root = FacecamService.directory(project.id) / 'recordings' / 'test'
    root.mkdir(parents=True)
    state.recording = {'directory': str(root)}
    state.assignment = [{'scene_index': 0, 'text': 'Un dragon noir. Puis elle revient.'}]
    cache = FacecamMediaService.alignment_cache(project.id, state)
    report = {'status': 'error', 'segments': [{'issues': ["Anomalously long word 'noir.'"]}],
              'global_issues': []}
    def fail(**kwargs):
        raise ForcedAlignmentError('Forced alignment quality check failed for single audio', report=report)
    monkeypatch.setattr(ForcedAlignmentService, '_align_single_audio', fail)
    monkeypatch.setattr(TranscriberService, 'model_session', lambda: contextlib.nullcontext())
    with pytest.raises(ForcedAlignmentError, match="pas assez fiable") as raised:
        FacecamMediaService.source_alignment(project.id, state, root, {'words': []})
    assert raised.value.report == report
    assert not cache.exists()
    assert json.loads(ForcedAlignmentService.report_path(cache.parent).read_text()) == report
    assert project.id in caplog.text and 'noir.' in caplog.text
    assert FacecamService.load(project.id).prepared is None


def test_edit_invalidates_recording_review_and_prevents_stale_result(source):
    project,state=draft(source)
    FacecamService.update(project.id, {'recording':{'transcript':state.spoken_text(),'reviewed_revision':state.revision}, 'prepared':{'revision':state.revision},'processed_signature':'old'})
    state.blocks[0].text='Une nouvelle accroche.'
    result=FacecamService.save_draft(project.id,state.revision,state.blocks,'','')
    assert result.prepared is None and result.processed_signature is None and result.approved_revision is None
    assert result.recording['reviewed_revision'] is None and result.recording['differences']
    with pytest.raises(StaleFacecamRevision): FacecamService.save_draft(project.id,state.revision,state.blocks,'','')
    with pytest.raises(ValueError): FacecamService.save_draft(project.id,result.revision,[b for b in result.blocks if b.kind!='raw'],'','')


def test_fact_checker_fails_closed_and_keeps_evidence(source,monkeypatch):
    from app.services.openrouter_service import OpenRouterService
    project,state=draft(source)
    monkeypatch.setattr(OpenRouterService,'research',lambda *a,**k: {'text':'Official evidence','sources':[{'url':'https://example.org/official','title':'Official'}]})
    monkeypatch.setattr(FacecamService,'llm',lambda *a: {'findings':[{'quote':'Naruto','kind':'contradiction','explanation':'Wrong claim','source_urls':['https://example.org/official','https://invented.test']}]})
    checked=FacecamService.fact_check(project.id)
    assert checked.fact_revision==state.revision and len(checked.findings[0].sources)==1
    def fail(*a,**k): raise RuntimeError('Research offline')
    monkeypatch.setattr(OpenRouterService,'research',fail)
    assert FacecamService.fact_check(project.id).fact_status=='unavailable'


def test_fact_context_preserves_between_shot_dialogue_and_uses_source_timestamps(source, monkeypatch, tmp_path):
    from app.services.anime_library import AnimeLibraryService, SubtitleSidecarEntry
    from app.services.facecam_fact_context import FacecamFactContext
    project, state = draft(source)
    reference = MatchList(matches=[SceneMatch(scene_index=i, episode=episode, start_time=start,
        end_time=end, confidence=1, speed_ratio=1) for i, (episode,start,end) in enumerate([
            ('episode06',120,180), ('episode06',340,360), ('episode07',10,12)])])
    (FacecamService.directory(project.id)/'reference_matches.json').write_text(reference.model_dump_json())
    # Approval/montage timings must not replace the immutable original source anchors.
    ProjectService.save_matches(project.id, MatchList(matches=[]))
    asset = tmp_path/'dialogue.srt'
    asset.write_text('1\n00:01:28,000 --> 00:01:31,000\n<i>Frontière</i>\n\n'
        '2\n00:02:00,000 --> 00:02:03,000\nNaruto est ici.\n\n'
        '3\n00:04:00,000 --> 00:04:03,000\nEntre les scènes.\n\n'
        '4\n00:06:29,000 --> 00:06:33,000\nFin du contexte.\n\n'
        '5\n00:09:00,000 --> 00:09:03,000\nHors contexte.\n', encoding='utf-8')
    tracks = [SubtitleSidecarEntry(stream_index=i, stream_position=i, codec_name='srt', language=lang,
        raw_language=lang, title=None, kind='text', asset_filename=asset.name) for i,lang in enumerate(['en','fr'])]
    monkeypatch.setattr(AnimeLibraryService, 'resolve_episode_path', lambda ep, **k: tmp_path/ep)
    monkeypatch.setattr(AnimeLibraryService, 'load_subtitle_sidecar_entries', lambda path: tracks if path.name=='episode06' else [])
    monkeypatch.setattr(AnimeLibraryService, 'get_subtitle_sidecar_asset_path', lambda *a: asset)
    context = FacecamFactContext.build(project.id)
    episode = context['episodes'][0]
    assert episode['language'] == 'fr' and episode['status'] == 'available'
    assert [(w['start'],w['end']) for w in episode['windows']] == [('00:01:30.000','00:06:30.000')]
    assert [cue['text'] for cue in episode['cues']] == ['Frontière','Naruto est ici.','Entre les scènes.','Fin du contexte.']
    assert episode['cues'][0]['start'] == '00:01:28.000'  # Preserve complete cue timestamps, not montage/clipped timing.
    assert episode['matched_scenes'][0]['original_text'] == 'Naruto est ici.'
    assert len(context['sources']) == 4
    assert context['episodes'][1]['status'] == 'unavailable'
    assert context['episodes'][1]['windows'][0]['start_seconds'] == 0
    monkeypatch.setattr(FacecamFactContext, 'MAX_CUES', 2)
    assert FacecamFactContext.build(project.id)['episodes'][0]['status'] == 'partial'


def test_fact_checker_passes_episode_evidence_to_both_stages_and_serves_citations(source, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from app.api.routes.facecam import fact_evidence
    from app.services.facecam_fact_context import FacecamFactContext
    from app.services.openrouter_service import OpenRouterService
    project, state = draft(source)
    subtitle = {'id':'cue1','url':f'/api/projects/{project.id}/facecam/fact-evidence/cue1',
        'title':'Épisode 06 · 00:02:10.000–00:02:12.000','kind':'subtitle','text':'Naruto est ici.','language':'fr'}
    context = {'padding_seconds':30,'episodes':[{'episode':'episode06','cues':[{'text':'Naruto est ici.'}]}],'sources':[subtitle]}
    monkeypatch.setattr(FacecamFactContext,'build',lambda *a:context)
    prompts = []
    def research(prompt, **kwargs):
        prompts.append(prompt)
        return {'text':'Evidence','sources':[{'url':'https://official.example','title':'Official'}]}
    def annotate(pid,prompt):
        prompts.append(prompt)
        return {'findings':[{'quote':'Naruto','kind':'contradiction','explanation':'Voir le dialogue.',
            'source_urls':[subtitle['url'],'/api/invented']} ]}
    monkeypatch.setattr(OpenRouterService,'research',research)
    monkeypatch.setattr(FacecamService,'llm',annotate)
    checked=FacecamService.fact_check(project.id)
    assert checked.fact_context == context and checked.fact_revision==state.revision
    assert checked.findings[0].sources == [subtitle]
    assert all('episode06' in p and 'Naruto est ici.' in p and 'détail purement visuel' in p for p in prompts)
    response=asyncio.run(fact_evidence(project.id,'cue1'))
    assert 'Naruto est ici.' in response.body.decode() and '00:02:10.000' in response.body.decode()
    with pytest.raises(HTTPException): asyncio.run(fact_evidence(project.id,'unknown'))


def test_cover_alias_lookup_deduplicates_and_refuses_ambiguity(source, monkeypatch):
    from app.services.facecam_cover_service import FacecamCoverService
    import app.services.facecam_cover_service as covers
    project,_=draft(source)
    project.anime_name='Kill Ao (Kill Blue)'
    ProjectService.save(project)
    media={'id':198113,'title':{'romaji':'Kill Ao','english':'KILL BLUE','native':'キルアオ'},'synonyms':[]}
    season2={'id':213628,'title':{'romaji':'Kill Ao 2nd Season','english':None},'synonyms':[]}
    queries=[]
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,url,json):
            queries.append(json['variables']['search'])
            data=[] if queries[-1]=='Kill Ao (Kill Blue)' else [media,season2]
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'data':{'Page':{'media':data}}})
    monkeypatch.setattr(covers.httpx,'Client',Client)
    results=FacecamCoverService.search(project.id)
    assert queries==['Kill Ao (Kill Blue)','Kill Ao','Kill Blue']
    assert results==[media,season2]
    assert FacecamCoverService.exact_matches(project.anime_name,results)==[media]
    monkeypatch.setattr(FacecamCoverService,'search',lambda *a:[media,{**media,'id':2}])
    with pytest.raises(ValueError,match='Choisissez'): FacecamCoverService.ensure(project.id)
    selected=[]
    monkeypatch.setattr(FacecamCoverService,'search',lambda *a:results)
    monkeypatch.setattr(FacecamCoverService,'select',lambda pid,mid:selected.append(mid))
    monkeypatch.setattr(FacecamCoverService,'render',lambda pid:'cached-cover')
    assert FacecamCoverService.ensure(project.id)=='cached-cover' and selected==[198113]
    cached=FacecamService.directory(project.id)/'cover.jpg'
    cached.write_bytes(b'cached')
    FacecamService.update(project.id,{'cover':{'path':str(cached)}})
    assert FacecamCoverService.ensure(project.id)=='cached-cover' and selected==[198113]


def test_comparison_ignores_case_and_punctuation_but_exposes_meaningful_changes():
    assert compare_words('Naruto, le ninja !','NARUTO le ninja.') == []
    assert compare_words('Naruto devient un ninja','Naruto est un grand ninja') == [
        {'kind':'replace','expected':'devient','spoken':'est'}, {'kind':'insert','expected':'','spoken':'grand'}]


def test_camera_cut_map_composes_raw_gap_without_drift():
    keep=keep_intervals(4,[(1,2)])
    assert keep==[(0,63),(118,240)]
    clean=sum(b-a for a,b in keep)/60
    playback=[SimpleNamespace(kind='audio',source_start=0,source_end=1,duration=1),
        SimpleNamespace(kind='silence',duration=2), SimpleNamespace(kind='audio',source_start=1,source_end=clean,duration=clean-1)]
    clips=camera_segments(keep,playback)
    assert clips[0]=={'start_frame':0,'end_frame':60,'source_frame':0}
    assert clips[1]['start_frame']==180 and clips[1]['source_frame']==60
    assert clips[-1]['end_frame']==sum(b-a for a,b in keep)+120
    assert all(c['end_frame']<=60 or c['start_frame']>=180 for c in clips)


def test_account_facecam_optin_is_separate_from_anime():
    from app.publishing_types import PublishingType
    from app.services.account_service import AccountConfig
    assert PublishingType.FACECAM != PublishingType.ANIME
    assert 'facecam' not in [PublishingType.ANIME]


def test_phone_stream_offsets_are_preserved_in_silent_camera_and_voice(source,monkeypatch,tmp_path):
    import contextlib
    import wave
    import array
    from app.services.facecam_media_service import ffmpeg,probe
    from app.services.transcriber import TranscriberService
    project,state=draft(source)
    FacecamService.update(project.id,{'approved_revision':state.revision})
    upload=tmp_path/'phone.mov'
    ffmpeg(['-f','lavfi','-i','color=black:s=160x240:r=60:d=1.2','-f','lavfi','-i','sine=frequency=1000:sample_rate=48000:duration=1',
        '-filter_complex',"[0:v]drawbox=color=white:t=fill:enable='gte(t,0.2)'[v];[1:a]asetpts=PTS+0.2/TB[a]",
        '-map','[v]','-map','[a]','-c:v','libx264','-c:a','pcm_s16le',str(upload)])
    monkeypatch.setattr(TranscriberService,'model_session',lambda:contextlib.nullcontext())
    monkeypatch.setattr(TranscriberService,'_transcribe_sync',lambda *a:([{'text':state.spoken_text(),'start':.2,'end':1.2}],'fr'))
    monkeypatch.setattr(FacecamService,'llm',lambda *a: {'suggestions': []})
    result=FacecamMediaService.ingest(project.id,upload)
    root=Path(result.recording['directory'])
    assert len(probe(root/'facecam.mp4')['streams'])==1
    assert probe(root/'facecam.mp4')['streams'][0]['codec_type']=='video'
    with wave.open(str(root/'voice.wav'),'rb') as wav:
        samples=array.array('h',wav.readframes(wav.getnframes()))
    onset=next(i for i,value in enumerate(samples) if abs(value)>500)/48000
    assert abs(onset-0.2)<=1/60
    assert result.recording['reviewed_revision'] is None
    assert result.recording['transcript'] == ''  # Comparison waits for cut approval.
    assert result.recording_edit.applied is False
    assert result.recording_edit.analysis_status == 'ready'
    assert (root/'original.mov').exists()
    # An identical take reuses both extracted streams and the speech analysis.
    import shutil
    from app.services import facecam_media_service as media
    source_mtime=(root/'voice.wav').stat().st_mtime_ns
    shutil.copy2(root/'original.mov',upload)
    def forbidden(*args,**kwargs):
        pytest.fail('Reimporting identical bytes must reuse the extracted audio and speech timing')
    monkeypatch.setattr(media,'ffmpeg',forbidden)
    monkeypatch.setattr(TranscriberService,'_transcribe_sync',forbidden)
    again=FacecamMediaService.ingest(project.id,upload)
    assert again.recording['fingerprint']==result.recording['fingerprint']
    assert again.recording_edit.id != result.recording_edit.id  # Even identical reuploads reopen cleanup.
    assert (root/'voice.wav').stat().st_mtime_ns==source_mtime
    # A changed take gets a new cache and must extract its own audio.
    shutil.copy2(root/'original.mov',upload)
    with upload.open('ab') as stream: stream.write(b'new take')
    calls=[]
    def track(args):
        calls.append(args)
        return ffmpeg(args)
    monkeypatch.setattr(media,'ffmpeg',track)
    monkeypatch.setattr(TranscriberService,'_transcribe_sync',lambda *a:([{'text':state.spoken_text(),'start':.2,'end':1.2}],'fr'))
    changed=FacecamMediaService.ingest(project.id,upload)
    assert changed.recording['fingerprint']!=result.recording['fingerprint']
    assert len(calls)==3  # Audio, silent camera and browser-compatible preview (video stream copy).
    assert Path(calls[0][-1]).name=='voice.tmp.wav'


@pytest.mark.parametrize('retimed', [False, True])
@pytest.mark.parametrize('edited', [False, True])
@pytest.mark.parametrize('extra_illustrations', [False, True])
def test_package_expands_subtitles_and_contains_original_media(source,monkeypatch,tmp_path,retimed,edited,extra_illustrations):
    from app.services.export_service import ExportService
    from app.services.facecam_cover_service import FacecamCoverService
    from PIL import Image
    import io
    project,state=draft(source)
    root=FacecamService.directory(project.id)/'recordings'/'test';root.mkdir(parents=True)
    for name in ['facecam.mp4','original.mov']:(root/name).write_bytes(b'media')
    source_directory = root
    if edited:
        root = root / 'edits' / 'cut'; root.mkdir(parents=True)
        (root / 'facecam.mp4').write_bytes(b'edited camera')
    prepared={'revision':state.revision,'fingerprint':'test'}
    if retimed:
        work=root/'retimed';work.mkdir()
        (work/'facecam.mp4').write_bytes(b'retimed camera')
        prepared['camera_path']=str(work/'facecam.mp4')
    FacecamService.update(project.id,{'approved_revision':state.revision,'prepared':prepared,
        'recording':{'directory':str(root),'source_directory':str(source_directory),'original_name':'original.mov','fingerprint':'test'}})
    picture=io.BytesIO();Image.new('RGB',(200,300),'red').save(picture,'PNG')
    FacecamCoverService.store(project.id,picture.getvalue(),{'manual':True})
    output=ExportService.get_output_dir(project.id);output.mkdir(parents=True,exist_ok=True)
    editor_payload = {}
    if extra_illustrations:
        references = FacecamService.source_matches(project.id)
        references.matches[2].episode = 'extra.mp4'
        (FacecamService.directory(project.id) / 'reference_matches.json').write_text(references.model_dump_json())
        editor_payload = {'editor_track': {'clips': [{'kind': 'extra', 'source_scene_index': 2, 'clipName': 'extra.mp4'}]}}
    (output/'import_project.jsx').write_text('var FACECAM = ' + json.dumps(editor_payload) + '; var scenes = [{"clipName":"episode"}];')
    (output/'tts_edited.wav').write_bytes(b'voice')
    subtitle=output/'subtitles.srt';subtitle.write_text('timing')
    (output/'subtitles').mkdir();mogrt=output/'subtitles'/'word.mogrt';mogrt.write_bytes(b'mogrt')
    timing=output/'subtitles'/'subtitle_timings.srt';timing.write_text('timing')
    episode=tmp_path/'episode.mp4';episode.write_bytes(b'episode')
    extra=tmp_path/'extra.mp4';extra.write_bytes(b'extra illustration episode')
    monkeypatch.setattr(ExportService,'subtitle_path',lambda p:subtitle)
    def collect_sources(project, matches):
        assert [m.episode for m in matches] == (['extra.mp4'] if extra_illustrations else [])
        return [episode, extra] if extra_illustrations else [episode]
    monkeypatch.setattr(ExportService,'_collect_episode_sources',collect_sources)
    monkeypatch.setattr(ExportService,'get_required_import_assets',lambda *a:['TikTok60fps.sqpreset'])
    monkeypatch.setattr(ExportService,'_resolve_selected_music_path',lambda *a:None)
    monkeypatch.setattr(ExportService,'_collect_baked_subtitle_files',lambda *a:[mogrt])
    monkeypatch.setattr(ExportService,'_collect_subtitle_timing_files',lambda *a:[timing])
    monkeypatch.setattr(ExportService,'_build_subtitles_archive_entry',lambda *a,**k:pytest.fail('No nested ZIP for Facecam'))
    FacecamService.update(project.id,{'processed_signature':FacecamService.processing_signature(project.id)})
    bundle=ExportService.build_bundle(project,[])
    assert bundle.name==ExportService.output_folder_name(project)+'.zip'
    with zipfile.ZipFile(bundle) as archive:
        names=archive.namelist();folder=ExportService.output_folder_name(project)+'/'
        for name in ['sources/episode.mp4','sources/facecam.mp4','sources/anime_reveal.png','recording/original.mov','subtitles/word.mogrt','subtitles/subtitle_timings.srt','run_in_premiere.bat','script.txt']:
            assert folder+name in names
        assert not any(name.endswith('.zip') for name in names)
        assert archive.read(folder+'script.txt').decode()==state.spoken_text()
        assert archive.read(folder+'sources/facecam.mp4')==(b'retimed camera' if retimed else b'edited camera' if edited else b'media')
        assert archive.read(folder+'recording/original.mov') == b'media'
        if extra_illustrations:
            assert archive.read(folder+'sources/extra.mp4') == b'extra illustration episode'
    FacecamService.save_draft(project.id,state.revision,state.blocks,'','')
    with pytest.raises(ValueError,match='Relancez'): ExportService.build_bundle(project,[])


def test_facecam_acl_changes_only_project_folder_and_is_idempotent(monkeypatch):
    from unittest.mock import MagicMock
    from app.services.facecam_delivery_service import FacecamDeliveryService
    from app.services.google_drive_service import GoogleDriveService
    drive=MagicMock()
    monkeypatch.setattr(GoogleDriveService,'_execute_with_retries',lambda function,**kw:function())
    drive.permissions().list().execute.return_value={'permissions':[]}
    FacecamDeliveryService.share_folder(drive,'project-folder')
    body=drive.permissions().create.call_args.kwargs
    assert body['fileId']=='project-folder' and body['body']=={'type':'anyone','role':'writer','allowFileDiscovery':False}
    drive.permissions().list().execute.return_value={'permissions':[{'id':'anyone','type':'anyone','role':'writer'}]}
    FacecamDeliveryService.share_folder(drive,'project-folder')
    update=drive.permissions().update.call_args.kwargs
    assert update['fileId']=='project-folder' and update['permissionId']=='anyone'
    assert drive.permissions().create.call_count==1


def test_premiere_camera_clips_and_cover_have_frame_exact_ends():
    import subprocess
    from app.services.processing import ProcessingService
    template=ProcessingService.PREMIERE_JSX_TEMPLATE_PATH.read_text()
    place='function placeFacecam'+template.split('  function placeFacecam',1)[1].split('\n  function ',1)[0]
    harness='''
const assert=require('node:assert/strict');
var FACECAM={filename:'facecam.mp4',cover:'anime_reveal.png',scale:100,cover_start:5,camera_segments:[{start_frame:0,end_frame:60,source_frame:30},{start_frame:180,end_frame:300,source_frame:90}]};
var TRACK_ITEM_WAIT_MAX_MS=1,placed=[];
function getOrImportClip(name){return {name,setInPoint(s){this.in=s},setOutPoint(s){this.out=s},clearInPoint(){},clearOutPoint(){}};}
var sequence={videoTracks:Array.from({length:5},(_,index)=>({overwriteClip(clip,start){placed.push({index,start,sourceIn:clip.in,sourceOut:clip.out,name:clip.name})}}))};
function resolvePlacedItemFast(){return placed.at(-1)}
function stripKnownExtension(s){return s.split('.')[0]}
function setTrackItemEndSeconds(item,end){item.end=end;return true;}
function getTrackItemEndSeconds(item){return item.end;}
function setScaleOnItem(item,scale){item.scale=scale;}
'''+place+'''
placeFacecam(sequence);
assert.deepEqual(placed.map(p=>p.index),[3,3,2]);
assert.deepEqual(placed.map(p=>[p.start,p.end]),[[0,1],[3,5],[5,6]]);
assert.deepEqual(placed.slice(0,2).map(p=>[p.sourceIn,p.sourceOut]),[[0.5,1.5],[1.5,3.5]]);
assert.equal((placed[2].end-placed[2].start)*60,60);
setTrackItemEndSeconds=()=>false;
assert.throws(()=>placeFacecam(sequence),/duration/);
'''
    subprocess.run(['node','-e',harness],check=True,capture_output=True,text=True)


def test_responses_preserves_provider_citations_and_omits_sampling(monkeypatch):
    import httpx
    from app.services.openrouter_service import OpenRouterService
    from app.models.llm_config import LLMPresetEntry, GeminiThinking
    entry=LLMPresetEntry(openrouter_id='openai/gpt-6-astra',thinking=GeminiThinking(effort='low'),max_output_tokens=8192)
    monkeypatch.setattr(OpenRouterService,'_resolve_entry',lambda **kw:entry)
    def respond(request):
        body=json.loads(request.content)
        assert request.url.path.endswith('/responses')
        assert body['tools'][0]['type']=='openrouter:web_search'
        assert body['reasoning']['effort']=='low' and body['max_output_tokens']==8192
        assert not {'temperature','top_p','logprobs'} & body.keys()
        return httpx.Response(200,json={'output':[{'type':'message','content':[{'type':'output_text','text':'Verified.',
            'annotations':[{'type':'url_citation','url':'https://example.org/source','title':'Evidence'}]}]}]})
    client=httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx,'Client',lambda **kw:client)
    assert OpenRouterService.research('Verify')['sources']==[{'url':'https://example.org/source','title':'Evidence'}]


@pytest.mark.parametrize('speed', [1.0, 1.25])
def test_preview_reuses_one_voice_camera_timeline_with_original_dialogue(source,monkeypatch,speed):
    import array, contextlib, math, wave
    from app.models import Word
    from app.services.processing import ProcessingService
    from app.services.forced_alignment import ForcedAlignmentService
    from app.services.transcriber import TranscriberService
    from app.services import facecam_media_service as media
    from app.models.facecam import FacecamAutoEditorSettings
    project,state=draft(source)
    monkeypatch.setattr(FacecamService,'llm',range_model)
    state=FacecamService.approve(project.id,state.revision)
    root=FacecamService.directory(project.id)/'recordings'/'test';root.mkdir(parents=True)
    with wave.open(str(root/'voice.wav'),'wb') as wav:
        wav.setparams((1,2,48000,0,'NONE','not compressed'))
        wav.writeframes(array.array('h',(int(8000*math.sin(2*math.pi*440*i/48000)) for i in range(96000))).tobytes())
    FacecamService.update(project.id,{'recording':{'fingerprint':'test','directory':str(root),'reviewed_revision':state.revision},
        'auto_editor':FacecamAutoEditorSettings(speed=speed)})
    # Source timing/cuts are always at native speed. Only the subsequent audio
    # and camera clocks change, after source alignment and before raw-dialogue inserts.
    clean_duration=2/speed
    if speed!=1.0:
        def retime(clean,camera,work,requested):
            assert requested==speed
            voice=work/'voice_speed.wav';video=work/'facecam.mp4'
            with wave.open(str(clean)) as src,wave.open(str(voice),'wb') as dst:
                dst.setparams(src.getparams());dst.writeframes(src.readframes(round(clean_duration*48000)))
            return voice,video,1/speed
        monkeypatch.setattr(FacecamMediaService,'apply_speed',retime)
    reference=ProjectService.load_transcription(project.id)
    aligned=reference.model_copy(deep=True)
    narrated=[s for s in aligned.scenes if not s.is_raw]
    assigned={a['scene_index']:a['text'] for a in state.assignment}
    for index,scene in enumerate(narrated):
        text=assigned[scene.scene_index];words=text.split();start=index*2/len(narrated);end=(index+1)*2/len(narrated)
        scene.text=text;scene.start_time=start;scene.end_time=end
        scene.words=[Word(text=w,start=start+(end-start)*i/len(words),end=start+(end-start)*(i+1)/len(words)) for i,w in enumerate(words)]
    align_calls=[]
    def align(**kwargs):
        align_calls.append(kwargs['edited_audio_path'])
        assert kwargs['manifest']['mode']=='single_audio'
        assert all(part['text'] for part in kwargs['manifest']['segments'])
        assert kwargs['edited_audio_path'] == root/'voice.wav'
        assert kwargs['coarse_words'] == [{'start':0,'end':2,'text':'Voix'}]
        return SimpleNamespace(transcription=aligned,report={"status":"complete"})
    monkeypatch.setattr(ForcedAlignmentService,'_align_single_audio',align)
    monkeypatch.setattr(TranscriberService,'model_session',lambda:contextlib.nullcontext())
    monkeypatch.setattr(FacecamMediaService,'speech_timing',lambda root:{'words':[{'start':0,'end':2,'text':'Voix'}]})
    monkeypatch.setattr(media,'probe',lambda path:{'format':{'duration':60}})
    result=FacecamMediaService.prepare(project.id)
    work=Path(result.prepared['directory']);transcription=Transcription.model_validate_json((work/'transcription.json').read_text())
    raw=next(s for s in transcription.scenes if s.is_raw)
    assert abs((raw.end_time-raw.start_time)-2)<1/60
    clips=result.prepared['camera_segments']
    assert all(c['end_frame']/60<=raw.start_time+1/60 or c['start_frame']/60>=raw.end_time-1/60 for c in clips)
    assert sum(c['end_frame']-c['start_frame'] for c in clips)==round(clean_duration*60)
    assert result.prepared['camera_time_scale']==1/speed
    assert Path(result.prepared['camera_path'])==(work/'facecam.mp4' if speed!=1.0 else root/'facecam.mp4')
    voice_duration=ProcessingService._probe_wav_duration(work/'narration.wav')
    assert abs(voice_duration-(clean_duration+2))<1/60
    assert 0<=result.prepared['cover_start']-voice_duration<1/60
    assert result.prepared['scenes'][-1]['duration_frames']==60
    assert FacecamMediaService.prepare(project.id).prepared==result.prepared
    assert len(align_calls)==1
    # Changing only the tuning must rebuild audio using cached alignment without a new
    # recording, script revision, approval, or discrepancy review.
    stale_processing=ProcessingService.get_output_dir(project.id)/'processing_state.json'
    stale_processing.parent.mkdir(exist_ok=True)
    stale_processing.write_text('{}')
    FacecamService.update(project.id,{'auto_editor':FacecamAutoEditorSettings(min_silence_ms=200,speed=speed)})
    updated=FacecamMediaService.prepare(project.id)
    assert len(align_calls)==1
    assert not stale_processing.exists()
    assert updated.revision==result.revision
    assert updated.approved_revision==result.approved_revision
    assert updated.recording==result.recording
    assert updated.prepared['directory']!=result.prepared['directory']
    assert updated.prepared['auto_editor_signature']!=result.prepared['auto_editor_signature']
    assert updated.prepared['cut_report']['clipped_word_count']==0
    assert (work/'narration.wav').is_file()
    changed_state=updated.model_copy(deep=True)
    changed_state.assignment[0]['text']='Un autre texte.'
    assert FacecamMediaService.alignment_cache(project.id,changed_state)!=FacecamMediaService.alignment_cache(project.id,updated)


def test_facecam_schedule_requires_explicit_account_optin(source,monkeypatch):
    from app.services.scheduling_service import SchedulingService
    from app.services.account_service import AccountService,AccountConfig
    from app.publishing_types import PublishingType
    project=FacecamService.create(source.id)
    account=AccountConfig(id='anime',name='Anime',language='fr')
    monkeypatch.setattr(AccountService,'get_account',lambda _:account)
    with pytest.raises(ValueError,match='facecam'):
        SchedulingService._validate_duplication_restrictions(project,account.id,[])
    account.supported_types.append(PublishingType.FACECAM)
    SchedulingService._validate_duplication_restrictions(project,account.id,[])
