/**
 * Anime TikTok Reproducer - Premiere Pro 2025 Automation Script (v7.7 - EXTERNAL SUBTITLE MOGRT LOAD)
 *
 * CHANGES from v7.6:
 * - Removed all in-script subtitle MOGRT generation logic.
 * - Loads pre-generated subtitle MOGRT files from /subtitles.
 * - Uses subtitles.srt only for timeline timing (start/end).
 */

(function () {
  // ========================================================================
  // 1. CONFIGURATION
  // ========================================================================
  var ROOT_DIR = new File($.fileName).parent.fsName;
  var ASSETS_DIR = ROOT_DIR + "/assets";
  var SOURCES_DIR = ROOT_DIR + "/sources";

  var SEQUENCE_PRESET_PATH = ASSETS_DIR + "/TikTok60fps.sqpreset";
  var BORDER_MOGRT_PATH = ASSETS_DIR + "/White border 10px.mogrt";
  var AUDIO_FILENAME = "tts_edited.wav";
  var CATEGORY_OVERLAY_FILENAME = "category_overlay.png";
  var TITLE_OVERLAY_FILENAME = "title_overlay.png";
  var MUSIC_FILENAME = "credits song for my death.mp3";
  var MUSIC_GAIN_DB = -32;
  var PROJECT_PURGE_BIN_NAME = "__ATR_PURGE__";
  var BACKGROUND_PRESET_NAME = "SPM Anime Background";
  var BACKGROUND_PRESET_FILE_PATH =
    ASSETS_DIR + "/" + BACKGROUND_PRESET_NAME + ".prfpset";
  var FOREGROUND_PRESET_NAME = "SPM Anime Foreground";
  var FOREGROUND_PRESET_FILE_PATH =
    ASSETS_DIR + "/" + FOREGROUND_PRESET_NAME + ".prfpset";
  var SUBTITLE_MOGRT_DIR = ROOT_DIR + "/subtitles";
  var SUBTITLE_SRT_PATH = ROOT_DIR + "/subtitles.srt";

  // --- SCENES DATA ---
  var scenes = [
    {
      scene_index: 0,
      start: 0.0,
      end: 2.216667,
      text: "Ce type a décidé d'épouser une femme robot.",
      clipName: "[EMBER] Boku no Tsuma wa Kanjou ga Nai - 06",
      source_in_frame: 25523,
      source_out_frame: 25576,
      source_in: 1064.521792,
      source_out: 1066.732333,
      clip_duration: 2.2105,
      target_duration: 2.2167,
      speed_ratio: 0.9972,
      effective_speed: 0.9972,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 1,
      start: 2.216667,
      end: 4.15,
      text: "Ils ont même fini par avoir un gosse ensemble.",
      clipName: "[EMBER] Boku no Tsuma wa Kanjou ga Nai - 08",
      source_in_frame: 10566,
      source_out_frame: 10634,
      source_in: 440.69025,
      source_out: 443.526417,
      clip_duration: 2.8362,
      target_duration: 1.9333,
      speed_ratio: 1.467,
      effective_speed: 1.467,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 2,
      start: 4.15,
      end: 5.716667,
      text: "Mais il ne s'attendait pas à un truc :",
      clipName: "[EMBER] Boku no Tsuma wa Kanjou ga Nai - 08",
      source_in_frame: 8308,
      source_out_frame: 8346,
      source_in: 346.512833,
      source_out: 348.09775,
      clip_duration: 1.5849,
      target_duration: 1.5667,
      speed_ratio: 1.0116,
      effective_speed: 1.0116,
      leaves_gap: false,
      used_alternative: false,
    },
  ];

  // ========================================================================
  // 2. LOGGING & UTILS
  // ========================================================================
  function log(msg) {
    $.writeln("[ATR] " + msg);
  }
  function sleep(ms) {
    $.sleep(ms);
  }
  var TICKS_PER_SECOND = 254016000000; // Premiere Pro timebase constant
  var SEQ_FPS = 60; // TikTok preset is 60fps
  var SOURCE_FPS_NUM = 24000;
  var SOURCE_FPS_DEN = 1001;
  var TICKS_PER_FRAME = TICKS_PER_SECOND / SEQ_FPS;
  var TRACK_ITEM_WAIT_STEP_MS = 25;
  var TRACK_ITEM_WAIT_MAX_MS = 400;
  var SPEED_RETRY_WAIT_MS = 60;
  var PROJECT_ITEM_CACHE = {};
  var PROJECT_ITEM_CACHE_WARMED = false;
  var PRESET_FILE_TEXT_CACHE = {};
  var PRESET_EFFECT_VALUE_ENTRIES_CACHE = {};
  var LUMETRI_PRESET_VALUES_CACHE = {};
  var LUMETRI_PRESET_ARB_STRINGS_CACHE = {};
  var LUMETRI_LOOK_PATH_CACHE = {};
  var KNOWN_MEDIA_EXTENSIONS = {
    ".mkv": true,
    ".mp4": true,
    ".mov": true,
    ".avi": true,
    ".webm": true,
    ".m4v": true,
    ".wav": true,
    ".mp3": true,
    ".m4a": true,
    ".aac": true,
    ".flac": true,
    ".ogg": true,
    ".aiff": true,
    ".aif": true,
  };

  function snapSecondsToFrame(sec) {
    // Add small epsilon to prevent floating-point rounding errors
    // Values like 17.583333 (representing 1055/60) become 1054.99998 due to float precision
    // Without epsilon, this can cause 1-frame drift when truncated elsewhere
    return Math.round(sec * SEQ_FPS + 0.0001) / SEQ_FPS;
  }

  function secondsToTicks(sec) {
    // Snap to frame boundaries to avoid 1-frame drift
    // Add small epsilon to prevent floating-point rounding errors (same as snapSecondsToFrame)
    return Math.round(sec * SEQ_FPS + 0.0001) * TICKS_PER_FRAME;
  }

  function secondsToRawTicks(sec) {
    // Raw tick conversion (no sequence-frame snap), used for source in/out.
    return Math.round(sec * TICKS_PER_SECOND);
  }

  function sourceFramesToRawTicks(frame) {
    // Exact source frame -> ticks conversion (avoids decimal precision drift).
    return Math.round(
      (frame * TICKS_PER_SECOND * SOURCE_FPS_DEN) / SOURCE_FPS_NUM,
    );
  }

  function sourceFrameDurationTicks() {
    return (TICKS_PER_SECOND * SOURCE_FPS_DEN) / SOURCE_FPS_NUM;
  }

  function buildSequenceTimeFromSeconds(sec) {
    var t = new Time();
    try {
      // 2025: ticks might need to be a String or Number.
      // Safe to assign Number, PPro handles it.
      t.ticks = secondsToTicks(sec).toString();
    } catch (e) {
      t.seconds = sec;
    }
    return t;
  }

  function buildRawTimeFromSeconds(sec) {
    var t = new Time();
    try {
      t.ticks = secondsToRawTicks(sec).toString();
    } catch (e) {
      t.seconds = sec;
    }
    return t;
  }

  function buildRawTimeFromSourceFrame(frame, centerInFrame) {
    var t = new Time();
    var center = !!centerInFrame;
    var frameTicks = sourceFrameDurationTicks();
    var targetTicks =
      sourceFramesToRawTicks(frame) + (center ? Math.floor(frameTicks / 2) : 0);
    try {
      t.ticks = Math.round(targetTicks).toString();
    } catch (e) {
      // Fallback should be unreachable with valid frame numbers.
      t.ticks = secondsToRawTicks(
        (frame * SOURCE_FPS_DEN) / SOURCE_FPS_NUM,
      ).toString();
    }
    return t;
  }

  function getStartTicks(item) {
    if (!item || !item.start) return null;
    // PPro 2024/2025: .ticks is often a String. Parse it!
    if (item.start.ticks !== undefined) {
      var val = parseInt(item.start.ticks, 10);
      if (!isNaN(val)) return val;
    }
    // Fallback
    if (typeof item.start.seconds === "number") {
      return secondsToTicks(item.start.seconds);
    }
    return null;
  }

  function stripKnownExtension(name) {
    var txt = name ? name.toString() : "";
    var trimmed = txt.replace(/^\s+|\s+$/g, "");
    var dotPos = trimmed.lastIndexOf(".");
    if (dotPos <= 0) return trimmed;
    var ext = trimmed.substring(dotPos).toLowerCase();
    if (!KNOWN_MEDIA_EXTENSIONS[ext]) return trimmed;
    return trimmed.substring(0, dotPos);
  }

  function normalizeLooseName(name) {
    var txt = stripKnownExtension(name).toLowerCase();
    if (!txt) return "";
    txt = txt.replace(/[\u2018\u2019\u0060']/g, "_");
    txt = txt.replace(/[\\\/:\*\?"<>\|]+/g, "_");
    txt = txt.replace(/\s+/g, "_");
    txt = txt.replace(/_+/g, "_");
    txt = txt.replace(/^_+|_+$/g, "");
    return txt;
  }

  function normalizeNameKey(name) {
    if (!name) return "";
    return normalizeLooseName(name);
  }

  function cacheProjectItemByName(name, item) {
    if (!name || !item) return;
    var key = normalizeNameKey(name);
    if (!key) return;
    PROJECT_ITEM_CACHE[key] = item;
  }

  function cacheProjectItem(item) {
    if (!item || !item.name) return;
    var itemName = item.name.toString();
    cacheProjectItemByName(itemName, item);
    cacheProjectItemByName(stripKnownExtension(itemName), item);
  }

  function getCachedProjectItem(name) {
    var key = normalizeNameKey(name);
    if (!key) return null;
    var item = PROJECT_ITEM_CACHE[key];
    if (!item) return null;
    try {
      // Touch one property to validate stale object refs.
      var _ = item.name;
      return item;
    } catch (e) {
      delete PROJECT_ITEM_CACHE[key];
      return null;
    }
  }

  function warmProjectItemCache() {
    if (PROJECT_ITEM_CACHE_WARMED || !app.project || !app.project.rootItem)
      return;
    var walk = function (bin) {
      if (!bin || !bin.children) return;
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (!item) continue;
        if (item.type !== ProjectItemType.BIN) {
          cacheProjectItem(item);
        }
        if (item.type === ProjectItemType.BIN) {
          walk(item);
        }
      }
    };
    walk(app.project.rootItem);
    PROJECT_ITEM_CACHE_WARMED = true;
  }

  function isItemNameMatch(itemName, nameRef) {
    if (!nameRef) return true;
    var itemNameNorm = itemName ? itemName.toString() : "";
    var nameRefNorm = nameRef ? nameRef.toString() : "";
    if (itemNameNorm.replace(/\s/g, "") === "") return false;

    if (itemNameNorm.indexOf(nameRefNorm) !== -1) return true;
    if (nameRefNorm.indexOf(itemNameNorm) !== -1) return true;

    var itemLoose = normalizeLooseName(itemNameNorm);
    var refLoose = normalizeLooseName(nameRefNorm);
    if (!itemLoose || !refLoose) return false;
    if (itemLoose === refLoose) return true;
    if (itemLoose.indexOf(refLoose) !== -1) return true;
    if (refLoose.indexOf(itemLoose) !== -1) return true;
    return false;
  }

  function isTrackItemMatch(item, targetTicks, toleranceTicks, nameRef) {
    if (!item) return false;
    var itemTicks = getStartTicks(item);
    if (itemTicks === null) return false;
    if (Math.abs(itemTicks - targetTicks) > toleranceTicks) return false;
    if (!nameRef) return true;
    var itemName = item.name ? item.name.toString() : "";
    return isItemNameMatch(itemName, nameRef);
  }

  function findTrackItemAtStart(track, startSeconds, nameRef) {
    if (!track || !track.clips) return null;

    var targetTicks = secondsToTicks(startSeconds);
    // Relaxed tolerance
    var toleranceTicks = secondsToTicks(0.1);

    var bestItem = null;
    var minDiff = toleranceTicks + 1;

    for (var i = 0; i < track.clips.numItems; i++) {
      var item = track.clips[i];
      if (!item) continue;
      var itemTicks = getStartTicks(item);
      if (itemTicks === null) continue;
      var diff = Math.abs(itemTicks - targetTicks);
      if (diff <= toleranceTicks) {
        if (nameRef) {
          var itemName = item.name ? item.name.toString() : "";
          if (!isItemNameMatch(itemName, nameRef)) continue;
        }
        if (diff < minDiff) {
          minDiff = diff;
          bestItem = item;
        }
      }
    }
    return bestItem;
  }

  function findRecentTrackItemAtStart(track, startSeconds, nameRef) {
    if (!track || !track.clips || track.clips.numItems <= 0) return null;
    var targetTicks = secondsToTicks(startSeconds);
    var toleranceTicks = secondsToTicks(0.2);
    var tailWindow = 8;
    var startIdx = track.clips.numItems - 1;
    var stopIdx = Math.max(0, startIdx - tailWindow + 1);

    for (var i = startIdx; i >= stopIdx; i--) {
      var item = track.clips[i];
      if (isTrackItemMatch(item, targetTicks, toleranceTicks, nameRef)) {
        return item;
      }
    }
    return null;
  }

  function waitForTrackItemAtStart(track, startSeconds, nameRef, maxWaitMs) {
    if (!track) return null;
    var timeout =
      typeof maxWaitMs === "number" ? maxWaitMs : TRACK_ITEM_WAIT_MAX_MS;
    var waited = 0;
    var item = findRecentTrackItemAtStart(track, startSeconds, nameRef);
    if (item) return item;
    item = findTrackItemAtStart(track, startSeconds, nameRef);
    if (item) return item;

    while (waited < timeout) {
      sleep(TRACK_ITEM_WAIT_STEP_MS);
      waited += TRACK_ITEM_WAIT_STEP_MS;
      item = findRecentTrackItemAtStart(track, startSeconds, nameRef);
      if (item) return item;
    }
    return findTrackItemAtStart(track, startSeconds, nameRef);
  }

  function setTrackItemInOutFromItem(
    item,
    inSeconds,
    outSeconds,
    inFrame,
    outFrame,
  ) {
    if (!item) return null;
    try {
      if (typeof inFrame === "number" && typeof outFrame === "number") {
        // In-point is nudged to frame center to avoid boundary rounding to previous frame.
        item.inPoint = buildRawTimeFromSourceFrame(inFrame, true);
        // Out-point stays on frame boundary (exclusive end frame).
        item.outPoint = buildRawTimeFromSourceFrame(outFrame, false);
      } else {
        item.inPoint = buildRawTimeFromSeconds(inSeconds);
        item.outPoint = buildRawTimeFromSeconds(outSeconds);
      }
    } catch (e) {
      return null;
    }
    return item;
  }

  function enforceTrackItemDuration(item, durationSeconds) {
    if (!item || typeof durationSeconds !== "number") return;
    try {
      var startSec = null;
      if (item.start && typeof item.start.seconds === "number") {
        startSec = item.start.seconds;
      } else {
        var startTicks = getStartTicks(item);
        if (typeof startTicks === "number") {
          startSec = startTicks / TICKS_PER_SECOND;
        }
      }
      if (typeof startSec === "number") {
        item.end = buildSequenceTimeFromSeconds(startSec + durationSeconds);
      }
    } catch (e) {}
  }

  function timeObjectToSeconds(timeObj) {
    if (!timeObj) return null;
    try {
      if (timeObj.ticks !== undefined) {
        var ticksVal = parseInt(timeObj.ticks, 10);
        if (!isNaN(ticksVal)) return ticksVal / TICKS_PER_SECOND;
      }
    } catch (e0) {}
    try {
      if (typeof timeObj.seconds === "number") return timeObj.seconds;
      if (typeof timeObj.secs === "number") return timeObj.secs;
    } catch (e1) {}
    return null;
  }

  function getTrackItemStartSeconds(item) {
    if (!item) return null;
    var startSec = timeObjectToSeconds(item.start);
    if (typeof startSec === "number") return startSec;
    var startTicks = getStartTicks(item);
    if (typeof startTicks === "number") return startTicks / TICKS_PER_SECOND;
    return null;
  }

  function getTrackItemEndSeconds(item) {
    if (!item) return null;
    var endSec = timeObjectToSeconds(item.end);
    if (typeof endSec === "number") return endSec;

    var startSec = getTrackItemStartSeconds(item);
    if (typeof startSec !== "number") return null;

    var durSec = null;
    try {
      if (item.duration && typeof item.duration.seconds === "number") {
        durSec = item.duration.seconds;
      } else if (item.duration && item.duration.ticks !== undefined) {
        var durTicks = parseInt(item.duration.ticks, 10);
        if (!isNaN(durTicks)) durSec = durTicks / TICKS_PER_SECOND;
      }
    } catch (e0) {}
    if (typeof durSec === "number") return startSec + durSec;
    return null;
  }

  function setTrackItemEndSeconds(item, endSec) {
    if (!item || typeof endSec !== "number") return false;
    try {
      item.end = endSec;
      return true;
    } catch (e0) {}
    try {
      item.end = buildSequenceTimeFromSeconds(endSec);
      return true;
    } catch (e1) {}
    return false;
  }

  function getMotionComponent(item) {
    if (!item || !item.components) return null;
    var fallback =
      item.components.numItems > 1 ? item.components[1] : item.components[0];
    for (var c = 0; c < item.components.numItems; c++) {
      var comp = item.components[c];
      if (!comp || !comp.displayName) continue;
      if (comp.displayName === "Motion" || comp.displayName === "Trajectoire") {
        return comp;
      }
    }
    return fallback || null;
  }

  function setScaleOnItem(item, scaleVal) {
    if (!item) return false;
    var motion = getMotionComponent(item);
    if (!motion || !motion.properties) return false;
    for (var p = 0; p < motion.properties.numItems; p++) {
      var prop = motion.properties[p];
      if (!prop || !prop.displayName) continue;
      if (
        prop.displayName === "Scale" ||
        prop.displayName === "Echelle" ||
        prop.displayName === "\u00c9chelle"
      ) {
        try {
          prop.setValue(scaleVal, true);
          return true;
        } catch (e) {
          return false;
        }
      }
    }
    return false;
  }

  function logClipDuration(item, targetSeconds, label) {
    if (!item || !label) return;
    try {
      var dur = null;
      if (item.duration && typeof item.duration.seconds === "number") {
        dur = item.duration.seconds;
      } else if (item.duration && typeof item.duration.ticks === "number") {
        dur = item.duration.ticks / TICKS_PER_SECOND;
      }
      if (typeof dur === "number") {
        log(
          label +
            " duration " +
            dur.toFixed(4) +
            "s (target " +
            targetSeconds.toFixed(4) +
            "s)",
        );
      }
    } catch (e) {}
  }

  function findProjectItem(name) {
    var cached = getCachedProjectItem(name);
    if (cached) return cached;
    var findInBin = function (bin) {
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (item.name === name && item.type !== ProjectItemType.BIN) {
          cacheProjectItem(item);
          return item;
        }
        if (item.type === ProjectItemType.BIN) {
          var found = findInBin(item);
          if (found) return found;
        }
      }
      return null;
    };
    return findInBin(app.project.rootItem);
  }

  function findProjectItemLoose(name) {
    var target = normalizeLooseName(name);
    if (!target) return null;
    var findInBin = function (bin) {
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (!item) continue;
        if (item.type !== ProjectItemType.BIN) {
          var itemName = item.name ? item.name.toString() : "";
          if (normalizeLooseName(itemName) === target) {
            cacheProjectItem(item);
            return item;
          }
        }
        if (item.type === ProjectItemType.BIN) {
          var found = findInBin(item);
          if (found) return found;
        }
      }
      return null;
    };
    return findInBin(app.project.rootItem);
  }

  function getOrImportClip(clipName) {
    var cleanName = clipName.replace(/^\s+|\s+$/g, "");
    var nameNoExt = stripKnownExtension(cleanName);

    var item = getCachedProjectItem(cleanName);
    if (item) return item;
    item = getCachedProjectItem(nameNoExt);
    if (item) return item;

    item = findProjectItem(cleanName);
    if (item) return item;
    item = findProjectItem(nameNoExt);
    if (item) return item;
    item = findProjectItemLoose(cleanName);
    if (item) return item;
    item = findProjectItemLoose(nameNoExt);
    if (item) return item;

    var searchPaths = [
      ROOT_DIR + "/" + cleanName,
      ROOT_DIR + "/" + nameNoExt,
      ROOT_DIR + "/" + cleanName + ".wav",
      SOURCES_DIR + "/" + cleanName,
      SOURCES_DIR + "/" + nameNoExt,
      SOURCES_DIR + "/" + nameNoExt + ".mkv",
      SOURCES_DIR + "/" + nameNoExt + ".mp4",
      SOURCES_DIR + "/" + nameNoExt + ".mov",
      SOURCES_DIR + "/" + nameNoExt + ".avi",
      SOURCES_DIR + "/" + nameNoExt + ".webm",
      SOURCES_DIR + "/" + nameNoExt + ".m4v",
      SOURCES_DIR + "/" + nameNoExt + ".wav",
      SOURCES_DIR + "/" + nameNoExt + ".mp3",
    ];

    for (var i = 0; i < searchPaths.length; i++) {
      var f = new File(searchPaths[i]);
      if (f.exists) {
        app.project.importFiles([f.fsName], true, app.project.rootItem, false);
        // Small bounded retry; import indexing can be async.
        var retries = 0;
        while (!item && retries < 8) {
          item = findProjectItem(f.name);
          if (!item) item = findProjectItem(f.displayName);
          if (!item) item = findProjectItem(nameNoExt);
          if (!item) item = findProjectItem(stripKnownExtension(f.name));
          if (!item) item = findProjectItemLoose(f.name);
          if (!item) item = findProjectItemLoose(f.displayName);
          if (!item) item = findProjectItemLoose(cleanName);
          if (!item) item = findProjectItemLoose(nameNoExt);
          if (!item) {
            sleep(40);
            retries++;
          }
        }
        if (item) {
          cacheProjectItem(item);
          cacheProjectItemByName(cleanName, item);
          cacheProjectItemByName(nameNoExt, item);
        }
        return item;
      }
    }
    log("Error: Clip not found: " + cleanName);
    return null;
  }

  // ========================================================================
  // 3. MAIN LOGIC
  // ========================================================================
  function main() {
    app.enableQE();
    if (!app.project) {
      alert("Open a project.");
      return;
    }
    log("Purging project to start fresh...");
    if (!purgeProjectCompletely()) {
      alert("Error: Could not fully purge the project. Aborting.");
      return;
    }

    var seqName = "ATR_Layered_" + Math.floor(Math.random() * 9999);
    var presetFile = new File(SEQUENCE_PRESET_PATH);
    var sequence;

    if (presetFile.exists) {
      qe.project.newSequence(seqName, presetFile.fsName);
      sequence = app.project.activeSequence;
    } else {
      sequence = app.project.createNewSequence(seqName, "ID_1");
    }

    // --- ENSURE TRACKS (V=6, A=3) ---
    ensureVideoTracks(sequence, 6);
    ensureAudioTracks(sequence, 3);

    // Mapping Tracks
    // V1: Index 0 (Back)
    // V2: Index 1 (Border)
    // V3: Index 2 (Main)
    // V4: Index 3 (Subs)
    // V5: Index 4 (Reserved)
    // V6: Index 5 (Reserved)
    // A1: Index 0 (Source audio muted)
    // A2: Index 1 (TTS)
    // A3: Index 2 (Reserved)

    var v1 = sequence.videoTracks[0];
    var v2 =
      sequence.videoTracks.numTracks > 1 ? sequence.videoTracks[1] : null;
    var v3 =
      sequence.videoTracks.numTracks > 2 ? sequence.videoTracks[2] : null;
    var v4 =
      sequence.videoTracks.numTracks > 3 ? sequence.videoTracks[3] : null;
    var v5 =
      sequence.videoTracks.numTracks > 4 ? sequence.videoTracks[4] : null;
    var v6 =
      sequence.videoTracks.numTracks > 5 ? sequence.videoTracks[5] : null;
    var a1 = sequence.audioTracks[0];
    var a2 = sequence.audioTracks.numTracks > 1 ? sequence.audioTracks[1] : a1;
    var a3 =
      sequence.audioTracks.numTracks > 2 ? sequence.audioTracks[2] : null;

    // --- MUTE A1 (Clip Audio) ---
    try {
      a1.setMute(1);
    } catch (e) {}

    // Warm cache once, then preload only the source clips we will use.
    warmProjectItemCache();
    var preloadNames = {};
    for (var i = 0; i < scenes.length; i++) {
      preloadNames[scenes[i].clipName] = true;
    }
    preloadNames[AUDIO_FILENAME] = true;
    preloadNames[CATEGORY_OVERLAY_FILENAME] = true;
    preloadNames[TITLE_OVERLAY_FILENAME] = true;
    if (trimSpaces(MUSIC_FILENAME) !== "") {
      preloadNames[MUSIC_FILENAME] = true;
    }
    for (var preloadName in preloadNames) {
      if (preloadNames.hasOwnProperty(preloadName)) {
        getOrImportClip(preloadName);
      }
    }

    // --- MARKERS ---
    log("Creating Markers...");
    for (var i = 0; i < scenes.length; i++) {
      var mStart = snapSecondsToFrame(scenes[i].start);
      var mEnd = snapSecondsToFrame(scenes[i].end);
      var m = sequence.markers.createMarker(mStart);
      m.name = "Scene " + scenes[i].scene_index;
      m.duration = mEnd - mStart;
    }

    // --- INTERLEAVED PROCESSING (V1 & V3) ---
    // V1 (Background) & V3 (Main)
    log("Processing Scenes (Layering & Speed)...");
    var nameCleaner = function (n) {
      return stripKnownExtension(n);
    }; // Helper

    for (var i = 0; i < scenes.length; i++) {
      var s = scenes[i];
      var startSec = snapSecondsToFrame(s.start);
      var clip = getOrImportClip(s.clipName);
      var cleanName = nameCleaner(s.clipName);

      if (clip) {
        // 1. PLACE ON V3 (Main)
        if (v3) v3.overwriteClip(clip, startSec);

        // 2. PLACE ON V1 (Background)
        if (v1) v1.overwriteClip(clip, startSec);

        // 2b. SET PER-INSTANCE IN/OUT (TrackItem) TO AVOID UNIT AMBIGUITY
        var v3Item = null;
        var v1Item = null;
        var a1Item = null;
        var a2Item = null;
        if (v3) {
          v3Item = waitForTrackItemAtStart(
            v3,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          v3Item = setTrackItemInOutFromItem(
            v3Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        if (v1) {
          v1Item = waitForTrackItemAtStart(
            v1,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          v1Item = setTrackItemInOutFromItem(
            v1Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        if (a1) {
          a1Item = waitForTrackItemAtStart(
            a1,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          a1Item = setTrackItemInOutFromItem(
            a1Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }
        if (a2 && a2 !== a1) {
          a2Item = waitForTrackItemAtStart(
            a2,
            startSec,
            cleanName,
            TRACK_ITEM_WAIT_MAX_MS,
          );
          a2Item = setTrackItemInOutFromItem(
            a2Item,
            s.source_in,
            s.source_out,
            s.source_in_frame,
            s.source_out_frame,
          );
        }

        // 3. ENFORCE DURATION (ALL SPEEDS)
        // Always enforce the target timeline duration, even at 1.0x.
        // If in/out fails or speed is exactly 1.0, this prevents huge clip lengths.
        var newDurationSeconds = snapSecondsToFrame(
          s.clip_duration / s.effective_speed,
        );
        enforceTrackItemDuration(v3Item, newDurationSeconds);
        enforceTrackItemDuration(v1Item, newDurationSeconds);
        enforceTrackItemDuration(a1Item, newDurationSeconds);
        enforceTrackItemDuration(a2Item, newDurationSeconds);

        // 4. APPLY SPEED (Both V1, V3, A1, A2)
        // QE setSpeed often fails to ripple-edit duration for speedups, so we pre-resize above.
        if (Math.abs(s.effective_speed - 1.0) > 0.01) {
          if (v3)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              2,
              "Video",
              cleanName,
              sequence,
            );
          if (v1)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              0,
              "Video",
              cleanName,
              sequence,
            );
          if (a1 && a1Item)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              0,
              "Audio",
              cleanName,
              sequence,
            );
          if (a2 && a2Item && a2 !== a1)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              1,
              "Audio",
              cleanName,
              sequence,
            );
        }

        // 4. APPLY SCALE (Standard API)
        if (!setScaleOnItem(v3Item, 75) && v3)
          setScaleAndPosition(v3, startSec, 75); // Main Scaled Down
        if (!setScaleOnItem(v1Item, 183) && v1)
          setScaleAndPosition(v1, startSec, 183); // Background Scaled Up

        if (v3Item) {
          logClipDuration(v3Item, s.target_duration, "Scene " + s.scene_index);
        } else if (v3) {
          var v3ItemForLog = findTrackItemAtStart(v3, startSec, cleanName);
          if (v3ItemForLog) {
            logClipDuration(
              v3ItemForLog,
              s.target_duration,
              "Scene " + s.scene_index,
            );
          }
        }
      }
    }

    // --- V2: BORDER MOGRT ---
    if (v2 && new File(BORDER_MOGRT_PATH).exists) {
      log("Adding Border Mogrt to V2...");
      try {
        // Insert once at 0
        var totalDuration =
          scenes.length > 0
            ? snapSecondsToFrame(scenes[scenes.length - 1].end)
            : 0;
        if (totalDuration > 0) {
          var mgt = sequence.importMGT(BORDER_MOGRT_PATH, 0, 1, 0); // Index 1 starts V2 ?? Wait, numTracks test used sequence.videoTracks[1]?
          // No, importMGT(path, time, videoTrackIndex, audioTrackIndex)
          // The script previously used index 1.
          if (mgt) {
            mgt.end = totalDuration;
            log("Border Mogrt inserted. Duration: " + totalDuration);
          }
        }
      } catch (e) {
        log("Border Mogrt Error: " + e.message);
      }
    }

    // --- IMPORT TTS (A2), THEN BUILD OVERLAYS + MUSIC FROM TTS DURATION ---
    log("Importing TTS to A2...");
    var ttsItem = getOrImportClip(AUDIO_FILENAME);
    if (!a2 || !ttsItem) {
      alert("Error: Missing TTS track or file '" + AUDIO_FILENAME + "'.");
      return;
    }
    a2.overwriteClip(ttsItem, 0);

    // Cleanup all audio tracks except A1 and A2 (TTS)
    cleanupAudioTracks(1, AUDIO_FILENAME);

    var ttsNameNoExt = stripKnownExtension(AUDIO_FILENAME);
    var ttsTrackItem =
      waitForTrackItemAtStart(a2, 0, ttsNameNoExt, TRACK_ITEM_WAIT_MAX_MS) ||
      waitForTrackItemAtStart(a2, 0, AUDIO_FILENAME, TRACK_ITEM_WAIT_MAX_MS) ||
      findTrackItemAtStart(a2, 0, null);
    var ttsEndSec = ttsTrackItem ? getTrackItemEndSeconds(ttsTrackItem) : null;
    if (typeof ttsEndSec !== "number" || !(ttsEndSec > 0)) {
      alert(
        "Error: Unable to resolve TTS end time from '" + AUDIO_FILENAME + "'.",
      );
      return;
    }
    ttsEndSec = snapSecondsToFrame(ttsEndSec);

    log("Adding overlays on V5 and V6...");
    if (!placeOverlayOnTrack(v5, CATEGORY_OVERLAY_FILENAME, ttsEndSec)) {
      log("Warning: Failed to place " + CATEGORY_OVERLAY_FILENAME + " on V5.");
    }
    if (!placeOverlayOnTrack(v6, TITLE_OVERLAY_FILENAME, ttsEndSec)) {
      log("Warning: Failed to place " + TITLE_OVERLAY_FILENAME + " on V6.");
    }

    var musicFilenameTrimmed = trimSpaces(MUSIC_FILENAME);
    if (musicFilenameTrimmed !== "") {
      log("Adding looped music bed on A3...");
      if (!a3) {
        log("Warning: A3 track is unavailable, skipping music bed.");
      } else {
        var musicItem = getOrImportClip(musicFilenameTrimmed);
        if (!musicItem) {
          log(
            "Warning: Music file '" +
              musicFilenameTrimmed +
              "' not found. Skipping music bed.",
          );
        } else if (
          !buildLoopedMusicBed(a3, musicItem, ttsEndSec, MUSIC_GAIN_DB)
        ) {
          log("Warning: Could not fully build looped music bed.");
        }
      }
    } else {
      log("Skipping music bed (MUSIC_FILENAME is empty).");
    }

    // --- APPLY VIDEO PRESETS ---
    log("Applying Background preset on V1...");
    applyVideoPresetToTrackItems(
      0,
      BACKGROUND_PRESET_NAME,
      BACKGROUND_PRESET_FILE_PATH,
    );
    log("Applying Foreground preset on V3...");
    applyVideoPresetToTrackItems(
      2,
      FOREGROUND_PRESET_NAME,
      FOREGROUND_PRESET_FILE_PATH,
    );

    // --- V4: SUBTITLES (SRT timings + external MOGRT files) ---
    log("Loading subtitle MOGRT files to V4...");
    importSubtitleMogrtsFromFolder(
      sequence,
      3,
      SUBTITLE_MOGRT_DIR,
      SUBTITLE_SRT_PATH,
    );

    alert(
      "Script Complete (v7.7 Layered - Presets + External Subtitle MOGRTs).",
    );
  }

  // ========================================================================
  // 4. HELPERS
  // ========================================================================

  function trimSpaces(value) {
    if (value === null || value === undefined) return "";
    return value.toString().replace(/^\s+|\s+$/g, "");
  }

  function parseSrtTimestampToSeconds(rawTimecode) {
    var tc = trimSpaces(rawTimecode);
    if (!tc) return null;
    var m = /^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})$/.exec(tc);
    if (!m) return null;
    var h = parseInt(m[1], 10);
    var mn = parseInt(m[2], 10);
    var s = parseInt(m[3], 10);
    var msTxt = (m[4] + "00").substr(0, 3);
    var ms = parseInt(msTxt, 10);
    if (isNaN(h) || isNaN(mn) || isNaN(s) || isNaN(ms)) return null;
    return h * 3600 + mn * 60 + s + ms / 1000;
  }

  function parseSrtEntries(filePath) {
    var entries = [];
    if (!filePath) return entries;
    var f = new File(filePath);
    if (!f.exists || !f.open("r")) return entries;

    var content = "";
    try {
      content = f.read();
    } catch (e0) {
      content = "";
    }
    f.close();
    if (!content) return entries;

    content = content
      .replace(/^\uFEFF/, "")
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");

    var blocks = content.split(/\n{2,}/);
    for (var bi = 0; bi < blocks.length; bi++) {
      var block = blocks[bi];
      if (!block) continue;
      var lines = block.split("\n");
      if (!lines || lines.length < 2) continue;

      while (lines.length > 0 && trimSpaces(lines[0]) === "") lines.shift();
      while (lines.length > 0 && trimSpaces(lines[lines.length - 1]) === "")
        lines.pop();
      if (lines.length < 2) continue;

      var cursor = 0;
      if (/^\d+$/.test(trimSpaces(lines[0]))) cursor = 1;
      if (cursor >= lines.length) continue;

      var timingLine = lines[cursor];
      if (timingLine.indexOf("-->") === -1) continue;
      var timingParts = timingLine.split(/-->/);
      if (!timingParts || timingParts.length < 2) continue;

      var startSec = parseSrtTimestampToSeconds(timingParts[0]);
      var endSec = parseSrtTimestampToSeconds(timingParts[1]);
      if (startSec === null || endSec === null || endSec <= startSec) continue;

      var hasText = false;
      for (var li = cursor + 1; li < lines.length; li++) {
        var txtLine = lines[li];
        if (txtLine === null || txtLine === undefined) continue;
        if (trimSpaces(txtLine) === "") continue;
        hasText = true;
        break;
      }
      if (!hasText) continue;

      entries.push({
        index: entries.length + 1,
        start: startSec,
        end: endSec,
      });
    }
    return entries;
  }

  function parseSubtitleMogrtIndex(fileName) {
    if (!fileName) return null;
    var m = /^subtitle_(\d+)\.mogrt$/i.exec(trimSpaces(fileName));
    if (!m || m.length < 2) return null;
    var n = parseInt(m[1], 10);
    return isNaN(n) ? null : n;
  }

  function listSubtitleMogrtFilesSorted(folderPath) {
    var result = [];
    if (!folderPath) return result;
    var folder = new Folder(folderPath);
    if (!folder.exists) return result;

    var entries = folder.getFiles();
    var sortable = [];
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      if (!(entry instanceof File)) continue;
      var idx = parseSubtitleMogrtIndex(entry.name);
      if (idx === null) continue;
      sortable.push({
        file: entry,
        index: idx,
        name: entry.name ? entry.name.toLowerCase() : "",
      });
    }

    sortable.sort(function (a, b) {
      if (a.index !== b.index) return a.index - b.index;
      if (a.name < b.name) return -1;
      if (a.name > b.name) return 1;
      return 0;
    });

    for (var j = 0; j < sortable.length; j++) {
      result.push(sortable[j].file);
    }
    return result;
  }

  function importSubtitleMogrtsFromFolder(
    sequence,
    videoTrackIndex,
    subtitleDirPath,
    srtPath,
  ) {
    var stats = {
      timings: 0,
      mogrtsFound: 0,
      inserted: 0,
      insertFailed: 0,
      timingsUnused: 0,
      mogrtsUnused: 0,
    };

    if (
      !sequence ||
      !sequence.videoTracks ||
      sequence.videoTracks.numTracks <= videoTrackIndex
    ) {
      log(
        "Warning: Cannot add subtitles (missing sequence track V" +
          (videoTrackIndex + 1) +
          ").",
      );
      return stats;
    }

    var entries = parseSrtEntries(srtPath);
    stats.timings = entries.length;
    if (entries.length <= 0) {
      log("Warning: No subtitle entries parsed from " + srtPath);
      return stats;
    }

    var subtitleDir = new Folder(subtitleDirPath);
    if (!subtitleDir.exists) {
      log("Warning: Subtitle MOGRT folder not found: " + subtitleDirPath);
      return stats;
    }

    var mogrtFiles = listSubtitleMogrtFilesSorted(subtitleDirPath);
    stats.mogrtsFound = mogrtFiles.length;
    if (stats.mogrtsFound <= 0) {
      log("Warning: No subtitle MOGRT files found in " + subtitleDirPath + ".");
      return stats;
    }

    var pairCount = Math.min(stats.timings, stats.mogrtsFound);
    stats.timingsUnused = stats.timings - pairCount;
    stats.mogrtsUnused = stats.mogrtsFound - pairCount;
    if (stats.timingsUnused > 0 || stats.mogrtsUnused > 0) {
      log(
        "Warning: Subtitle timing/MOGRT count mismatch (timings: " +
          stats.timings +
          ", mogrts: " +
          stats.mogrtsFound +
          "). Inserting " +
          pairCount +
          ".",
      );
    }

    for (var k = 0; k < pairCount; k++) {
      var entry = entries[k];
      var mogrtFile = mogrtFiles[k];
      if (!mogrtFile || !mogrtFile.exists) {
        stats.insertFailed++;
        continue;
      }

      var startSec = snapSecondsToFrame(entry.start);
      var endSec = snapSecondsToFrame(entry.end);
      if (endSec <= startSec) {
        endSec = snapSecondsToFrame(startSec + 1 / SEQ_FPS);
      }

      var mogrtItem = null;
      try {
        mogrtItem = sequence.importMGT(
          mogrtFile.fsName,
          secondsToTicks(startSec).toString(),
          videoTrackIndex,
          0,
        );
      } catch (e0) {}
      if (!mogrtItem) {
        try {
          mogrtItem = sequence.importMGT(
            mogrtFile.fsName,
            startSec,
            videoTrackIndex,
            0,
          );
        } catch (e1) {}
      }
      if (!mogrtItem) {
        stats.insertFailed++;
        continue;
      }

      stats.inserted++;
      try {
        mogrtItem.end = endSec;
      } catch (e2) {
        try {
          mogrtItem.end = buildSequenceTimeFromSeconds(endSec);
        } catch (e3) {}
      }
    }

    log(
      "Subtitles (MOGRT load-only) on V" +
        (videoTrackIndex + 1) +
        ": timings " +
        stats.timings +
        ", mogrts found " +
        stats.mogrtsFound +
        ", inserted " +
        stats.inserted +
        ", insert failed " +
        stats.insertFailed +
        ", timings unused " +
        stats.timingsUnused +
        ", mogrts unused " +
        stats.mogrtsUnused +
        ".",
    );
    return stats;
  }

  function clearSelection(sequence, updateUI) {
    if (!sequence) return;
    var uiRefresh = !!updateUI;
    try {
      var tracks = sequence.videoTracks;
      for (var i = 0; i < tracks.numTracks; i++) {
        var track = tracks[i];
        for (var j = 0; j < track.clips.numItems; j++) {
          track.clips[j].setSelected(false, uiRefresh);
        }
      }
      // Clear Audio as well if needed? Usually audio tracks are less prone to this crash but good practice.
      // Skipping to save time/performance unless necessary.
    } catch (e) {}
  }

  function safeApplySpeedQE(
    startTime,
    speed,
    trackIndex,
    trackType,
    clipNameRef,
    sequence,
  ) {
    try {
      var qeSeq = qe.project.getActiveSequence();
      if (!qeSeq) return false;
      var qeTrack;
      if (trackType === "Audio") qeTrack = qeSeq.getAudioTrackAt(trackIndex);
      else qeTrack = qeSeq.getVideoTrackAt(trackIndex);
      if (!qeTrack) return false;

      var targetTicks = secondsToTicks(startTime);
      var toleranceTicks = secondsToTicks(0.2);
      var retriedAfterReset = false;

      // Search for the item with Name validation and Time tolerance
      // Iterate ALL items to find the best match or correct item
      for (var i = qeTrack.numItems - 1; i >= 0; i--) {
        try {
          var item = qeTrack.getItemAt(i);
          // Defensive: access properties safely
          if (!item || typeof item.start === "undefined") continue;

          // Time Check using Ticks (Robust)
          var startTicks = null;
          // 1. Try Ticks (String or Number)
          try {
            if (item.start.ticks !== undefined) {
              startTicks = parseInt(item.start.ticks, 10);
            }
          } catch (e0) {}

          // 2. Fallback to Seconds
          if (isNaN(startTicks) || startTicks === null) {
            try {
              if (typeof item.start.seconds === "number")
                startTicks = secondsToTicks(item.start.seconds);
              else if (typeof item.start.secs === "number")
                startTicks = secondsToTicks(item.start.secs);
            } catch (e1) {}
          }

          var matchTime = false;
          if (typeof startTicks === "number" && !isNaN(startTicks)) {
            // Use relaxed tolerance (0.2s)
            matchTime = Math.abs(startTicks - targetTicks) < toleranceTicks;
          } else {
            // Last resort fallback
            try {
              matchTime = Math.abs(item.start.secs - startTime) < 0.2;
            } catch (e3) {}
          }

          if (matchTime) {
            // Name Check (if ref provided)
            if (clipNameRef) {
              var itemName = item.name ? item.name.toString() : "";
              if (!isItemNameMatch(itemName, clipNameRef)) {
                continue;
              }
            }

            try {
              // args: speed, stretch, reverse, ripple, flicker
              item.setSpeed(speed, "", false, false, false);
              // log("Speed Applied: " + (speed*100).toFixed(1) + "% to " + item.name + " at " + startTime);
              return true;
            } catch (err) {
              var errMsg = err && err.message ? err.message.toString() : "";
              if (
                !retriedAfterReset &&
                errMsg.toLowerCase().indexOf("invalid trackitem") !== -1
              ) {
                retriedAfterReset = true;
                clearSelection(sequence || app.project.activeSequence, false);
                sleep(SPEED_RETRY_WAIT_MS);
                try {
                  item.setSpeed(speed, "", false, false, false);
                  return true;
                } catch (err2) {
                  log(
                    "Speed Apply Retry Error: " +
                      (err2 && err2.message ? err2.message : err2),
                  );
                }
              } else {
                log("Speed Apply Error: " + errMsg);
              }
            }
            return false;
          }
        } catch (e) {} // Ignore individual item access errors
      }
      log(
        "Warning: Could not find clip at " +
          startTime +
          " (" +
          clipNameRef +
          ") for Speed change.",
      );
      return false;
    } catch (e) {
      log("QE Speed Fail: " + e.message);
      return false;
    }
  }

  function resolveVideoEffectByName(effectName) {
    if (!effectName) return null;
    var name = effectName.toString().replace(/^\s+|\s+$/g, "");
    if (!name) return null;
    try {
      return qe.project.getVideoEffectByName(name);
    } catch (e) {}
    return null;
  }

  function pushUnique(arr, value) {
    if (!arr || !value) return;
    for (var i = 0; i < arr.length; i++) {
      if (arr[i] === value) return;
    }
    arr.push(value);
  }

  function decodeXmlEntities(text) {
    if (!text) return "";
    return text
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'");
  }

  function readPresetFileText(filePath) {
    if (!filePath) return "";
    if (PRESET_FILE_TEXT_CACHE[filePath] !== undefined) {
      return PRESET_FILE_TEXT_CACHE[filePath];
    }
    var f = new File(filePath);
    if (!f.exists || !f.open("r")) {
      PRESET_FILE_TEXT_CACHE[filePath] = "";
      return "";
    }
    var content = "";
    try {
      content = f.read();
    } catch (e) {
      content = "";
    }
    f.close();
    PRESET_FILE_TEXT_CACHE[filePath] = content || "";
    return PRESET_FILE_TEXT_CACHE[filePath];
  }

  function extractVideoFilterEntriesFromPresetFile(filePath) {
    var entries = [];
    if (!filePath) return entries;
    var content = readPresetFileText(filePath);
    if (!content) return entries;

    var re =
      /<VideoFilterComponent[\s\S]*?<DisplayName>([\s\S]*?)<\/DisplayName>[\s\S]*?<MatchName>([\s\S]*?)<\/MatchName>[\s\S]*?<\/VideoFilterComponent>/g;
    var m = null;
    while ((m = re.exec(content)) !== null) {
      var displayName = decodeXmlEntities(
        m[1] ? m[1].replace(/^\s+|\s+$/g, "") : "",
      );
      var matchName = decodeXmlEntities(
        m[2] ? m[2].replace(/^\s+|\s+$/g, "") : "",
      );
      if (!displayName && !matchName) continue;
      entries.push({ displayName: displayName, matchName: matchName });
    }
    return entries;
  }

  function parsePresetScalarValue(raw) {
    if (raw === null || raw === undefined) return null;
    var txt = raw.toString().replace(/^\s+|\s+$/g, "");
    if (txt === "") return null;
    if (/^(true|false)$/i.test(txt)) return txt.toLowerCase() === "true";
    var n = parseFloat(txt);
    if (!isNaN(n) && /^[-+0-9.]+$/.test(txt)) return n;
    return null;
  }

  function decodeBase64ToBytes(base64Text) {
    if (!base64Text) return [];
    var src = base64Text.toString().replace(/\s+/g, "");
    if (!src) return [];

    var alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    var bytes = [];
    var i = 0;
    while (i < src.length) {
      var c0 = src.charAt(i++);
      var c1 = src.charAt(i++);
      var c2 = i < src.length ? src.charAt(i++) : "=";
      var c3 = i < src.length ? src.charAt(i++) : "=";
      if (!c0 || !c1) break;

      var b0 = alphabet.indexOf(c0);
      var b1 = alphabet.indexOf(c1);
      var b2 = c2 === "=" ? -1 : alphabet.indexOf(c2);
      var b3 = c3 === "=" ? -1 : alphabet.indexOf(c3);
      if (b0 < 0 || b1 < 0) continue;
      if (b2 < 0 && c2 !== "=") continue;
      if (b3 < 0 && c3 !== "=") continue;

      var bits =
        (b0 << 18) | (b1 << 12) | ((b2 < 0 ? 0 : b2) << 6) | (b3 < 0 ? 0 : b3);
      bytes.push((bits >> 16) & 255);
      if (b2 >= 0) bytes.push((bits >> 8) & 255);
      if (b3 >= 0) bytes.push(bits & 255);
    }
    return bytes;
  }

  function decodeBase64Utf16LE(base64Text) {
    var bytes = decodeBase64ToBytes(base64Text);
    if (!bytes || bytes.length <= 0) return "";

    var chars = [];
    for (var i = 0; i + 1 < bytes.length; i += 2) {
      var code = bytes[i] | (bytes[i + 1] << 8);
      if (code === 0) break;
      chars.push(String.fromCharCode(code));
    }
    return chars.join("");
  }

  function extractLumetriArbStringsByIndex(filePath) {
    if (!filePath) return {};
    if (LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath]) {
      return LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath];
    }

    var result = {};
    var content = readPresetFileText(filePath);
    if (!content) {
      LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] = result;
      return result;
    }

    var lumetriBlockMatch =
      /<VideoFilterComponent[\s\S]*?<MatchName>\s*AE\.ADBE Lumetri\s*<\/MatchName>[\s\S]*?<\/VideoFilterComponent>/i.exec(
        content,
      );
    if (!lumetriBlockMatch || !lumetriBlockMatch[0]) {
      LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] = result;
      return result;
    }
    var lumetriBlock = lumetriBlockMatch[0];

    var paramRefByIndex = {};
    var reRef = /<Param Index="(\d+)" ObjectRef="(\d+)"\/>/g;
    var mRef = null;
    while ((mRef = reRef.exec(lumetriBlock)) !== null) {
      var idx = parseInt(mRef[1], 10);
      if (isNaN(idx)) continue;
      paramRefByIndex[idx] = mRef[2];
    }

    // Decode only Look-related indexes to avoid expensive parsing of large Arb payloads.
    var targetIndexes = [32, 33];
    for (var ti = 0; ti < targetIndexes.length; ti++) {
      var targetIdx = targetIndexes[ti];
      var objRef = paramRefByIndex[targetIdx];
      if (!objRef) continue;

      var reNode = new RegExp(
        '<ArbVideoComponentParam\\s+ObjectID="' +
          objRef +
          '"[\\s\\S]*?<\\/ArbVideoComponentParam>',
        "i",
      );
      var nodeMatch = reNode.exec(content);
      if (!nodeMatch || !nodeMatch[0]) continue;

      var valMatch =
        /<StartKeyframeValue[^>]*>([\s\S]*?)<\/StartKeyframeValue>/.exec(
          nodeMatch[0],
        );
      if (!valMatch || valMatch.length < 2) continue;
      var b64 = valMatch[1] ? valMatch[1].replace(/^\s+|\s+$/g, "") : "";
      if (!b64) continue;

      var decoded = decodeBase64Utf16LE(b64)
        .replace(/\u0000+$/g, "")
        .replace(/^\s+|\s+$/g, "");
      if (!decoded) continue;
      result[targetIdx] = decoded;
    }

    LUMETRI_PRESET_ARB_STRINGS_CACHE[filePath] = result;
    return result;
  }

  function isAbsoluteLutPath(value) {
    if (!value) return false;
    var v = value.toString().replace(/^\s+|\s+$/g, "");
    if (!v) return false;
    var hasAbsoluteRoot =
      /^[A-Za-z]:[\\\/]/.test(v) || /^\\\\/.test(v) || /^\//.test(v);
    var hasLutExt = /\.(itx|cube|look)(\/)?$/i.test(v);
    return hasAbsoluteRoot && hasLutExt;
  }

  function resolveLumetriLookPathForPreset(presetFilePath) {
    var cacheKey = presetFilePath || "__missing_preset__";
    if (LUMETRI_LOOK_PATH_CACHE[cacheKey] !== undefined) {
      return LUMETRI_LOOK_PATH_CACHE[cacheKey];
    }
    if (!presetFilePath) {
      LUMETRI_LOOK_PATH_CACHE[cacheKey] = "";
      return "";
    }

    var values = extractLumetriArbStringsByIndex(presetFilePath);
    var indexes = [32, 33];
    for (var i = 0; i < indexes.length; i++) {
      var idx = indexes[i];
      var candidate = values[idx];
      if (!candidate) continue;
      if (isAbsoluteLutPath(candidate)) {
        LUMETRI_LOOK_PATH_CACHE[cacheKey] = candidate;
        return candidate;
      }
    }

    LUMETRI_LOOK_PATH_CACHE[cacheKey] = "";
    return "";
  }

  function buildLookPathCandidates(pathValue) {
    var candidates = [];
    var basePath = pathValue
      ? pathValue.toString().replace(/^\s+|\s+$/g, "")
      : "";
    if (!basePath) return candidates;
    if (basePath.charAt(basePath.length - 1) !== "/") basePath += "/";
    pushUnique(candidates, basePath);
    return candidates;
  }

  function extractLumetriPresetValuesByIndex(filePath) {
    if (!filePath) return [];
    if (LUMETRI_PRESET_VALUES_CACHE[filePath]) {
      return LUMETRI_PRESET_VALUES_CACHE[filePath];
    }
    var content = readPresetFileText(filePath);
    if (!content) {
      LUMETRI_PRESET_VALUES_CACHE[filePath] = [];
      return [];
    }

    var lumetriBlockMatch =
      /<VideoFilterComponent[\s\S]*?<MatchName>\s*AE\.ADBE Lumetri\s*<\/MatchName>[\s\S]*?<\/VideoFilterComponent>/i.exec(
        content,
      );
    if (!lumetriBlockMatch || !lumetriBlockMatch[0]) {
      LUMETRI_PRESET_VALUES_CACHE[filePath] = [];
      return [];
    }
    var lumetriBlock = lumetriBlockMatch[0];

    var paramRefs = [];
    var reRef = /<Param Index="(\d+)" ObjectRef="(\d+)"\/>/g;
    var mRef = null;
    while ((mRef = reRef.exec(lumetriBlock)) !== null) {
      paramRefs.push({ index: parseInt(mRef[1], 10), objectRef: mRef[2] });
    }
    if (paramRefs.length <= 0) {
      LUMETRI_PRESET_VALUES_CACHE[filePath] = [];
      return [];
    }

    var valueByObjectRef = {};
    var reParamNode =
      /<VideoComponentParam\s+ObjectID="(\d+)"[\s\S]*?<\/VideoComponentParam>/g;
    var mNode = null;
    while ((mNode = reParamNode.exec(content)) !== null) {
      var objectId = mNode[1];
      var node = mNode[0];
      var currMatch = /<CurrentValue>([\s\S]*?)<\/CurrentValue>/.exec(node);
      if (!currMatch || currMatch.length < 2) continue;
      var value = parsePresetScalarValue(currMatch[1]);
      if (value === null) continue;

      var nameMatch = /<Name>([\s\S]*?)<\/Name>/.exec(node);
      var name =
        nameMatch && nameMatch[1]
          ? decodeXmlEntities(nameMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      var typeMatch =
        /<ParameterControlType>([\s\S]*?)<\/ParameterControlType>/.exec(node);
      var controlType =
        typeMatch && typeMatch[1] ? parseInt(typeMatch[1], 10) : null;

      valueByObjectRef[objectId] = {
        value: value,
        name: name,
        controlType: controlType,
      };
    }

    var result = [];
    for (var i = 0; i < paramRefs.length; i++) {
      var ref = paramRefs[i];
      var data = valueByObjectRef[ref.objectRef];
      if (!data) continue;
      result.push({
        index: ref.index,
        value: data.value,
        name: data.name,
        controlType: data.controlType,
      });
    }
    result.sort(function (a, b) {
      return a.index - b.index;
    });

    LUMETRI_PRESET_VALUES_CACHE[filePath] = result;
    return result;
  }

  function isMeaningfulLumetriValue(value) {
    if (typeof value === "boolean") return value === true;
    if (typeof value === "number") return Math.abs(value) > 0.000001;
    return false;
  }

  function getLumetriComponent(item) {
    if (!item || !item.components) return null;
    for (var c = 0; c < item.components.numItems; c++) {
      var comp = item.components[c];
      if (!comp || !comp.displayName) continue;
      var nm = comp.displayName.toString();
      if (
        nm === "Couleur Lumetri" ||
        nm === "Lumetri Color" ||
        nm.toLowerCase().indexOf("lumetri") !== -1
      ) {
        return comp;
      }
    }
    return null;
  }

  function applyLumetriPresetValuesToTrack(stdTrack, presetFilePath) {
    var stats = {
      settingsApplied: 0,
      clipsWithLumetri: 0,
      clipsUpdated: 0,
      propWrites: 0,
      propFails: 0,
    };
    if (!stdTrack || !stdTrack.clips) return stats;

    var allValues = extractLumetriPresetValuesByIndex(presetFilePath);
    if (!allValues || allValues.length <= 0) return stats;

    var values = [];
    for (var i = 0; i < allValues.length; i++) {
      if (isMeaningfulLumetriValue(allValues[i].value)) {
        values.push(allValues[i]);
      }
    }
    if (values.length <= 0) return stats;
    stats.settingsApplied = values.length;

    for (var ci = 0; ci < stdTrack.clips.numItems; ci++) {
      var clip = stdTrack.clips[ci];
      if (!clip) continue;
      var lumetri = getLumetriComponent(clip);
      if (!lumetri || !lumetri.properties) continue;
      stats.clipsWithLumetri++;

      var clipWrites = 0;
      for (var vi = 0; vi < values.length; vi++) {
        var setting = values[vi];
        if (setting.index < 0 || setting.index >= lumetri.properties.numItems)
          continue;
        var prop = lumetri.properties[setting.index];
        if (!prop) continue;
        try {
          prop.setValue(setting.value, true);
          stats.propWrites++;
          clipWrites++;
        } catch (e1) {
          stats.propFails++;
        }
      }
      if (clipWrites > 0) stats.clipsUpdated++;
    }
    return stats;
  }

  function applyLumetriLookPathToTrack(stdTrack, presetFilePath) {
    var stats = {
      resolvedPath: "",
      candidateCount: 0,
      pathUsed: "",
      clipsWithLumetri: 0,
      clipsUpdated: 0,
      propWrites: 0,
      propFails: 0,
    };
    if (!stdTrack || !stdTrack.clips) return stats;

    var resolvedPath = resolveLumetriLookPathForPreset(presetFilePath);
    stats.resolvedPath = resolvedPath;
    var candidates = buildLookPathCandidates(resolvedPath);
    stats.candidateCount = candidates.length;
    if (candidates.length <= 0) return stats;

    var targetIndexes = [32, 33];
    for (var ci = 0; ci < stdTrack.clips.numItems; ci++) {
      var clip = stdTrack.clips[ci];
      if (!clip) continue;
      var lumetri = getLumetriComponent(clip);
      if (!lumetri || !lumetri.properties) continue;
      stats.clipsWithLumetri++;

      var clipApplied = false;
      for (var pi = 0; pi < candidates.length && !clipApplied; pi++) {
        var pathCandidate = candidates[pi];
        for (var ti = 0; ti < targetIndexes.length; ti++) {
          var idx = targetIndexes[ti];
          if (idx < 0 || idx >= lumetri.properties.numItems) continue;
          var prop = lumetri.properties[idx];
          if (!prop) continue;
          try {
            prop.setValue(pathCandidate, true);
            stats.propWrites++;
            clipApplied = true;
            if (!stats.pathUsed) {
              stats.pathUsed = pathCandidate;
            } else if (stats.pathUsed !== pathCandidate) {
              stats.pathUsed = "mixed";
            }
            break;
          } catch (e0) {
            stats.propFails++;
          }
        }
      }

      if (clipApplied) stats.clipsUpdated++;
    }
    return stats;
  }

  function isLumetriMatchName(matchName) {
    if (!matchName) return false;
    var m = matchName.toString().toLowerCase();
    return m.indexOf("adbe lumetri") !== -1 || m.indexOf("lumetri") !== -1;
  }

  function extractPresetEffectValueEntries(filePath) {
    if (!filePath) return [];
    if (PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath]) {
      return PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath];
    }

    var content = readPresetFileText(filePath);
    if (!content) {
      PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath] = [];
      return [];
    }

    var valueByObjectRef = {};
    var reParamNode =
      /<VideoComponentParam\s+ObjectID="(\d+)"[\s\S]*?<\/VideoComponentParam>/g;
    var mNode = null;
    while ((mNode = reParamNode.exec(content)) !== null) {
      var objectId = mNode[1];
      var node = mNode[0];
      var currMatch = /<CurrentValue>([\s\S]*?)<\/CurrentValue>/.exec(node);
      if (!currMatch || currMatch.length < 2) continue;
      var value = parsePresetScalarValue(currMatch[1]);
      if (value === null) continue;

      var nameMatch = /<Name>([\s\S]*?)<\/Name>/.exec(node);
      var name =
        nameMatch && nameMatch[1]
          ? decodeXmlEntities(nameMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      var typeMatch =
        /<ParameterControlType>([\s\S]*?)<\/ParameterControlType>/.exec(node);
      var controlType =
        typeMatch && typeMatch[1] ? parseInt(typeMatch[1], 10) : null;
      valueByObjectRef[objectId] = {
        value: value,
        name: name,
        controlType: controlType,
      };
    }

    var entries = [];
    var reComponent = /<VideoFilterComponent[\s\S]*?<\/VideoFilterComponent>/g;
    var mComp = null;
    while ((mComp = reComponent.exec(content)) !== null) {
      var block = mComp[0];
      var displayMatch = /<DisplayName>([\s\S]*?)<\/DisplayName>/.exec(block);
      var matchMatch = /<MatchName>([\s\S]*?)<\/MatchName>/.exec(block);
      var displayName =
        displayMatch && displayMatch[1]
          ? decodeXmlEntities(displayMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      var matchName =
        matchMatch && matchMatch[1]
          ? decodeXmlEntities(matchMatch[1].replace(/^\s+|\s+$/g, ""))
          : "";
      if (!displayName && !matchName) continue;

      var values = [];
      var reRef = /<Param Index="(\d+)" ObjectRef="(\d+)"\/>/g;
      var mRef = null;
      while ((mRef = reRef.exec(block)) !== null) {
        var idx = parseInt(mRef[1], 10);
        var objRef = mRef[2];
        var data = valueByObjectRef[objRef];
        if (!data) continue;
        values.push({
          index: idx,
          value: data.value,
          name: data.name,
          controlType: data.controlType,
        });
      }
      values.sort(function (a, b) {
        return a.index - b.index;
      });

      entries.push({
        displayName: displayName,
        matchName: matchName,
        values: values,
      });
    }

    PRESET_EFFECT_VALUE_ENTRIES_CACHE[filePath] = entries;
    return entries;
  }

  function equalsIgnoreCase(a, b) {
    if (a === null || a === undefined || b === null || b === undefined)
      return false;
    return a.toString().toLowerCase() === b.toString().toLowerCase();
  }

  function findComponentByEffectEntry(clip, entry) {
    if (!clip || !clip.components || !entry) return null;
    var candidates = getFallbackEffectNameCandidates(
      entry.matchName,
      entry.displayName,
    );
    for (var i = 0; i < clip.components.numItems; i++) {
      var comp = clip.components[i];
      if (!comp || !comp.displayName) continue;
      var compName = comp.displayName.toString();
      for (var c = 0; c < candidates.length; c++) {
        if (candidates[c] && equalsIgnoreCase(compName, candidates[c])) {
          return comp;
        }
      }
    }
    return null;
  }

  function applyNonLumetriPresetValuesToTrack(stdTrack, presetFilePath) {
    var stats = {
      effectsWithValues: 0,
      clipsWithComponents: 0,
      clipsUpdated: 0,
      propWrites: 0,
      propFails: 0,
    };
    if (!stdTrack || !stdTrack.clips) return stats;

    var effectEntries = extractPresetEffectValueEntries(presetFilePath);
    if (!effectEntries || effectEntries.length <= 0) return stats;

    var entries = [];
    for (var i = 0; i < effectEntries.length; i++) {
      var e = effectEntries[i];
      if (!e || !e.values || e.values.length <= 0) continue;
      if (isLumetriMatchName(e.matchName)) continue;
      entries.push(e);
    }
    if (entries.length <= 0) return stats;
    stats.effectsWithValues = entries.length;

    for (var ci = 0; ci < stdTrack.clips.numItems; ci++) {
      var clip = stdTrack.clips[ci];
      if (!clip) continue;
      var clipWrites = 0;
      var hadComponent = false;

      for (var ei = 0; ei < entries.length; ei++) {
        var entry = entries[ei];
        var component = findComponentByEffectEntry(clip, entry);
        if (!component || !component.properties) continue;
        hadComponent = true;

        for (var vi = 0; vi < entry.values.length; vi++) {
          var setting = entry.values[vi];
          if (
            setting.index < 0 ||
            setting.index >= component.properties.numItems
          )
            continue;
          var prop = component.properties[setting.index];
          if (!prop) continue;
          try {
            prop.setValue(setting.value, true);
            stats.propWrites++;
            clipWrites++;
          } catch (e0) {
            stats.propFails++;
          }
        }
      }

      if (hadComponent) stats.clipsWithComponents++;
      if (clipWrites > 0) stats.clipsUpdated++;
    }
    return stats;
  }

  function getFallbackEffectNameCandidates(matchName, displayName) {
    var candidates = [];
    var m = matchName ? matchName.toString() : "";
    var d = displayName ? displayName.toString() : "";
    var lower = m.toLowerCase();

    // Prefer display names first; QE can resolve these better than internal match names.
    pushUnique(candidates, d);

    if (
      m === "AE.ADBE Horizontal Flip" ||
      lower.indexOf("horizontal flip") !== -1
    ) {
      pushUnique(candidates, "Miroir horizontal");
      pushUnique(candidates, "Horizontal Flip");
    }
    if (m === "AE.ADBE Lumetri" || lower.indexOf("lumetri") !== -1) {
      pushUnique(candidates, "Couleur Lumetri");
      pushUnique(candidates, "Lumetri Color");
      pushUnique(candidates, "Lumetri");
    }

    var stripped = m.replace(/^AE\.ADBE\s*/g, "");
    if (stripped && stripped !== m) {
      pushUnique(candidates, stripped);
    }

    // Keep raw match name as last resort.
    pushUnique(candidates, m);
    return candidates;
  }

  function getQEItemStartTicks(qeItem) {
    if (!qeItem || !qeItem.start) return null;
    var startTicks = null;
    try {
      if (qeItem.start.ticks !== undefined) {
        startTicks = parseInt(qeItem.start.ticks, 10);
      }
    } catch (e0) {}

    if (typeof startTicks === "number" && !isNaN(startTicks)) {
      return startTicks;
    }

    try {
      if (typeof qeItem.start.seconds === "number") {
        return secondsToTicks(qeItem.start.seconds);
      }
      if (typeof qeItem.start.secs === "number") {
        return secondsToTicks(qeItem.start.secs);
      }
    } catch (e1) {}

    return null;
  }

  function getTrackItemStartSeconds(item) {
    if (!item || !item.start) return null;
    try {
      if (typeof item.start.seconds === "number") return item.start.seconds;
    } catch (e0) {}

    var ticks = getStartTicks(item);
    if (typeof ticks === "number" && !isNaN(ticks)) {
      return ticks / TICKS_PER_SECOND;
    }
    return null;
  }

  function getTrackItemComponentsCount(item) {
    try {
      if (item && item.components) return item.components.numItems;
    } catch (e) {}
    return -1;
  }

  function findQETrackItemAtStartInTrack(qeTrack, startSeconds, nameRef) {
    if (!qeTrack) return null;
    var targetTicks = secondsToTicks(startSeconds);
    var toleranceTicks = secondsToTicks(0.2);

    for (var i = qeTrack.numItems - 1; i >= 0; i--) {
      var qeItem = null;
      try {
        qeItem = qeTrack.getItemAt(i);
      } catch (e0) {}
      if (!qeItem) continue;

      var qeStartTicks = getQEItemStartTicks(qeItem);
      if (qeStartTicks === null) continue;
      if (Math.abs(qeStartTicks - targetTicks) > toleranceTicks) continue;

      if (nameRef) {
        var qeName = qeItem.name ? qeItem.name.toString() : "";
        if (!isItemNameMatch(qeName, nameRef)) continue;
      }
      return qeItem;
    }
    return null;
  }

  function getQEVideoTrackAtSafe(qeSeq, idx) {
    if (!qeSeq || typeof idx !== "number" || idx < 0) return null;
    try {
      return qeSeq.getVideoTrackAt(idx);
    } catch (e) {}
    return null;
  }

  function scoreQETrackAgainstStandardTrack(stdTrack, qeTrack, maxSamples) {
    var score = { matches: 0, samples: 0 };
    if (!stdTrack || !stdTrack.clips || !qeTrack) return score;

    var total = stdTrack.clips.numItems;
    if (total <= 0) return score;

    var sampleLimit = Math.min(total, Math.max(1, maxSamples || 8));
    for (var i = 0; i < sampleLimit; i++) {
      var stdItem = stdTrack.clips[i];
      if (!stdItem) continue;
      var startSec = getTrackItemStartSeconds(stdItem);
      if (typeof startSec !== "number") continue;
      score.samples++;
      var nm = "";
      try {
        nm =
          stdItem.projectItem && stdItem.projectItem.name
            ? stdItem.projectItem.name.toString()
            : "";
      } catch (e0) {}
      nm = nm ? nm.replace(/\.[^\.]+$/, "") : "";
      if (findQETrackItemAtStartInTrack(qeTrack, startSec, nm)) {
        score.matches++;
      }
    }
    return score;
  }

  function resolveBestQEVideoTrackForStandardTrack(
    qeSeq,
    stdTrack,
    preferredIdx,
  ) {
    var bestTrack = getQEVideoTrackAtSafe(qeSeq, preferredIdx);
    var bestIdx = preferredIdx;
    var bestScore = scoreQETrackAgainstStandardTrack(stdTrack, bestTrack, 8);

    for (var i = 0; i < 16; i++) {
      var t = getQEVideoTrackAtSafe(qeSeq, i);
      if (!t) continue;
      var s = scoreQETrackAgainstStandardTrack(stdTrack, t, 8);
      if (s.matches > bestScore.matches) {
        bestTrack = t;
        bestIdx = i;
        bestScore = s;
      }
    }

    return { track: bestTrack, index: bestIdx, score: bestScore };
  }

  function applyQEEffectToTrackWithVerification(stdTrack, qeTrack, effectObj) {
    var stats = {
      totalClips: 0,
      matchedQEItems: 0,
      applyCalls: 0,
      verifiedChanges: 0,
      noChange: 0,
      failedCalls: 0,
    };
    if (!stdTrack || !stdTrack.clips || !qeTrack || !effectObj) return stats;

    for (var i = 0; i < stdTrack.clips.numItems; i++) {
      var stdItem = stdTrack.clips[i];
      if (!stdItem) continue;
      stats.totalClips++;

      var startSec = getTrackItemStartSeconds(stdItem);
      if (typeof startSec !== "number") continue;

      var nameRef = "";
      try {
        if (stdItem.projectItem && stdItem.projectItem.name) {
          nameRef = stdItem.projectItem.name.toString();
        } else if (stdItem.name) {
          nameRef = stdItem.name.toString();
        }
      } catch (e0) {}
      var cleanNameRef = nameRef ? stripKnownExtension(nameRef) : "";

      var qeItem = findQETrackItemAtStartInTrack(
        qeTrack,
        startSec,
        cleanNameRef,
      );
      if (!qeItem && nameRef)
        qeItem = findQETrackItemAtStartInTrack(qeTrack, startSec, nameRef);
      if (!qeItem)
        qeItem = findQETrackItemAtStartInTrack(qeTrack, startSec, null);
      if (!qeItem) continue;

      stats.matchedQEItems++;
      var before = getTrackItemComponentsCount(stdItem);

      try {
        qeItem.addVideoEffect(effectObj);
        stats.applyCalls++;
      } catch (e1) {
        stats.failedCalls++;
        continue;
      }

      sleep(10);
      var after = getTrackItemComponentsCount(stdItem);
      if (before >= 0 && after > before) stats.verifiedChanges++;
      else stats.noChange++;
    }
    return stats;
  }

  function applyVideoPresetToTrackItems(
    videoTrackIndex,
    presetName,
    presetFilePath,
  ) {
    var sequence = app.project ? app.project.activeSequence : null;
    if (
      !sequence ||
      !sequence.videoTracks ||
      sequence.videoTracks.numTracks <= videoTrackIndex
    ) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (missing sequence track V" +
          (videoTrackIndex + 1) +
          ").",
      );
      return false;
    }
    var stdTrack = sequence.videoTracks[videoTrackIndex];
    if (!stdTrack || !stdTrack.clips || stdTrack.clips.numItems <= 0) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (V" +
          (videoTrackIndex + 1) +
          " has no clips).",
      );
      return false;
    }

    app.enableQE();
    var qeSeq = null;
    try {
      qeSeq = qe.project.getActiveSequence();
    } catch (e0) {}
    if (!qeSeq) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (no active QE sequence).",
      );
      return false;
    }

    var qeTrackInfo = resolveBestQEVideoTrackForStandardTrack(
      qeSeq,
      stdTrack,
      videoTrackIndex,
    );
    if (!qeTrackInfo.track) {
      log(
        "Warning: Cannot apply preset '" +
          presetName +
          "' (no QE video track found).",
      );
      return false;
    }
    if (qeTrackInfo.index !== videoTrackIndex) {
      log(
        "Info: QE track remap for V" +
          (videoTrackIndex + 1) +
          " -> QE track " +
          qeTrackInfo.index +
          " (match " +
          qeTrackInfo.score.matches +
          "/" +
          qeTrackInfo.score.samples +
          ").",
      );
    }

    var totalVerified = 0;
    var anyApplied = false;
    var unresolvedEffects = 0;
    var filterEntries = extractVideoFilterEntriesFromPresetFile(presetFilePath);
    if (filterEntries.length <= 0) {
      var hint = "";
      try {
        var pf = new File(presetFilePath);
        hint = pf.exists
          ? " .prfpset exists but no filter entries were parsed."
          : " .prfpset file not found at: " + presetFilePath;
      } catch (e2) {}
      log(
        "Warning: Fallback unavailable for preset '" + presetName + "'." + hint,
      );
    } else {
      for (var eIdx = 0; eIdx < filterEntries.length; eIdx++) {
        var entry = filterEntries[eIdx];
        var label =
          entry.displayName || entry.matchName || "Effect " + (eIdx + 1);
        var candidates = getFallbackEffectNameCandidates(
          entry.matchName,
          entry.displayName,
        );
        var effectAppliedForEntry = false;

        for (var c = 0; c < candidates.length; c++) {
          var candidateName = candidates[c];
          if (!candidateName) continue;
          var effectCandidate = resolveVideoEffectByName(candidateName);
          if (!effectCandidate) continue;

          var st = applyQEEffectToTrackWithVerification(
            stdTrack,
            qeTrackInfo.track,
            effectCandidate,
          );
          if (st.applyCalls > 0) anyApplied = true;
          log(
            "Fallback candidate '" +
              candidateName +
              "' for '" +
              label +
              "': verified " +
              st.verifiedChanges +
              "/" +
              st.totalClips +
              " clip(s).",
          );

          if (st.verifiedChanges > 0) {
            totalVerified += st.verifiedChanges;
            effectAppliedForEntry = true;
            break;
          }
        }

        if (!effectAppliedForEntry) {
          unresolvedEffects++;
          log("Warning: Could not apply fallback for '" + label + "'.");
        }
      }
    }

    var nonLumetriSync = applyNonLumetriPresetValuesToTrack(
      stdTrack,
      presetFilePath,
    );
    var nonLumetriWrites = nonLumetriSync.propWrites;
    if (
      nonLumetriSync.effectsWithValues > 0 &&
      nonLumetriSync.clipsWithComponents > 0
    ) {
      log(
        "Effect values synced from .prfpset (non-Lumetri): " +
          nonLumetriSync.clipsUpdated +
          "/" +
          nonLumetriSync.clipsWithComponents +
          " clip(s), " +
          nonLumetriWrites +
          " write(s), " +
          nonLumetriSync.propFails +
          " fail(s).",
      );
    }

    var lumetriSync = applyLumetriPresetValuesToTrack(stdTrack, presetFilePath);
    var lumetriWrites = lumetriSync.propWrites;
    if (lumetriSync.settingsApplied > 0 && lumetriSync.clipsWithLumetri > 0) {
      log(
        "Lumetri values synced from .prfpset: " +
          lumetriSync.clipsUpdated +
          "/" +
          lumetriSync.clipsWithLumetri +
          " clip(s), " +
          lumetriWrites +
          " write(s), " +
          lumetriSync.propFails +
          " fail(s).",
      );
    }

    var lookPathSync = applyLumetriLookPathToTrack(stdTrack, presetFilePath);
    var lookPathWrites = lookPathSync.propWrites;
    if (lookPathSync.resolvedPath) {
      log("Lumetri Look path resolved: " + lookPathSync.resolvedPath);
    } else {
      log(
        "Warning: Lumetri Look path not found in preset '" + presetName + "').",
      );
    }
    if (lookPathSync.candidateCount > 0 && lookPathSync.clipsWithLumetri > 0) {
      log(
        "Lumetri Look path synced from .prfpset: " +
          lookPathSync.clipsUpdated +
          "/" +
          lookPathSync.clipsWithLumetri +
          " clip(s), " +
          lookPathWrites +
          " write(s), " +
          lookPathSync.propFails +
          " fail(s), path mode: " +
          (lookPathSync.pathUsed || "none") +
          ".",
      );
    }

    if (
      totalVerified > 0 ||
      nonLumetriWrites > 0 ||
      lumetriWrites > 0 ||
      lookPathWrites > 0
    ) {
      log(
        "Preset pipeline completed for '" +
          presetName +
          "' on V" +
          (videoTrackIndex + 1) +
          " (verified component changes: " +
          totalVerified +
          ", non-Lumetri writes: " +
          nonLumetriWrites +
          ", lumetri writes: " +
          lumetriWrites +
          ", lumetri look path writes: " +
          lookPathWrites +
          ").",
      );
      return true;
    }

    log(
      "Warning: Preset pipeline finished for '" +
        presetName +
        "' but no verifiable component change was detected." +
        (anyApplied
          ? " QE may be swallowing preset/effect application in this Premiere build."
          : " No QE effect application call succeeded.") +
        (unresolvedEffects > 0
          ? " Unresolved effects from preset: " + unresolvedEffects + "."
          : ""),
    );
    return false;
  }

  function setScaleAndPosition(track, startTime, scaleVal) {
    if (!track) return;
    var item = findRecentTrackItemAtStart(track, startTime, null);
    if (!item) {
      item = findTrackItemAtStart(track, startTime, null);
    }
    if (item) {
      setScaleOnItem(item, scaleVal);
    }
  }

  function placeOverlayOnTrack(track, filename, endSec) {
    if (!track || !filename || typeof endSec !== "number" || !(endSec > 0))
      return false;

    var overlayItem = getOrImportClip(filename);
    if (!overlayItem) {
      log("Warning: Overlay not found: " + filename);
      return false;
    }

    try {
      track.overwriteClip(overlayItem, 0);
    } catch (e0) {
      log(
        "Warning: Failed to place overlay '" +
          filename +
          "': " +
          (e0 && e0.message ? e0.message : e0),
      );
      return false;
    }

    var filenameNoExt = stripKnownExtension(filename);
    var item =
      waitForTrackItemAtStart(
        track,
        0,
        filenameNoExt,
        TRACK_ITEM_WAIT_MAX_MS,
      ) ||
      waitForTrackItemAtStart(track, 0, filename, TRACK_ITEM_WAIT_MAX_MS) ||
      findTrackItemAtStart(track, 0, null);

    if (!item) {
      log(
        "Warning: Overlay track item not found on timeline for '" +
          filename +
          "'.",
      );
      return false;
    }

    if (!setTrackItemEndSeconds(item, endSec)) {
      log(
        "Warning: Could not trim overlay '" +
          filename +
          "' to " +
          endSec +
          "s.",
      );
      return false;
    }
    return true;
  }

  function applyTrackItemGainDb(trackItem, gainDb) {
    if (!trackItem || typeof gainDb !== "number" || !trackItem.components)
      return false;

    var volumeComponent = null;
    for (var c = 0; c < trackItem.components.numItems; c++) {
      var comp = trackItem.components[c];
      if (!comp) continue;
      var compName = comp.displayName
        ? comp.displayName.toString().toLowerCase()
        : "";
      var compMatch = comp.matchName
        ? comp.matchName.toString().toLowerCase()
        : "";
      if (
        compName.indexOf("volume") !== -1 ||
        compMatch.indexOf("volume") !== -1
      ) {
        volumeComponent = comp;
        break;
      }
    }
    if (!volumeComponent || !volumeComponent.properties) return false;

    var levelProp = null;
    for (var p = 0; p < volumeComponent.properties.numItems; p++) {
      var prop = volumeComponent.properties[p];
      if (!prop) continue;
      var propName = prop.displayName
        ? prop.displayName.toString().toLowerCase()
        : "";
      var propMatch = prop.matchName
        ? prop.matchName.toString().toLowerCase()
        : "";
      if (
        propName === "level" ||
        propName === "niveau" ||
        propName.indexOf("level") !== -1 ||
        propName.indexOf("niveau") !== -1 ||
        propMatch.indexOf("level") !== -1
      ) {
        levelProp = prop;
        break;
      }
    }

    if (levelProp) {
      try {
        levelProp.setValue(gainDb, true);
        return true;
      } catch (e0) {}
    }

    for (var p2 = 0; p2 < volumeComponent.properties.numItems; p2++) {
      var fallbackProp = volumeComponent.properties[p2];
      if (!fallbackProp) continue;
      try {
        fallbackProp.setValue(gainDb, true);
        return true;
      } catch (e1) {}
    }
    return false;
  }

  function buildLoopedMusicBed(track, musicItem, targetEndSec, gainDb) {
    if (
      !track ||
      !musicItem ||
      typeof targetEndSec !== "number" ||
      !(targetEndSec > 0)
    ) {
      return false;
    }

    var clipName = musicItem.name ? musicItem.name.toString() : "";
    var clipNameNoExt = clipName.replace(/\.[^\.]+$/, "");
    var minStep = 1 / SEQ_FPS;
    var cursor = 0;
    var maxLoops = 2000;

    for (var loopCount = 0; loopCount < maxLoops; loopCount++) {
      if (cursor >= targetEndSec - minStep / 2) return true;

      try {
        track.overwriteClip(musicItem, cursor);
      } catch (e0) {
        log("Warning: Music placement failed at " + cursor + "s.");
        return false;
      }

      var placedItem =
        waitForTrackItemAtStart(
          track,
          cursor,
          clipNameNoExt,
          TRACK_ITEM_WAIT_MAX_MS,
        ) ||
        waitForTrackItemAtStart(
          track,
          cursor,
          clipName,
          TRACK_ITEM_WAIT_MAX_MS,
        ) ||
        findTrackItemAtStart(track, cursor, null);

      if (!placedItem) {
        log("Warning: Could not resolve placed music clip at " + cursor + "s.");
        return false;
      }

      if (!applyTrackItemGainDb(placedItem, gainDb)) {
        log(
          "Warning: Could not set music gain to " +
            gainDb +
            " dB on one segment.",
        );
      }

      var placedStart = getTrackItemStartSeconds(placedItem);
      if (typeof placedStart !== "number") placedStart = cursor;
      var placedEnd = getTrackItemEndSeconds(placedItem);
      if (
        typeof placedEnd !== "number" ||
        placedEnd <= placedStart + 0.000001
      ) {
        log("Warning: Invalid music clip duration at " + cursor + "s.");
        return false;
      }

      if (placedEnd >= targetEndSec - minStep / 2) {
        setTrackItemEndSeconds(placedItem, targetEndSec);
        return true;
      }

      var nextCursor = snapSecondsToFrame(placedEnd);
      if (!(nextCursor > placedStart + 0.000001)) {
        nextCursor = snapSecondsToFrame(placedStart + minStep);
      }
      cursor = nextCursor;
    }

    log("Warning: Music loop guard reached before hitting target duration.");
    return false;
  }

  function ensureVideoTracks(sequence, desiredCount) {
    if (!sequence || !sequence.videoTracks) return;
    var existing = sequence.videoTracks.numTracks;
    if (existing >= desiredCount) return;

    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) return;
    var toAdd = desiredCount - existing;
    try {
      // addTracks(videoCount, insertAfterVideoIdx, audioCount)
      qeSeq.addTracks(toAdd, Math.max(0, existing - 1), 0);
    } catch (e) {
      // fallback
      for (var i = 0; i < toAdd; i++) {
        try {
          qeSeq.addTracks(1, Math.max(0, existing - 1 + i), 0);
        } catch (e2) {}
      }
    }
  }

  function ensureAudioTracks(sequence, desiredCount) {
    if (!sequence || !sequence.audioTracks) return;
    var existing = sequence.audioTracks.numTracks;
    if (existing >= desiredCount) return;

    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) return;
    var toAdd = desiredCount - existing;
    try {
      // addTracks(video, insertAfterVideo, audio, insertAfterAudio)
      qeSeq.addTracks(0, 0, toAdd, Math.max(0, existing - 1));
    } catch (e) {
      for (var i = 0; i < toAdd; i++) {
        try {
          qeSeq.addTracks(0);
        } catch (e2) {}
      }
    }
  }

  function purgeProjectCompletely() {
    if (!app.project || !app.project.rootItem) return false;
    var root = app.project.rootItem;
    var hadWarnings = false;

    try {
      if (
        app.project.sequences &&
        app.project.sequences.numSequences !== undefined
      ) {
        for (var s = app.project.sequences.numSequences - 1; s >= 0; s--) {
          var seq = app.project.sequences[s];
          if (!seq) continue;
          var deleted = false;
          try {
            if (seq.sequenceID !== undefined) {
              deleted = app.project.deleteSequence(seq.sequenceID);
            }
          } catch (e0) {}
          if (!deleted) {
            try {
              deleted = app.project.deleteSequence(seq);
            } catch (e1) {}
          }
          if (!deleted) {
            hadWarnings = true;
            var seqName = "";
            try {
              seqName = seq.name ? seq.name.toString() : "sequence#" + s;
            } catch (e2) {
              seqName = "sequence#" + s;
            }
            log("Warning: Could not delete sequence '" + seqName + "'.");
          }
        }
      }
    } catch (eSeq) {
      hadWarnings = true;
      log(
        "Warning: Sequence purge failed: " +
          (eSeq && eSeq.message ? eSeq.message : eSeq),
      );
    }

    var purgeBin = null;
    try {
      purgeBin = root.createBin(PROJECT_PURGE_BIN_NAME);
    } catch (eCreate) {}
    if (!purgeBin) {
      log(
        "Error: Could not create purge bin '" + PROJECT_PURGE_BIN_NAME + "'.",
      );
      return false;
    }

    var moveGuard = 0;
    while (root.children && root.children.numItems > 1 && moveGuard < 10000) {
      moveGuard++;
      var movedInPass = false;

      for (var i = root.children.numItems - 1; i >= 0; i--) {
        var child = root.children[i];
        if (!child || child === purgeBin) continue;
        try {
          child.moveBin(purgeBin);
          movedInPass = true;
        } catch (eMove) {
          hadWarnings = true;
          var childName = "";
          try {
            childName = child.name ? child.name.toString() : "item#" + i;
          } catch (eName) {
            childName = "item#" + i;
          }
          log(
            "Warning: Could not move item '" + childName + "' into purge bin.",
          );
        }
      }

      if (!movedInPass) break;
    }
    if (moveGuard >= 10000) {
      hadWarnings = true;
      log("Warning: Purge guard reached while moving project items.");
    }

    var deletedBin = false;
    try {
      deletedBin = purgeBin.deleteBin();
    } catch (eDel0) {}
    if (!deletedBin) {
      try {
        app.project.deleteBin(purgeBin);
        deletedBin = true;
      } catch (eDel1) {}
    }
    if (!deletedBin) {
      log(
        "Error: Could not delete purge bin '" + PROJECT_PURGE_BIN_NAME + "'.",
      );
      return false;
    }

    PROJECT_ITEM_CACHE = {};
    PROJECT_ITEM_CACHE_WARMED = false;

    var remainingSequences = 0;
    try {
      remainingSequences =
        app.project.sequences &&
        app.project.sequences.numSequences !== undefined
          ? app.project.sequences.numSequences
          : 0;
    } catch (e3) {
      remainingSequences = 0;
    }
    var remainingRootItems = 0;
    try {
      remainingRootItems = root.children ? root.children.numItems : 0;
    } catch (e4) {
      remainingRootItems = 0;
    }

    if (remainingSequences > 0 || remainingRootItems > 0) {
      log(
        "Error: Project purge incomplete (sequences: " +
          remainingSequences +
          ", root items: " +
          remainingRootItems +
          ").",
      );
      return false;
    }

    if (hadWarnings) {
      log("Info: Purge completed with warnings but final state is clean.");
    }
    return true;
  }

  function cleanupAudioTracks(ttsTrackIndex, ttsName) {
    var seq = app.project.activeSequence;
    if (!seq || !seq.audioTracks) return;
    for (var i = 0; i < seq.audioTracks.numTracks; i++) {
      if (i === 0) continue; // keep A1
      var track = seq.audioTracks[i];
      if (!track || !track.clips) continue;
      for (var j = track.clips.numItems - 1; j >= 0; j--) {
        var clip = track.clips[j];
        var nm = clip && clip.projectItem ? clip.projectItem.name : "";
        var keep = i === ttsTrackIndex && nm === ttsName;
        if (!keep) {
          try {
            clip.remove(false, true);
          } catch (e1) {
            try {
              clip.remove();
            } catch (e2) {}
          }
        }
      }
    }
  }

  main();
})();
