// Simulated Premiere host: real preset functions run against these API objects.
const assert = require('node:assert/strict');
var SEQ_FPS = 60, TICKS_PER_SECOND = 254016000000, RAW_AUDIO_TRACK_START_INDEX = 3;
var RAW_AUDIO_PRESET_WARNING = '', messages = [], mutations = [], continued = false;
var pending = [], elapsedMs = 0;
var $ = {global: {__ATR_IMPORT_WARNINGS__: 'existing warning'}, sleep(ms) {
    elapsedMs += ms;
    while (pending.length && pending[0].at <= elapsedMs) pending.shift().run();
}};
function log(message) {
    if (failure === 'logging') throw Error('logging unavailable');
    messages.push(message);
}
function coll(items) {
    for (var key of ['numItems', 'numTracks']) {
        Object.defineProperty(items, key, {get() {return hostMode === 'string_counts' ? String(this.length) : this.length;}});
    }
    return items;
}
function time(seconds) {return {seconds, ticks: String(Math.round(seconds * TICKS_PER_SECOND))};}
function snapSecondsToFrame(s) {return Math.round(s * SEQ_FPS) / SEQ_FPS;}
function buildRawAudioSubclipName(s) {return 'raw_' + s.scene_index;}
function findTrackItemAtStart(track, start, name) {
    var clip = track.clips.find(c => c.name === name && Math.abs(c.start.seconds - start) < 0.001);
    if (clip && hostMode === 'stale_dom') return {...clip, components: coll(clip.components.slice())};
    return clip;
}
function getQEItemStartTicks(c) {return c && c.start ? Number(c.start.ticks) : null;}
function getSourceAudioChannelTypeName() {
    return channelMode === 'unknown' ? '' : channelMode === 'split51' ? '51' : channelMode;
}
function getSourceAudioSelectedChannelCount() {
    return channelMode === '51' || channelMode === 'split51' ? 6 : channelMode === 'mono' ? 1 : 2;
}
function getAudioChannelMappingWithRetry() {return null;}
function setPropertyValueFast(prop, value) {return prop.setValue(value);}
function source(name, start, end) {
    return {name, projectItem: {name, nodeId: name}, start: time(start), end: time(end),
        inPoint: time(363.237875), outPoint: time(363.237875 + end - start), fxVersion: 0,
        components: coll([{displayName: 'Volume', matchName: 'volume',
            properties: [{value: 0.1778279410038923}]}])};
}
function track(clips) {return {clips: coll(clips)};}
var scenes = [{is_raw: true, scene_index: 20, clipName: 'episode', start: 50.533333, end: 57.8},
    {is_raw: true, scene_index: 21, clipName: 'episode', start: 60, end: 62},
    {is_raw: false, scene_index: 22, start: 62, end: 65}];
var first = source('raw_20', 3032 / 60, 3032 / 60 + 174 * 1001 / 24000);
var second = source('raw_21', 60, 62);
var tracks = [track([source('raw_20', 3032 / 60, 57.8)]), track([source('tts', 0, 80)]),
    track([source('music', 0, 80)]), track([source('unrelated', 0, 80)]), track([]), track([first, second])];
if (channelMode === 'split51') {
    for (var i = 0; i < 5; i++) tracks.push(track([
        source('raw_20', first.start.seconds, first.end.seconds), source('raw_21', 60, 62)]));
}
var sequence = {sequenceID: 'main', audioTracks: coll(tracks), clone() {assert.fail('No clone');},
    createSubsequence() {assert.fail('No subsequence');}};
var app = {project: {activeSequence: sequence, openSequence() {assert.fail('No sequence switching');}}};
var runtimeChannel = channelMode === 'stereo' ? 1 : channelMode === '51' ? 2 : 0;
var specs = RAW_AUDIO_EFFECTS.filter(e => e.channelType === runtimeChannel);
var originals = tracks.flatMap(t => t.clips.map(c => ({clip: c, components: c.components,
    timing: JSON.stringify([c.projectItem, c.start, c.end, c.inPoint, c.outPoint]),
    volume: JSON.stringify(c.components[0])})));
