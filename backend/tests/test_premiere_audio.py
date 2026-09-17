"""Audio config, exported preset interpretation, and executable JSX contracts."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import PROJECT_ROOT
from app.services.premiere_audio import DYNAMICS_ID, LIMITER_ID, RAW_AUDIO_PRESET, load_raw_audio_effects
from app.services.processing import ProcessingService
from app.services.voice_config_service import VoiceConfigService
from test_processing_overlay_jsx import _template


PRESET = PROJECT_ROOT / "assets" / RAW_AUDIO_PRESET


def _voice_config(**extra):
    return {"default_voice_key": "test", "voices": {"test": {
        "display_name": "Test", "elevenlabs_voice_id": "id", **extra,
    }}}


@pytest.mark.parametrize("gain", [-96, -24, 0, 7.5, 15])
def test_voice_gain_accepts_attenuation_and_positive_boost(gain):
    assert VoiceConfigService._parse(_voice_config(volume_db=gain)).voices["test"].volume_db == gain


def test_existing_voice_config_defaults_to_unity():
    assert VoiceConfigService._parse(_voice_config()).voices["test"].volume_db == 0


@pytest.mark.parametrize("gain", [True, False, "7", None, float("nan"), float("inf"), -96.1, 15.1])
def test_invalid_voice_gain_is_rejected(gain):
    with pytest.raises(ValueError, match="volume_db"):
        VoiceConfigService._parse(_voice_config(volume_db=gain))


def test_preset_uses_only_stereo_and_orders_dynamics_before_limiter():
    effects = load_raw_audio_effects(PRESET)
    assert [effect["matchName"] for effect in effects] == [DYNAMICS_ID, LIMITER_ID]
    # The exported mono/5.1 variants have different ceiling values. Do not use them.
    assert effects[1]["params"][1]["value"] == pytest.approx(0.89901471138)
    assert effects[1]["params"][2]["value"] == pytest.approx(0.666666984558)
    params = effects[0]["params"]
    count = round(params[11]["value"] * 7)
    points = [(params[18 + i * 2]["value"] * 100 - 100,
               params[19 + i * 2]["value"] * 100 - 100) for i in range(count)]
    assert points == pytest.approx([(-100, -100), (-60, -60), (-40, -18), (-20, -12), (0, -10)])
    assert params[12]["value"] == 1  # linked stereo, no image wandering


@pytest.mark.parametrize("mutation", ["missing_stereo", "animated", "curve_layout", "extra_effect"])
def test_preset_rejects_ambiguous_or_unsupported_serialization(tmp_path, mutation):
    tree = ET.parse(PRESET)
    root = tree.getroot()
    if mutation == "missing_stereo":
        for c in root.findall("AudioFilterComponent"):
            c.find("AudioComponent/ChannelType").text = "0"
    elif mutation == "animated":
        root.find("AudioComponentParam/IsTimeVarying").text = "true"
    elif mutation == "curve_layout":
        for p in root.findall("AudioComponentParam"):
            if p.findtext("Name") == "Location1": p.find("Name").text = "Unknown"
    else:
        root.find("FilterPreset/FilterMatchName").text = "unknown-effect"
    path = tmp_path / "broken.prfpset"
    tree.write(path)
    with pytest.raises(ValueError): load_raw_audio_effects(path)


def _render(scenes, gain=0):
    return ProcessingService._render_jsx_from_template(
        project_id="audio_test", scenes=scenes, source_audio_policies={},
        source_fps_num=24000, source_fps_den=1001,
        subtitle_timing_relative_path="subtitles/subtitle_timings.srt",
        raw_scene_subtitle_timing_relative_path="raw_scene_subtitles/text_subtitles.srt",
        raw_scene_subtitle_mogrt_relative_dir="raw_scene_subtitles/text_mogrts",
        music_filename="music.wav", music_gain_db=-24, voice_gain_db=gain,
        template=_template(), overlay_title_enabled=False, overlay_category_enabled=False,
    )


def test_render_embeds_stereo_audio_values_and_voice_gain():
    jsx = _render([{"is_raw": True, "start": 2, "end": 4}], 15)
    effects = json.loads(re.search(r"var RAW_AUDIO_EFFECTS = (.*);", jsx)[1])
    assert effects == load_raw_audio_effects(PRESET)
    assert "var VOICE_GAIN_DB = 15.0;" in jsx
    assert "var MUSIC_GAIN_DB = -24;" in jsx


def test_no_raw_scenes_does_not_require_a_preset(monkeypatch):
    monkeypatch.setattr("app.services.premiere_audio.load_raw_audio_effects", lambda _: pytest.fail("No preset needed"))
    assert "var RAW_AUDIO_EFFECTS = [];" in _render([])


def test_generation_resolves_project_voice_gain(monkeypatch):
    from app.models.project import Project
    from app.models.transcription import Transcription
    from app.services.template_service import TemplateService

    monkeypatch.setattr(TemplateService, "get", lambda _: _template())
    monkeypatch.setattr(VoiceConfigService, "get_config", lambda **_: VoiceConfigService._parse(_voice_config(volume_db=7)))
    jsx = ProcessingService.generate_jsx_script(
        Project(id="test", template="classic", voice_key="test"), Transcription(language="fr", scenes=[]), [],
    )
    assert "var VOICE_GAIN_DB = 7.0;" in jsx


def test_export_includes_the_user_audio_preset(monkeypatch):
    from app.models.project import Project
    from app.services.export_service import ExportService
    from app.services.template_service import TemplateService

    monkeypatch.setattr(TemplateService, "get", lambda _: _template())
    assert RAW_AUDIO_PRESET in ExportService.get_required_import_assets(Project(id="test"))


def _run_js(script):
    node = shutil.which("node")
    if not node: pytest.skip("Node required for Premiere scripting contract tests")
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def _jsx_function_block(start, end):
    source = ProcessingService.PREMIERE_JSX_TEMPLATE_PATH.read_text()
    return source[source.index("  function " + start):source.index("  function " + end)]


def test_clip_gain_is_absolute_supports_positive_and_rejects_clamping():
    code = _jsx_function_block("scoreAudioGainProperty", "buildLoopedMusicBed")
    _run_js("""
        const assert = require('node:assert/strict');
        var AUDIO_GAIN_RAW_FALLBACK_AT_0DB = 0.1778279410038923;
        function log() {}
        function setPropertyValueFast(p, v) { p.setValue(v); return true; }
    """ + code + """
        var value = 0.1778279410038923;
        var prop = { displayName: 'Niveau', getValue: () => value, setValue: v => value = v };
        var props = [prop]; props.numItems = 1;
        var components = [{ displayName: 'Volume', properties: props }]; components.numItems = 1;
        var clip = { name: 'Narration', components };
        for (var gain of [-96, -24, 0, 7.5, 15, 7.5, 0]) {
            assert.equal(applyTrackItemGainDb(clip, gain), true);
            assert.ok(Math.abs(20 * Math.log10(value / 0.1778279410038923) - gain) < 0.00001);
        }
        // Simulate a host silently clamping at unity instead of accepting boost.
        prop.setValue = v => value = Math.min(v, 0.1778279410038923);
        assert.equal(applyTrackItemGainDb(clip, 15), false);
        prop.getValue = () => { throw Error('stale property'); };
        assert.equal(applyTrackItemGainDb(clip, 0), false);
    """)


def test_effect_parameters_reject_silent_host_clamping():
    code = _jsx_function_block("writeRawAudioEffectParameters", "applyRawAudioEffects")
    _run_js("""
        const assert = require('node:assert/strict');
        function setPropertyValueFast(p,v) { p.value = Math.min(v, 0.5); return true; }
    """ + code + """
        var prop = { displayName: 'Amplitude maximale', getValue() { return this.value; } };
        var properties = [prop]; properties.numItems = 1;
        var spec = { names: ['Limiter'], params: [{index: 0, name: 'Amplitude maximale', value: 0.9}] };
        assert.throws(() => writeRawAudioEffectParameters({properties}, spec), /rejected/);
        properties.numItems = 2;
        assert.throws(() => writeRawAudioEffectParameters({properties}, spec), /layout/);
    """)


def test_retained_raw_audio_can_live_beyond_the_reserved_zone():
    code = _jsx_function_block("retainedRawAudioTracks", "levelRawSceneAudio")
    _run_js("""
        const assert = require('node:assert/strict');
        var RAW_AUDIO_TRACK_START_INDEX = 3, RAW_AUDIO_SUBCLIP_PREFIX = 'raw_';
        function track(names) {
            var clips=names.map(name=>({projectItem:{name}})); clips.numItems=clips.length;
            return {clips};
        }
        var audioTracks = [track(['episode']),track(['tts']),track(['music']),track([]),
                           track([]),track(['raw_scene_1']),track(['raw_scene_1'])];
        audioTracks.numTracks=audioTracks.length;
    """ + code + """
        assert.deepEqual(retainedRawAudioTracks({audioTracks}), [5,6]);
        audioTracks[5]=track(['raw_scene_1','unrelated']);
        assert.throws(()=>retainedRawAudioTracks({audioTracks}), /unrelated/);
    """)


@pytest.mark.parametrize("reject_effect", [False, True])
def test_raw_leveling_keeps_originals_in_nest_and_applies_ordered_effects(reject_effect):
    code = _jsx_function_block("rawAudioIntervals", "scoreAudioGainProperty")
    _run_js("""
        const assert = require('node:assert/strict');
        var SEQ_FPS=60, RAW_AUDIO_TRACK_START_INDEX=3, RAW_AUDIO_SUBCLIP_PREFIX='raw_';
        var TRACK_ITEM_WAIT_MAX_MS=100, PROJECT_ID='test';
        var $={sleep(){}}, applied=[], retained=[];
        function log(){}
        function secondsToTicks(s){return s*254016000000;}
        function time(s){return {seconds:s,ticks:String(secondsToTicks(s))};}
        function snapSecondsToFrame(s){return Math.round(s*60)/60;}
        function coll(items){Object.defineProperty(items,'numItems',{get(){return this.length;}});return items;}
        function track(clips){return {clips:coll(clips),setMute(){}};}
        function source(name,start,end){return {name,projectItem:{name,nodeId:name},start:time(start),end:time(end)};}
        function buildRawAudioSubclipName(s){return 'raw_'+s.scene_index;}
        function findTrackItemAtStart(t,start,name){return t.clips.find(c=>c.start.seconds===start&&c.name===name);}
        function resolvePlacedItemFast(t,start,name){return findTrackItemAtStart(t,start,name);}
        function getTrackItemEndSeconds(c){return c.end.seconds;}
        function getQEItemStartTicks(c){return c.start?Number(c.start.ticks):null;}
        function setTrackItemEndSeconds(c,end){c.end=time(end);return true;}
        function applyTrackItemGainDb(c,gain){assert.equal(gain,0);return true;}
        function clearTrackClips(t){t.clips.splice(0);}
        function setPropertyValueFast(p,v){p.value=v;return true;}
        var a2=source('tts',0,12), a3=source('music',0,12);
        var sequence={sequenceID:'main',audioTracks:[track([]),track([a2]),track([a3]),track([]),
            track([]),track([source('raw_1',2,4),source('raw_2',8,10)]),
            track([source('raw_1',2,4),source('raw_2',8,10)])]};
        sequence.audioTracks.numTracks=sequence.audioTracks.length;
        var app={project:{openSequence(id){assert.equal(id,'main');}}};
    """ + f"var RAW_AUDIO_EFFECTS={json.dumps(load_raw_audio_effects(PRESET))};\nvar rejectEffect={json.dumps(reject_effect)};\n" + code + """
        var nestedItem={nodeId:'nest',name:'ATR_RAW_AUDIO__test',
            setInPoint(s,type){assert.equal(type,2);this.start=s;},
            setOutPoint(s,type){assert.equal(type,2);this.end=s;},
            clearInPoint(){this.clearedIn=true;},clearOutPoint(){this.clearedOut=true;}};
        createRawAudioNest=function(seq,indexes,end){
            assert.deepEqual(indexes,[5,6]); assert.equal(end,12);
            retained=indexes.flatMap(i=>seq.audioTracks[i].clips.slice());
            return {name:nestedItem.name,projectItem:nestedItem};
        };
        sequence.audioTracks[3].overwriteClip=function(item,start){
            this.clips.push({name:item.name,projectItem:item,start:time(start),end:time(item.end),
                inPoint:time(item.start),outPoint:time(item.end),components:coll([])});
        };
        var qe={project:{getAudioEffectByName(name){return name;},getActiveSequence(){return {
            getAudioTrackAt(index){
                assert.equal(index,3);
                var items=sequence.audioTracks[index].clips;
                return {numItems:items.length,getItemAt(i){var clip=items[i];return {
                    name:clip.name,start:clip.start,addAudioEffect(id){
                        var spec=RAW_AUDIO_EFFECTS.find(e=>e.matchName===id);
                        applied.push(id);
                        var properties=coll(spec.params.map(p=>({displayName:p.name,value:0,
                            getValue(){return rejectEffect?NaN:this.value;}})));
                        clip.components.push({matchName:id,displayName:spec.names[0],properties});
                    }
                };}};
            }
        };}}};
        var scenes=[{is_raw:true,scene_index:1,start:2,end:4},{is_raw:true,scene_index:2,start:8,end:10}];
        if(rejectEffect) assert.throws(()=>levelRawSceneAudio(sequence,scenes,12),/Original clips retained/);
        else {
            levelRawSceneAudio(sequence,scenes,12);
            assert.deepEqual(applied,RAW_AUDIO_EFFECTS.map(e=>e.matchName).concat(RAW_AUDIO_EFFECTS.map(e=>e.matchName)));
            assert.deepEqual(sequence.audioTracks[3].clips.map(c=>[c.start.seconds,c.end.seconds,c.inPoint.seconds]),
                [[2,4,2],[8,10,8]]);
        }
        assert.equal(retained.length,4);
        assert.equal(sequence.audioTracks[1].clips[0],a2);
        assert.equal(sequence.audioTracks[2].clips[0],a3);
        assert.equal(sequence.audioTracks[5].clips.length,0);
        assert.equal(sequence.audioTracks[6].clips.length,0);
        assert.equal(nestedItem.clearedIn,true); assert.equal(nestedItem.clearedOut,true);
    """)


@pytest.mark.parametrize("failure", [None, "leak_narration", "lose_channel", "shift_timing", "non_stereo", "host_throws"])
def test_nest_preserves_channels_and_restores_targeting_on_failures(failure):
    code = _jsx_function_block("rawAudioIntervals", "findRawAudioEffectComponent")
    _run_js("""
        const assert = require('node:assert/strict');
        var SEQ_FPS = 60, RAW_AUDIO_TRACK_START_INDEX = 3, PROJECT_ID = 'test';
        function snapSecondsToFrame(s) { return Math.round(s * 60) / 60; }
        function moveItemToProjectBin() {}
        function coll(items) { items.numTracks = items.length; items.numItems = items.length; return items; }
        function clip(id, start, end) {
            return {projectItem:{nodeId:id}, start:{ticks:String(start)}, end:{ticks:String(end)},
                    inPoint:{ticks:'100'}, outPoint:{ticks:'200'}};
        }
        function track(items, targeted) {
            return {clips:coll(items), targeted, isTargeted() {return this.targeted;},
                    setTargeted(v) {this.targeted = v;}, setMute(v) {this.muted = v;}};
        }
        var originals = [clip('left', 120, 240), clip('right', 120, 240), clip('center', 120, 240)];
        var main = {sequenceID:'main', videoTracks:coll([track([clip('video',0,600)],true)]),
                    audioTracks:coll([track([clip('source',0,600)],false), track([clip('tts',0,600)],true),
                        track([clip('music',0,600)],false), ...originals.map(c=>track([c],false))]),
                    inPoint:'1.25', outPoint:'8.5', getInPoint(){return this.inPoint;}, getOutPoint(){return this.outPoint;},
                    setInPoint(v){this.inPoint=v;}, setOutPoint(v){this.outPoint=v;}};
        var app = {project:{activeSequence: main, openSequence(id) {assert.equal(id,'main'); this.activeSequence=main;}}};
        var nested;
    """ + f"var failure = {json.dumps(failure)};\n" + code + """
        main.createSubsequence = function(ignore) {
            assert.equal(ignore, false);
            assert.equal(this.inPoint, 0); assert.equal(this.outPoint, 10);
            assert.equal(this.videoTracks[0].targeted, false);
            assert.deepEqual(this.audioTracks.map(t=>t.targeted), [false,false,false,true,true,true]);
            if (failure === 'host_throws') throw Error('host failure');
            var copies = originals.map(c=>JSON.parse(JSON.stringify(c)));
            if (failure === 'leak_narration') copies.push(clip('tts',0,600));
            if (failure === 'lose_channel') copies.pop();
            if (failure === 'shift_timing') copies[0].start.ticks='121';
            nested = {sequenceID:'nest', projectItem:{}, videoTracks:coll([track([],false)]),
                      audioTracks:coll(copies.map(c=>track([c],false))),
                      getSettings(){return {audioChannelType: failure === 'non_stereo' ? 2 : 1, audioChannelCount:2};}};
            app.project.activeSequence=nested;
            return nested;
        };
        if (failure) assert.throws(()=>createRawAudioNest(main,[3,4,5],10));
        else {
            assert.equal(createRawAudioNest(main,[3,4,5],10),nested);
            assert.equal(nested.name,'ATR_RAW_AUDIO__test');
            assert.ok(nested.audioTracks.every(t=>t.muted===0));
        }
        assert.equal(app.project.activeSequence,main);
        assert.equal(main.inPoint,1.25); assert.equal(main.outPoint,8.5);
        assert.equal(main.videoTracks[0].targeted,true);
        assert.deepEqual(main.audioTracks.map(t=>t.targeted),[false,true,false,false,false,false]);
        assert.equal(main.audioTracks[3].clips[0],originals[0]);
        assert.deepEqual(rawAudioIntervals([
            {is_raw:true,start:4,end:6},{is_raw:true,start:2,end:4},
            {is_raw:false,start:6,end:8},{is_raw:true,start:8,end:10}
        ]),[{start:2,end:6},{start:8,end:10}]);
    """)
