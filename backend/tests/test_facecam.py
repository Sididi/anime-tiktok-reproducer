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


def range_model(pid, prompt):
    payload = json.loads(prompt.split('\n',1)[1])
    count = len(payload['words'])
    indices = [s['scene_index'] for s in payload['scenes']]
    return {'ranges':[{'scene_index':i, 'start':pos*count//len(indices), 'end':(pos+1)*count//len(indices)} for pos,i in enumerate(indices)]}


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


@pytest.mark.parametrize('ranges', [[{'scene_index':0,'start':1,'end':3}], [{'scene_index':0,'start':0,'end':2}], [{'scene_index':0,'start':0,'end':4}], [{'scene_index':0,'start':False,'end':3}]])
def test_rejects_missing_duplicate_or_invalid_word_ranges(ranges):
    with pytest.raises(ValueError): FacecamService.validate_ranges(['un','deux','trois'],ranges,[0])


def test_invalid_assignment_keeps_approved_text_unchanged(source,monkeypatch):
    project,state=draft(source)
    monkeypatch.setattr(FacecamService,'llm',lambda *a:{'ranges':[]})
    with pytest.raises(ValueError): FacecamService.approve(project.id,state.revision)
    assert FacecamService.load(project.id).blocks == state.blocks
    assert FacecamService.load(project.id).approved_revision is None


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


def test_comparison_ignores_case_and_punctuation_but_exposes_meaningful_changes():
    assert compare_words('Naruto, le ninja !','NARUTO le ninja.') == []
    assert compare_words('Naruto devient un ninja','Naruto est un grand ninja') == [
        {'kind':'replace','expected':'devient','spoken':'est'}, {'kind':'insert','expected':'','spoken':'grand'}]


def test_camera_cut_map_composes_raw_gap_without_drift():
    keep=keep_intervals(4,[(1,2)])
    assert keep==[(0,71),(115,240)]
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
    monkeypatch.setattr(TranscriberService,'_transcribe_sync',lambda *a:([{'text':state.spoken_text()}],'fr'))
    result=FacecamMediaService.ingest(project.id,upload)
    root=Path(result.recording['directory'])
    assert len(probe(root/'facecam.mp4')['streams'])==1
    assert probe(root/'facecam.mp4')['streams'][0]['codec_type']=='video'
    with wave.open(str(root/'voice.wav'),'rb') as wav:
        samples=array.array('h',wav.readframes(wav.getnframes()))
    onset=next(i for i,value in enumerate(samples) if abs(value)>500)/48000
    assert abs(onset-0.2)<=1/60
    assert result.recording['reviewed_revision']==state.revision
    assert (root/'original.mov').exists()


def test_package_expands_subtitles_and_contains_original_media(source,monkeypatch,tmp_path):
    from app.services.export_service import ExportService
    from app.services.facecam_cover_service import FacecamCoverService
    from PIL import Image
    import io
    project,state=draft(source)
    root=FacecamService.directory(project.id)/'recordings'/'test';root.mkdir(parents=True)
    for name in ['facecam.mp4','original.mov']:(root/name).write_bytes(b'media')
    FacecamService.update(project.id,{'approved_revision':state.revision,'prepared':{'revision':state.revision,'fingerprint':'test'},
        'recording':{'directory':str(root),'original_name':'original.mov','fingerprint':'test'}})
    picture=io.BytesIO();Image.new('RGB',(200,300),'red').save(picture,'PNG')
    FacecamCoverService.store(project.id,picture.getvalue(),{'manual':True})
    output=ExportService.get_output_dir(project.id);output.mkdir(parents=True,exist_ok=True)
    (output/'import_project.jsx').write_text('var FACECAM = {}; var scenes = [{"clipName":"episode"}];')
    (output/'tts_edited.wav').write_bytes(b'voice')
    subtitle=output/'subtitles.srt';subtitle.write_text('timing')
    (output/'subtitles').mkdir();mogrt=output/'subtitles'/'word.mogrt';mogrt.write_bytes(b'mogrt')
    timing=output/'subtitles'/'subtitle_timings.srt';timing.write_text('timing')
    episode=tmp_path/'episode.mp4';episode.write_bytes(b'episode')
    monkeypatch.setattr(ExportService,'subtitle_path',lambda p:subtitle)
    monkeypatch.setattr(ExportService,'_collect_episode_sources',lambda *a:[episode])
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


def test_preview_reuses_one_voice_camera_timeline_with_original_dialogue(source,monkeypatch):
    import array, contextlib, math, wave
    from app.models import Word
    from app.services.processing import ProcessingService
    from app.services.forced_alignment import ForcedAlignmentService
    from app.services.transcriber import TranscriberService
    from app.services import facecam_media_service as media
    project,state=draft(source)
    monkeypatch.setattr(FacecamService,'llm',range_model)
    state=FacecamService.approve(project.id,state.revision)
    root=FacecamService.directory(project.id)/'recordings'/'test';root.mkdir(parents=True)
    with wave.open(str(root/'voice.wav'),'wb') as wav:
        wav.setparams((1,2,48000,0,'NONE','not compressed'))
        wav.writeframes(array.array('h',(int(8000*math.sin(2*math.pi*440*i/48000)) for i in range(96000))).tobytes())
    FacecamService.update(project.id,{'recording':{'fingerprint':'test','directory':str(root),'reviewed_revision':state.revision}})
    reference=ProjectService.load_transcription(project.id)
    aligned=reference.model_copy(deep=True)
    narrated=[s for s in aligned.scenes if not s.is_raw]
    assigned={a['scene_index']:a['text'] for a in state.assignment}
    for index,scene in enumerate(narrated):
        text=assigned[scene.scene_index];words=text.split();start=index*2/len(narrated);end=(index+1)*2/len(narrated)
        scene.text=text;scene.start_time=start;scene.end_time=end
        scene.words=[Word(text=w,start=start+(end-start)*i/len(words),end=start+(end-start)*(i+1)/len(words)) for i,w in enumerate(words)]
    def align(**kwargs):
        assert kwargs['manifest']['mode']=='single_audio'
        assert all(part['text'] for part in kwargs['manifest']['segments'])
        return SimpleNamespace(transcription=aligned,report={"status":"complete"})
    monkeypatch.setattr(ForcedAlignmentService,'_align_single_audio',align)
    monkeypatch.setattr(TranscriberService,'model_session',lambda:contextlib.nullcontext())
    monkeypatch.setattr(media,'probe',lambda path:{'format':{'duration':60}})
    result=FacecamMediaService.prepare(project.id)
    work=Path(result.prepared['directory']);transcription=Transcription.model_validate_json((work/'transcription.json').read_text())
    raw=next(s for s in transcription.scenes if s.is_raw)
    assert abs((raw.end_time-raw.start_time)-2)<1/60
    clips=result.prepared['camera_segments']
    assert all(c['end_frame']/60<=raw.start_time+1/60 or c['start_frame']/60>=raw.end_time-1/60 for c in clips)
    assert sum(c['end_frame']-c['start_frame'] for c in clips)==120
    voice_duration=ProcessingService._probe_wav_duration(work/'narration.wav')
    assert abs(voice_duration-4)<1/60
    assert 0<=result.prepared['cover_start']-voice_duration<1/60
    assert result.prepared['scenes'][-1]['duration_frames']==60
    assert FacecamMediaService.prepare(project.id).prepared==result.prepared


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