var qe = {project: {
    getAudioEffectByName(name) {
        if (failure === 'lookup') throw Error('effect lookup unavailable');
        var effect = RAW_AUDIO_EFFECTS.find(s => s.matchName === name || s.names.includes(name));
        if (failure === 'lookup_second' && effect && effect.matchName === specs[1].matchName) return null;
        if (hostMode === 'catalog') return null;
        if (hostMode === 'truthy_noop' && effect && (name === effect.matchName || name === effect.names[0])) {
            return {name, noop: true};
        }
        return effect ? effect.matchName : null;
    },
    getAudioEffectList() {
        return hostMode === 'catalog' ? specs.map(s => ({name: s.names[0], id: s.matchName})) : [];
    },
    getActiveSequence() {
        if (failure === 'qe_sequence') throw Error('QE unavailable');
        return {getAudioTrackAt(index) {
            var clips = tracks[index].clips;
            return {numItems: clips.length + 1, getItemAt(itemIndex) {
                if (!itemIndex) return {name: 'gap', start: time(0)};
                var clip = clips[itemIndex - 1], version = clip.fxVersion;
                return {name: clip.name, start: clip.start,
                    addAudioEffect(id) {
                        assert.equal(version, clip.fxVersion, 'reacquire stale QE item');
                        if (id && id.noop) return true; // Truthy lookup and add return; no mutation.
                        if (id && id.id) id = id.id;
                        if (clip === first && failure === 'noop_insert') return true;
                        if (clip === first && (failure === 'insert' ||
                            (failure === 'second_insert' && id === specs[1].matchName))) throw Error('insert rejected');
                        var spec = specs.find(s => s.matchName === id);
                        assert.ok(spec, 'correct channel variant');
                        var component = {matchName: hostMode === 'runtime_identity' ? 'host-' + id : id,
                            displayName: hostMode === 'runtime_identity' ? 'Runtime ' + spec.names[0] : spec.names[0], effectId: id};
                        component.properties = coll(spec.params.map(p => ({displayName: p.name, value: 0,
                            setValue(value) {
                                if (clip === first) {
                                    if (p.index === 0 && value === true && failure === 'bypass') return false;
                                    if (p.index === 0 && value === false && failure === 'enable' && id === specs[1].matchName) return false;
                                    if (p.index === 1 && id === specs[1].matchName) {
                                        if (['write', 'logging', 'rollback'].includes(failure)) throw Error('parameter write rejected');
                                        if (failure === 'clamp') {this.value = value + 0.25; return true;}
                                    }
                                    if (p.index === 0 && value === true && failure === 'rollback' && this.bypassed) return false;
                                }
                                if (p.index === 0 && value === true) this.bypassed = true;
                                this.value = value; return true;
                            },
                            getValue() {return this.value;}
                        })));
                        if (clip === first && failure === 'layout') component.properties.pop();
                        var materialize = function () {
                            clip.components.push(component); clip.fxVersion++;
                            mutations.push({clip, id});
                        };
                        if (hostMode === 'late_insert') pending.push({at: elapsedMs + 1010, run: materialize});
                        else materialize();
                        if (clip === first && failure === 'insert_after_mutation') throw Error('host threw after inserting');
                    },
                    getComponentAt(index) {
                        return {name: clip.components[index].displayName, remove() {
                            if (failure === 'rollback') throw Error('removal rejected');
                            clip.components.splice(index, 1); clip.fxVersion++;
                        }};
                    }
                };
            }};
        }};
    }
}};
if (failure === 'no_raw') scenes = [{is_raw: false}];
if (failure === 'missing_preset') {RAW_AUDIO_EFFECTS = []; RAW_AUDIO_PRESET_WARNING = 'Saved preset missing';}
if (failure === 'missing_clip') tracks[5].clips.splice(0, 1);
if (failure === 'existing_effect') first.components.push({matchName: specs[0].matchName, displayName: specs[0].names[0], properties: []});
if (failure === 'host_property') Object.defineProperty(first, 'components', {get() {throw Error('stale clip');}});
if (failure === 'logging') Object.defineProperty($, 'global', {get() {throw Error('global reporting unavailable');}});
if (failure === 'outer_host') Object.defineProperty(sequence, 'audioTracks', {get() {throw Error('stale sequence');}});

// This call must never prevent the subsequent import/export steps from running.
applyRawSceneAudioPreset(sequence, scenes);
continued = true;
assert.equal(continued, true);
assert.equal(app.project.activeSequence, sequence);
for (var original of originals) {
    assert.equal(JSON.stringify([original.clip.projectItem, original.clip.start, original.clip.end,
        original.clip.inPoint, original.clip.outPoint]), original.timing);
    assert.equal(JSON.stringify(original.components[0]), original.volume);
}
assert.equal(mutations.some(m => !['raw_20', 'raw_21'].includes(m.clip.name) || m.clip === tracks[0].clips[0]), false);
var wholeStageSkipped = ['no_raw', 'missing_preset', 'lookup', 'lookup_second', 'qe_sequence', 'outer_host'].includes(failure) || channelMode === 'unknown';
if (wholeStageSkipped) assert.equal(mutations.length, 0);
else {
    // A failure on the first raw clip does not prevent the later one being processed.
    assert.deepEqual(second.components.slice(1).map(c => c.effectId), specs.map(s => s.matchName));
    for (var i = 0; i < specs.length; i++) {
        assert.deepEqual(second.components[i + 1].properties.map(p => p.value), specs[i].params.map(p => p.value));
    }
}
if (!failure && channelMode !== 'unknown') {
    for (var t of tracks.slice(3)) for (var clip of t.clips) {
        if (!['raw_20', 'raw_21'].includes(clip.name)) continue;
        assert.deepEqual(clip.components.slice(1).map(c => c.effectId), specs.map(s => s.matchName));
        for (var i = 0; i < specs.length; i++) {
            assert.deepEqual(clip.components[i + 1].properties.map(p => p.value), specs[i].params.map(p => p.value));
        }
    }
    var counts = tracks.map(t => t.clips.map(c => c.components.length));
    applyRawSceneAudioPreset(sequence, scenes);
    assert.deepEqual(tracks.map(t => t.clips.map(c => c.components.length)), counts, 'no duplicate effects on retry');
} else if (!wholeStageSkipped && !['missing_clip', 'existing_effect', 'host_property'].includes(failure)) {
    assert.ok(first.components.slice(1).every(c => c.properties[0].value === true), 'failed preset leaves audio bypassed');
}
if (failure !== 'logging') {
    assert.ok($.global.__ATR_IMPORT_WARNINGS__.startsWith('existing warning'));
    if (failure && failure !== 'no_raw' || channelMode === 'unknown') {
        assert.notEqual($.global.__ATR_IMPORT_WARNINGS__, 'existing warning', 'warning reaches the CEP result');
    }
    if (failure === 'noop_insert') {
        assert.ok($.global.__ATR_IMPORT_WARNINGS__.includes('components 1 -> 1'));
        assert.ok($.global.__ATR_IMPORT_WARNINGS__.includes('Hard Limiter'));
    }
}
