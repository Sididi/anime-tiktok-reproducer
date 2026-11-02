"""Editorial isolation, source bounds, packaging, and executable Premiere contracts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import MatchList, Project, SceneMatch, SceneTranscription, Transcription
from app.models.facecam import FacecamState, ScriptBlock
from app.services.facecam_editor_track import FacecamEditorTrackService as Editor, IllustrationSelection
from app.services.facecam_service import FacecamService
from app.services.otio_timing import FrameRateInfo
from app.services.processing import ProcessingService, ResolvedSceneSource
from test_processing_overlay_jsx import _template


RATE = FrameRateInfo(timebase=24, ntsc=False)


def scene(index, start, end, source_in, source_out, **extra):
    return {"scene_index": index, "start": start, "end": end, "clipName": "episode",
            "source_in": source_in, "source_out": source_out, "source_in_frame": round(source_in * 24),
            "source_out_frame": round(source_out * 24), "is_raw": False, **extra}


def source(index, duration=1):
    return ResolvedSceneSource(index, Path("extra.mp4"), "extra", 240, round((10 + duration) * 24),
                               10, 10 + duration, duration)


def selection(*indices):
    return IllustrationSelection.model_validate({"decisions": [
        {"scene_index": i, "keep": True, "reason": "Illustration pertinente."} for i in indices]})


def references(count=6):
    return [SceneTranscription(scene_index=i, start_time=i * 2, end_time=i * 2 + 2,
                               text=f"Source {i}") for i in range(count)]


def mapping():
    return [{"output_scene_index": 0, "source_scene_index": 1, "kind": "body"},
            {"output_scene_index": 1, "source_scene_index": 5, "kind": "body"},
            {"output_scene_index": 2, "source_scene_index": 5, "kind": "outro"}]


def test_native_track_keeps_v3_anchors_and_editorial_trims_leaves_gaps_and_cuts_old_tails():
    scenes = [scene(0, 0, 4, 11, 13), scene(1, 4, 8, 20, 27), scene(2, 8, 9, 20, 27)]
    original = json.dumps(scenes)
    plan = Editor.place(scenes, mapping(), references(), selection(2, 4),
                        {2: source(2), 4: source(4, 6)}, 9, RATE)
    clips = plan["clips"]
    assert [(c["id"], c["start_frame"], c["end_frame"]) for c in clips] == [
        ("v3_0", 0, 60), ("source_2", 60, 120),  # A gap from 2 to 3 seconds is intentional.
        ("source_4", 180, 240), ("v3_1", 240, 480), ("v3_2", 480, 540),
    ]
    assert json.dumps(scenes) == original
    for clip in clips:
        assert clip["effective_speed"] == 1.0
        if clip["kind"] == "retained":
            old = scenes[clip["output_scene_index"]]
            for key in ("clipName", "source_in", "source_out", "source_in_frame", "source_out_frame"):
                assert clip[key] == old[key]
    assert plan["skipped"] == []


def test_original_dialogue_is_never_interrupted_by_an_extra_illustration():
    scenes = [scene(0, 0, 4, 10, 14, is_raw=True), scene(1, 4, 8, 20, 21)]
    origins = mapping()[:2]
    origins[0]["kind"] = "raw"
    plan = Editor.place(scenes, origins, references(), selection(2, 4),
                        {2: source(2), 4: source(4)}, 8, RATE)
    assert [c["id"] for c in plan["clips"]] == ["v3_0", "v3_1"]
    assert plan["clips"][0]["end_frame"] == 240
    assert [s["scene_index"] for s in plan["skipped"]] == [2, 4]


def test_frame_collisions_preserve_anchor_and_do_not_move_later_timeline():
    scenes = [scene(0, 0, 2 / 60, 10, 12), scene(1, 2 / 60, 4, 20, 21)]
    plan = Editor.place(scenes, mapping()[:2], references(), selection(2, 3, 4),
                        {i: source(i) for i in (2, 3, 4)}, 4, RATE)
    assert [(c["id"], c["start_frame"]) for c in plan["clips"]] == [
        ("v3_0", 0), ("source_2", 1), ("v3_1", 2)]
    assert len(plan["skipped"]) == 2


def test_ntsc_native_duration_is_never_extended_to_fill_a_gap():
    rate = FrameRateInfo(timebase=24, ntsc=True)
    scenes = [scene(0, 0, 10, 10, 11)]
    result = Editor.place(scenes, mapping()[:1], references(), selection(), {}, 10, rate)
    assert result["clips"][0]["end_frame"] == 60  # 24 source frames = 1.001 s, rounded inward.


@pytest.fixture
def editor_context(tmp_path, monkeypatch):
    project = Project(id="editor", project_mode="facecam", template="zoomed")
    state = FacecamState(revision=1, approved_revision=1, montage_map=mapping(),
                         blocks=[ScriptBlock(id="body", kind="body", text="Un héros cache son identité.", scene_indices=[1, 5])])
    refs = Transcription(language="fr", scenes=references())
    matches = MatchList(matches=[SceneMatch(scene_index=i, episode="episode", start_time=i * 2 + 10,
                                           end_time=i * 2 + 12, confidence=1, speed_ratio=1)
                                for i in range(6)])
    monkeypatch.setattr(FacecamService, "load", lambda _: state)
    monkeypatch.setattr(FacecamService, "reference", lambda _: refs)
    monkeypatch.setattr(FacecamService, "source_matches", lambda _: matches)
    monkeypatch.setattr(FacecamService, "selected_story", lambda *a: None)
    monkeypatch.setattr(FacecamService, "project", lambda _: project)
    monkeypatch.setattr(FacecamService, "directory", lambda _: tmp_path)
    calls = []

    def generate(_, prompt):
        calls.append(prompt)
        data = json.loads(prompt.splitlines()[-1])
        return {"decisions": [{"scene_index": c["scene_index"], "keep": c["scene_index"] != 3,
                               "reason": "Illustre le récit." if c["scene_index"] != 3 else "Branche supprimée."}
                              for c in data["candidates"]]}

    monkeypatch.setattr(FacecamService, "llm", generate)
    return state, refs, matches, calls


def test_selection_is_independent_cached_and_invalidates_when_script_or_sources_change(editor_context):
    state, refs, matches, calls = editor_context
    original_map = json.dumps(state.montage_map)
    result = Editor.selection("editor")
    assert [d.scene_index for d in result.decisions if d.keep] == [2, 4]
    assert len(calls) == 1
    assert Editor.selection("editor") == result
    assert len(calls) == 1
    # Changing voice speed does not reselect illustrations.
    state.auto_editor.speed = 1.25
    Editor.selection("editor")
    assert len(calls) == 1
    state.blocks[0].text = "Le héros rencontre des novices."
    Editor.selection("editor")
    assert len(calls) == 2
    matches.matches[2].start_time += 0.1
    Editor.selection("editor")
    assert len(calls) == 3
    refs.scenes[2].text = "Nouvelle description source."
    Editor.selection("editor")
    assert len(calls) == 4
    assert json.dumps(state.montage_map) == original_map
    assert state.revision == state.approved_revision == 1


@pytest.mark.parametrize("indices", [[2, 4], [2, 2, 4], [4, 3, 2], [2, 3, 99], [True, 3, 4]])
def test_model_cannot_omit_duplicate_reorder_or_invent_candidates(indices):
    with pytest.raises(ValueError):
        Editor.validate_selection({"decisions": [
            {"scene_index": i, "keep": True, "reason": "utile"} for i in indices]},
            [{"scene_index": i} for i in (2, 3, 4)])


def test_damaged_cache_is_regenerated_and_failed_selection_is_not_silently_accepted(editor_context, tmp_path, monkeypatch):
    _, _, _, calls = editor_context
    Editor.selection("editor")
    path = tmp_path / "editor_selection.json"
    cached = json.loads(path.read_text())
    cached["selection"]["decisions"][0]["scene_index"] = 99
    path.write_text(json.dumps(cached))
    Editor.selection("editor")
    assert len(calls) == 2
    path.unlink()
    monkeypatch.setattr(FacecamService, "llm", lambda *a: {"decisions": []})
    with pytest.raises(ValueError):
        Editor.selection("editor")
    assert not path.exists()


def render(facecam=None):
    return ProcessingService._render_jsx_from_template(
        project_id="editor", scenes=[], source_audio_policies={}, source_fps_num=24, source_fps_den=1,
        subtitle_timing_relative_path="subtitles/subtitle_timings.srt",
        raw_scene_subtitle_timing_relative_path="raw_scene_subtitles/text_subtitles.srt",
        raw_scene_subtitle_mogrt_relative_dir="raw_scene_subtitles/text_mogrts",
        music_filename="", music_gain_db=-24, template=_template(),
        overlay_title_enabled=True, overlay_category_enabled=True, facecam=facecam,
    )


def run_js(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                            env={k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"})
    assert result.returncode == 0, result.stderr


def test_non_facecam_exports_never_call_the_specialization(monkeypatch):
    monkeypatch.setattr("app.services.facecam_premiere.specialize_facecam_jsx",
                        lambda _: pytest.fail("Facecam specialization called for standard export"))
    jsx = render()
    assert "function placeFacecamEditorTrack" not in jsx
    assert "FACECAM ? 7 : 6" in jsx


@pytest.mark.parametrize("overlays", [False, True])
def test_facecam_track_topology_presets_and_card_are_shifted_together(overlays):
    jsx = render({"editor_track": {"clips": []}})
    layout = jsx[jsx.index("    ensureVideoTracks(sequence,"):jsx.index('    log(\n      "Reserved raw audio zone:')]
    preset = jsx[jsx.index("    // --- APPLY VIDEO PRESETS ---"):jsx.index('    perfEnd("presets", "Presets");')]
    camera = "function placeFacecam(sequence)" + jsx.split("  function placeFacecam(sequence)", 1)[1].split("\n  function ", 1)[0]
    run_js("var OVERLAY_ENABLED=" + json.dumps(overlays) + ";\n" + '''
const assert = require('node:assert/strict');
var trackCount=OVERLAY_ENABLED?7:5;
var sequence = {videoTracks: Array.from({length:trackCount}, (_,i)=>({index:i})), audioTracks:Array.from({length:4},()=>({}))};
sequence.videoTracks.numTracks=trackCount; sequence.audioTracks.numTracks=4;
var rawAudioTrackDesiredCount=4, RAW_AUDIO_TRACK_START_INDEX=3, qe={project:{getActiveSequence(){return {}}}};
var desiredTracks=0; function ensureVideoTracks(s,n){desiredTracks=n} function ensureAudioTracks(){}
''' + layout + '''
assert.equal(desiredTracks,trackCount);
assert.equal(categoryOverlayTrack,OVERLAY_ENABLED?sequence.videoTracks[5]:null);
assert.equal(titleOverlayTrack,OVERLAY_ENABLED?sequence.videoTracks[6]:null);
var applied=[], CATEGORY_OVERLAY_ENABLED=true, TITLE_OVERLAY_ENABLED=true;
var FOREGROUND_PRESET_NAME='foreground', FOREGROUND_PRESET_FILE_PATH='fg', BACKGROUND_PRESET_NAME='background', BACKGROUND_PRESET_FILE_PATH='bg';
var CATEGORY_TITLE_PRESET_NAME='overlay', CATEGORY_TITLE_PRESET_FILE_PATH='overlay', QE_TRACK_RESOLVE_CACHE={};
function placeFacecamEditorTrack(){} function verifyFacecamEditorTrack(){} function perfStart(){} function perfEnd(){} function log(){}
function applyVideoPresetToTrackItems(index,name){applied.push([index,name])}
''' + preset + '''
assert.deepEqual(applied,OVERLAY_ENABLED?[[0,'foreground'],[5,'overlay'],[6,'overlay']]:[[0,'foreground']]);
var FACECAM={filename:'facecam.mp4',cover:'anime_reveal.png',scale:100,cover_start:5,cover_end:6,camera_segments:[{start_frame:0,end_frame:300,source_frame:30}]};
var TRACK_ITEM_WAIT_MAX_MS=1, placed=[];
function getOrImportClip(name){return {name,setInPoint(s){this.in=s},setOutPoint(s){this.out=s},clearInPoint(){},clearOutPoint(){}}}
sequence.videoTracks.forEach(t=>t.overwriteClip=(clip,start)=>placed.push({index:t.index,start,name:clip.name}));
function resolvePlacedItemFast(){return placed.at(-1)} function stripKnownExtension(s){return s.split('.')[0]}
function setTrackItemEndSeconds(item,end){item.end=end;return true} function getTrackItemEndSeconds(item){return item.end}
function setScaleOnItem(item,scale){item.scale=scale;return true}
''' + camera + '''
placeFacecam(sequence); assert.deepEqual(placed.map(p=>[p.index,p.start,p.end]),[[2,0,5],[2,5,6]]);
''')
    assert "      4,\n      SUBTITLE_MOGRT_DIR," in jsx
    assert "overlayFadeTrackIndexes[overlayFadeIdx] + 1" in jsx


@pytest.mark.parametrize("overlays", [False, True])
def test_facecam_main_omits_legacy_video_layers_but_keeps_audio_and_subtitles(overlays):
    jsx = render({"editor_track": {"clips": []}})
    begin = jsx.index("  function main() {")
    main = jsx[begin:jsx.index("\n  // ========================================================================", begin)]
    run_js("var OVERLAY_ENABLED=" + json.dumps(overlays) + ";\n" + '''
const assert=require('node:assert/strict');
var FACECAM={cover_start:5,cover_end:6,editor_track:{clips:[{clipName:'extra'}]}};
// Non-native speeds and raw scenes must not recreate either removed video layer.
var scenes=[{scene_index:0,clipName:'episode',start:0,end:2,effective_speed:2},
            {scene_index:1,clipName:'episode',start:2,end:5,is_raw:true}];
var BATCH_SEQUENCE_NAME='test', SEQUENCE_PRESET_PATH='preset', PROJECT_BIN_NAME='project';
var AUDIO_FILENAME='narration.wav', MUSIC_FILENAME='music.wav', MUSIC_GAIN_DB=-24, VOICE_GAIN_DB=0;
var CATEGORY_OVERLAY_FILENAME='category.png', TITLE_OVERLAY_FILENAME='title.png';
var CATEGORY_OVERLAY_ENABLED=true, TITLE_OVERLAY_ENABLED=true, OVERLAY_FADE_DURATION_SEC=0.5;
var FOREGROUND_PRESET_NAME='foreground', FOREGROUND_PRESET_FILE_PATH='fg';
var CATEGORY_TITLE_PRESET_NAME='overlay', CATEGORY_TITLE_PRESET_FILE_PATH='overlay';
var SUBTITLE_MOGRT_DIR='subs', SUBTITLE_SRT_PATH='timings', RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR='raw';
var RAW_SCENE_TEXT_SUBTITLE_SRT_PATH='raw-timings', RAW_SCENE_SUBTITLE_MANIFEST_PATH='manifest';
var RAW_AUDIO_TRACK_START_INDEX=3, PERF_PROFILE_ENABLED=false, TRACK_ITEM_WAIT_MAX_MS=1;
function track(){return {clips:[],setMute(){},overwriteClip(item,start){this.clips.push({name:item.name,start,end:5})}}}
var sequence={videoTracks:[],audioTracks:[],markers:{createMarker(){return {}}}};
var app={enableQE(){},project:{activeSequence:sequence}};
var qe={project:{newSequence(){},getActiveSequence(){return {}}}};
function File(){this.exists=true;this.fsName='preset'}
function ensureProjectBin(){return {}}
function ensureVideoTracks(seq,n){seq.videoTracks=Array.from({length:n},track);seq.videoTracks.numTracks=n}
function ensureAudioTracks(seq,n){seq.audioTracks=Array.from({length:n},track);seq.audioTracks.numTracks=n}
function getRequiredRawAudioZoneWidthForScenes(){return 2}
function snapSecondsToFrame(s){return s} function trimSpaces(s){return s.trim()}
function stripKnownExtension(s){return s.split('.')[0]}
var preloaded,applied=[],fades=[],rawPresets=false,verified=false;
function preloadProjectItemsBatch(names){preloaded=Object.keys(names);return {}}
function hasProjectItemForName(){return true}
function getOrImportClip(name){return {name}}
function resolvePlacedItemFast(track){return track.clips[0]}
function getTrackItemEndSeconds(item){return item.end}
function applyTrackItemGainDb(item,gain){item.gain=gain;return true}
function cleanupAudioTracks(index,name){assert.equal(index,1);assert.equal(name,AUDIO_FILENAME)}
function placeFacecamEditorTrack(seq){seq.videoTracks[0].overwriteClip({name:'native-illustrations'},0)}
function verifyFacecamEditorTrack(seq){assert.equal(seq.videoTracks[0].clips.length,1);verified=true}
function clearRawAudioZone(seq,index,width){assert.equal(index,3);assert.equal(width,2)}
function duplicateRawSceneAudioToTrack(track,items,width){
  assert.equal(track,sequence.audioTracks[3]);assert.equal(items,scenes);assert.equal(width,2);
  track.overwriteClip({name:'raw-dialogue'},items[1].start);
}
function placeOverlayOnTrack(track,name,end){track.overwriteClip({name},0);track.clips[0].end=end;return track.clips[0]}
function applyOverlayFadeOut(index){fades.push(index)}
function buildLoopedMusicBed(track,item,end){track.overwriteClip(item,0);track.clips[0].end=end;return true}
function applyVideoPresetToTrackItems(index,name){applied.push([index,name])}
function importUnifiedSubtitles(seq,index){seq.videoTracks[index].overwriteClip({name:'subtitles'},0)}
function placeFacecam(seq){seq.videoTracks[2].overwriteClip({name:'camera'},0)}
function applyRawSceneAudioPreset(){rawPresets=true}
function finishFacecamEditorTrack(){} function refreshSequenceUI(){} function deleteSequenceByName(){}
function warmFullProjectMediaIndex(){} function warmProjectItemCache(){}
function perfStart(){} function perfEnd(){} function perfLogSummary(){} function log(){}
function ensureWhiteBorderImage(){assert.fail('Removed border must never be placed')}
function safeApplySpeedQE(){assert.fail('Removed montage must never be retimed')}
function validateAndRepairRawSceneVideoPlacement(){assert.fail('Removed montage must never be repaired')}
''' + main + '''
main();
assert.equal(sequence.videoTracks.length,OVERLAY_ENABLED?7:5);
assert.deepEqual(sequence.videoTracks.slice(0,5).map(t=>t.clips.map(c=>c.name)),
                 [['native-illustrations'],[],['camera'],[],['subtitles']]);
assert.deepEqual(applied,OVERLAY_ENABLED?[[0,'foreground'],[5,'overlay'],[6,'overlay']]:[[0,'foreground']]);
assert.deepEqual(fades,OVERLAY_ENABLED?[5,6]:[]);
if(OVERLAY_ENABLED){
  assert.equal(sequence.videoTracks[5].clips[0].name,CATEGORY_OVERLAY_FILENAME);
  assert.equal(sequence.videoTracks[6].clips[0].name,TITLE_OVERLAY_FILENAME);
}
assert.deepEqual(sequence.audioTracks.map(t=>t.clips.map(c=>c.name)),
                 [[],['narration.wav'],['music.wav'],['raw-dialogue'],[]]);
assert.equal(sequence.audioTracks[2].clips[0].end,6);
assert(preloaded.includes('extra'));assert(rawPresets);assert(verified);
''')


@pytest.mark.parametrize("mode", ["anime", "pure"])
def test_executable_video_only_placement_preserves_gaps_speed_and_empty_tracks(mode):
    code = (ProcessingService.PREMIERE_JSX_TEMPLATE_PATH.parent / "premiere_facecam_editor_track.jsx").read_text()
    scenes = [scene(0, 0, 4, 11, 13), scene(1, 4, 8, 20, 27), scene(2, 8, 9, 20, 27)]
    plan = Editor.place(scenes, mapping(), references(), selection(2, 4),
                        {2: source(2), 4: source(4, 6)}, 9, RATE)
    run_js('''
const assert=require('node:assert/strict');
var PROJECT_ID='test', PROJECT_EXECUTION_ID='run', SEQ_FPS=60, DEFAULT_FG_SCALE=76, TRACK_ITEM_WAIT_MAX_MS=1, QE_TRACK_RESOLVE_CACHE={};
var FACECAM={editor_track:''' + json.dumps(plan) + '''};
var $={global:{}}, cache={}, created=[], crops=[], activeSpeed=1;
function log(){} function getCachedProjectItem(n){return cache[n]} function findProjectItem(n){return cache[n]}
function buildRawTimeFromSourceFrame(frame){return {ticks:String(frame*1000)}}
function getOrImportClip(name){return {createSubClip(n,a,b,hard,v,audio){
 assert.equal(hard,0);assert.equal(v,1);assert.equal(audio,0);
 const result={name:n,duration:(Number(b)-Number(a))/24000};created.push(result);return result;
}}}
function moveItemToProjectBin(){} function cacheProjectItem(){} function cacheProjectItemByName(n,v){cache[n]=v}
function stripKnownExtension(n){return n}
function getSourceGeometry(){return {fg_scale:50,mode:''' + json.dumps(mode) + '''}}
function ensureCropOnSceneItem(item,geo,start,name,qe,cache,index,label){crops.push([index,label])}
function setScaleOnItem(item,s){item.scale=s;return true}
function getTrackItemStartSeconds(item){return item.start} function getTrackItemEndSeconds(item){return item.end}
function setTrackItemEndSeconds(item,end){item.end=end;return true}
function resolvePlacedItemFast(track,start,name){return track.clips.find(c=>c.start===start && c.name===name)}
function findTrackItemAtStart(track,start,name){return resolvePlacedItemFast(track,start,name)}
function makeTrack(){return {clips:[],muted:false,setMute(v){this.muted=!!v},isMuted(){return this.muted},overwriteClip(source,start){
 const end=start+source.duration, next=[];
 // Simulate normal overwrite retaining any tail outside the replacement.
 for(const old of this.clips){
   if(old.end<=start || old.start>=end){next.push(old);continue;}
   if(old.start<start)next.push({...old,end:start});
   if(old.end>end)next.push({...old,start:end});
 }
 next.push({name:source.name,start,end,getSpeed(){return activeSpeed},isSpeedReversed(){return false}});
 this.clips=next.sort((a,b)=>a.start-b.start);this.clips.numItems=this.clips.length;
}}}
var sequence={videoTracks:Array.from({length:5},makeTrack),audioTracks:[{sentinel:'narration'}]};
''' + code + '''
placeFacecamEditorTrack(sequence,{}); verifyFacecamEditorTrack(sequence);
assert.deepEqual(sequence.videoTracks[0].clips.map(c=>[c.start,c.end]),[[0,1],[1,2],[3,4],[4,8],[8,9]]);
assert(sequence.videoTracks[0].clips.every(c=>c.scale===50));
assert.deepEqual(crops,getSourceGeometry().mode==='pure'?Array.from({length:5},()=>[0,'V1']):[]);
assert.deepEqual(sequence.audioTracks,[{sentinel:'narration'}]);
assert.equal(created.length,5);
finishFacecamEditorTrack(sequence);
assert(sequence.videoTracks.every(t=>!t.muted));
assert.equal(sequence.videoTracks[1].clips.length,0);assert.equal(sequence.videoTracks[3].clips.length,0);
assert.equal(sequence.videoTracks[0].name,'Illustrations 100 %');
assert.equal(sequence.videoTracks[2].name,'Facecam');assert.equal(sequence.videoTracks[4].name,'Sous-titres');
assert.equal($.global.__ATR_IMPORT_WARNINGS__,undefined);
activeSpeed=0.5;assert.throws(()=>verifyFacecamEditorTrack(sequence),/100/);
''')


def test_export_uses_embedded_selection_and_validates_additional_media(editor_context, monkeypatch):
    from app.services.export_service import ExportService, ManifestEntry

    monkeypatch.setattr(FacecamService, "llm", lambda *a: pytest.fail("Export must not reselect"))
    jsx = 'var FACECAM = ' + json.dumps({"editor_track": {"clips": [
        {"kind": "retained", "clipName": "episode", "source_scene_index": 1},
        {"kind": "extra", "clipName": "extra", "source_scene_index": 2},
    ]}}) + '; var scenes = [{"clipName":"episode"}];'
    assert [m.scene_index for m in Editor.export_matches("editor", jsx)] == [2]
    paths = ["sources/episode.mp4", "sources/facecam.mp4", "sources/anime_reveal.png", "tts_edited.wav",
             "subtitles/subtitle_timings.srt", "assets/TikTok60fps.sqpreset", "run_in_premiere.bat"]
    entries = [ManifestEntry(relative_path="bundle/" + p, inline_content=b"") for p in paths]
    with pytest.raises(ValueError, match="extra"):
        ExportService.validate_facecam_manifest("bundle", entries, jsx)
    entries.append(ManifestEntry(relative_path="bundle/sources/extra.mp4", inline_content=b""))
    ExportService.validate_facecam_manifest("bundle", entries, jsx)


def test_reimport_indexes_episode_masters_instead_of_video_or_audio_subclips():
    jsx = render({"editor_track": {"clips": []}})

    def function(name):
        begin = jsx.index("  function " + name + "(")
        return jsx[begin:jsx.index("\n  function ", begin + 1)]

    run_js('''
const assert=require('node:assert/strict');
var PROJECT_ITEM_CACHE={};
function normalizeMediaPathCacheKey(path){return path}
function normalizeComparePath(path){return path}
function isBinItem(){return false}
function walkProjectItems(items,visit){items.forEach(visit)}
function getProjectItemMediaPath(item){return item.path}
function cacheProjectItem(item){cacheProjectItemByMediaPath(item.path,item)}
''' + function("isFacecamDerivedSubclip") + function("cacheProjectItemByMediaPath") +
           function("findProjectItemByMediaPathInContainer") + '''
var master={name:'episode',path:'episode.mp4'}, raw={name:'__ATR_RAW_AUDIO__old',path:'episode.mp4'}, video={name:'V1_100__new',path:'episode.mp4'}, legacy={name:'V4_100__old',path:'episode.mp4'};
[master,raw,video,legacy].forEach(cacheProjectItem);
assert.equal(PROJECT_ITEM_CACHE['episode.mp4'],master);
assert.equal(findProjectItemByMediaPathInContainer([legacy,video,raw,master],'episode.mp4'),master);
assert.equal(findProjectItemByMediaPathInContainer([legacy,video,raw],'episode.mp4'),null);
''')
