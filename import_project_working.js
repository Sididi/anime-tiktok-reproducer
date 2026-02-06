/**
 * Anime TikTok Reproducer - Premiere Pro 2025 Automation Script (v7.1 - CLEANED)
 *
 * CHANGES from v6:
 * - 4-Track Structure: V4(Subtitles - Reserved), V3(Main), V2(Border), V1(Background).
 * - Interleaved Speed & Placement for V1 and V3.
 * - Scaling: V1 (183%), V3 (68%).
 * - Audio: Cleans A2 before placing TTS.
 */

(function () {
  // ========================================================================
  // 1. CONFIGURATION
  // ========================================================================
  var SCRIPT_FILE = new File($.fileName);
  var ROOT_DIR = SCRIPT_FILE.parent.fsName;
  var ASSETS_DIR = ROOT_DIR + "/assets";
  var SOURCES_DIR = ROOT_DIR + "/sources";

  var SEQUENCE_PRESET_PATH = ASSETS_DIR + "/TikTok60fps.sqpreset";
  var BORDER_MOGRT_PATH = ASSETS_DIR + "/White border 5px.mogrt";
  var AUDIO_FILENAME = "tts_edited.wav";

  // --- SCENES DATA ---
  var scenes = [
    {
      scene_index: 0,
      start: 0.0,
      end: 2.166667,
      text: "En une seule séance, elle a enchaîné 6 000 coups de raquette,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 419.0,
      source_out: 421.0,
      clip_duration: 2.0,
      target_duration: 2.1667,
      speed_ratio: 0.9231,
      effective_speed: 0.9231,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 1,
      start: 2.166667,
      end: 4.333333,
      text: "et rentré 200 tirs d'affilée. C'est juste une lycéenne,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 424.0,
      source_out: 427.0,
      clip_duration: 3.0,
      target_duration: 2.1667,
      speed_ratio: 1.3846,
      effective_speed: 1.3846,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 2,
      start: 4.333333,
      end: 5.9,
      text: "mais elle tient tête aux pros sans transpirer.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 429.0,
      source_out: 430.5,
      clip_duration: 1.5,
      target_duration: 1.5667,
      speed_ratio: 0.9574,
      effective_speed: 0.9574,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 3,
      start: 5.9,
      end: 7.366667,
      text: "Un vrai génie, le genre qu'on voit jamais,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 476.5,
      source_out: 478.0,
      clip_duration: 1.5,
      target_duration: 1.4667,
      speed_ratio: 1.0227,
      effective_speed: 1.0227,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 4,
      start: 7.366667,
      end: 10.05,
      text: "capable de couvrir ses alliés tout en claquant un smash sauté.",
      clipName: "[9volt] Hanebado! - 02 [6C96B36F]",
      source_in: 697.5,
      source_out: 701.0,
      clip_duration: 3.5,
      target_duration: 2.6833,
      speed_ratio: 1.3043,
      effective_speed: 1.3043,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 5,
      start: 10.05,
      end: 11.45,
      text: "Sa défense ? Juste imprenable,",
      clipName: "[9volt] Hanebado! - 02 [6C96B36F]",
      source_in: 42.0,
      source_out: 43.5,
      clip_duration: 1.5,
      target_duration: 1.4,
      speed_ratio: 1.0714,
      effective_speed: 1.0714,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 6,
      start: 11.45,
      end: 13.45,
      text: "elle renvoie tout en un éclair. Mais malgré ce talent,",
      clipName: "[9volt] Hanebado! - 02 [6C96B36F]",
      source_in: 44.1,
      source_out: 46.5,
      clip_duration: 2.4,
      target_duration: 2.0,
      speed_ratio: 1.2,
      effective_speed: 1.2,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 7,
      start: 13.45,
      end: 15.316667,
      text: "une seule défaite au collège l'a complètement brisée,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 1018.0,
      source_out: 1020.0,
      clip_duration: 2.0,
      target_duration: 1.8667,
      speed_ratio: 1.0714,
      effective_speed: 1.0714,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 8,
      start: 15.316667,
      end: 17.583333,
      text: "et depuis, elle refuse de jouer sérieusement au badminton.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 391.5,
      source_out: 393.5,
      clip_duration: 2.0,
      target_duration: 2.2667,
      speed_ratio: 0.8824,
      effective_speed: 0.8824,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 9,
      start: 17.583333,
      end: 19.283333,
      text: "Au club, elle est présente mais l'esprit ailleurs.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 483.5,
      source_out: 485.0,
      clip_duration: 1.5,
      target_duration: 1.7,
      speed_ratio: 0.8824,
      effective_speed: 0.8824,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 10,
      start: 19.283333,
      end: 20.1,
      text: "Pour la secouer un peu,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 636.5,
      source_out: 637.5,
      clip_duration: 1.0,
      target_duration: 0.8167,
      speed_ratio: 1.2245,
      effective_speed: 1.2245,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 11,
      start: 20.1,
      end: 21.716667,
      text: "sa meilleure pote tente un coup de poker.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 643.5,
      source_out: 645.1167,
      clip_duration: 1.6167,
      target_duration: 1.6167,
      speed_ratio: 1.0,
      effective_speed: 1.0,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 12,
      start: 21.716667,
      end: 24.35,
      text: "Le lendemain, une nouvelle arrive et la provoque direct en duel.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 657.5,
      source_out: 660.5,
      clip_duration: 3.0,
      target_duration: 2.6333,
      speed_ratio: 1.1392,
      effective_speed: 1.1392,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 13,
      start: 24.35,
      end: 26.333333,
      text: "Après les présentations, surprise : c'est une première année,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 679.0,
      source_out: 684.0,
      clip_duration: 5.0,
      target_duration: 1.9833,
      speed_ratio: 2.521,
      effective_speed: 2.521,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 14,
      start: 26.333333,
      end: 27.966667,
      text: "mais une crack, classée top 3 national.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 696.5,
      source_out: 698.5,
      clip_duration: 2.0,
      target_duration: 1.6333,
      speed_ratio: 1.2245,
      effective_speed: 1.2245,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 15,
      start: 27.966667,
      end: 30.033333,
      text: "Notre prodige l'ignore, mais l'autre est super têtue.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 714.5,
      source_out: 717.5,
      clip_duration: 3.0,
      target_duration: 2.0667,
      speed_ratio: 1.4516,
      effective_speed: 1.4516,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 16,
      start: 30.033333,
      end: 31.716667,
      text: "Elle l'attend plantée là toute la journée,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 720.0,
      source_out: 722.5135,
      clip_duration: 2.5135,
      target_duration: 1.6833,
      speed_ratio: 1.4931,
      effective_speed: 1.4931,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 17,
      start: 31.716667,
      end: 34.7,
      text: "obligeant la championne à accepter. Le match démarre et là, stupeur :",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 728.5,
      source_out: 732.5,
      clip_duration: 4.0,
      target_duration: 2.9833,
      speed_ratio: 1.3408,
      effective_speed: 1.3408,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 18,
      start: 34.7,
      end: 36.966667,
      text: "La nouvelle vise pile ses points faibles. En trois coups, elle marque,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 734.5,
      source_out: 738.5,
      clip_duration: 4.0,
      target_duration: 2.2667,
      speed_ratio: 1.7647,
      effective_speed: 1.7647,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 19,
      start: 36.966667,
      end: 38.116667,
      text: "laissant tout le gymnase bouche bée.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 739.5,
      source_out: 740.5,
      clip_duration: 1.0,
      target_duration: 1.15,
      speed_ratio: 0.8696,
      effective_speed: 0.8696,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 20,
      start: 38.116667,
      end: 39.8,
      text: "C'est la première fois qu'elle se fait autant malmener.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 756.0,
      source_out: 758.0,
      clip_duration: 2.0,
      target_duration: 1.6833,
      speed_ratio: 1.1881,
      effective_speed: 1.1881,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 21,
      start: 39.8,
      end: 42.3,
      text: "Son adversaire anticipe tout et adapte sa stratégie en temps réel.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 768.5,
      source_out: 771.5,
      clip_duration: 3.0,
      target_duration: 2.5,
      speed_ratio: 1.2,
      effective_speed: 1.2,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 22,
      start: 42.3,
      end: 44.966667,
      text: "Un vrai jeu mental. Elle appuie là où ça fait mal au pire moment. En dix minutes,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 774.0,
      source_out: 782.0,
      clip_duration: 8.0,
      target_duration: 2.6667,
      speed_ratio: 3.0,
      effective_speed: 3.0,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 23,
      start: 44.966667,
      end: 47.716667,
      text: "l'écart est de 13 points. Une humiliation. La gagnante est même déçue.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 784.5,
      source_out: 788.5,
      clip_duration: 4.0,
      target_duration: 2.75,
      speed_ratio: 1.4545,
      effective_speed: 1.4545,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 24,
      start: 47.716667,
      end: 50.0,
      text: "Tout le monde comprend alors : c'est ELLE, sa rivale du collège.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 794.0,
      source_out: 799.0,
      clip_duration: 5.0,
      target_duration: 2.2833,
      speed_ratio: 2.1898,
      effective_speed: 2.1898,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 25,
      start: 50.0,
      end: 51.65,
      text: "Le coach veut débriefer, mais elle tourne les talons.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 806.0,
      source_out: 808.0,
      clip_duration: 2.0,
      target_duration: 1.65,
      speed_ratio: 1.2121,
      effective_speed: 1.2121,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 26,
      start: 51.65,
      end: 53.616667,
      text: "Sur le coup, elle jure de ne plus jamais toucher une raquette,",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 817.0,
      source_out: 831.5,
      clip_duration: 14.5,
      target_duration: 1.9667,
      speed_ratio: 7.3729,
      effective_speed: 7.3729,
      leaves_gap: false,
      used_alternative: false,
    },
    {
      scene_index: 27,
      start: 53.616667,
      end: 54.933333,
      text: "et rien ne pourra la faire changer d'avis.",
      clipName: "[9volt] Hanebado! - 03 [D0B8F455]",
      source_in: 841.0,
      source_out: 843.0,
      clip_duration: 2.0,
      target_duration: 1.3167,
      speed_ratio: 1.519,
      effective_speed: 1.519,
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
  var TICKS_PER_FRAME = TICKS_PER_SECOND / SEQ_FPS;

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

  function buildTimeFromSeconds(sec) {
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
        // Name Check
        if (nameRef) {
          var itemName = item.name ? item.name.toString() : "";
          if (itemName.replace(/\s/g, "") !== "") {
            // Check containment both ways
            if (
              itemName.indexOf(nameRef) === -1 &&
              nameRef.indexOf(itemName) === -1
            ) {
              continue;
            }
          }
        }

        // We found a candidate. Is it the best one?
        if (diff < minDiff) {
          minDiff = diff;
          bestItem = item;
        }
      }
    }
    return bestItem;
  }

  function setTrackItemInOut(
    track,
    startSeconds,
    inSeconds,
    outSeconds,
    nameRef,
  ) {
    var item = findTrackItemAtStart(track, startSeconds, nameRef);
    if (!item) return null;
    try {
      item.inPoint = buildTimeFromSeconds(inSeconds);
      item.outPoint = buildTimeFromSeconds(outSeconds);
    } catch (e) {
      log("Warning: Failed to set in/out for item at " + startSeconds);
    }
    return item;
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
    var findInBin = function (bin) {
      for (var i = 0; i < bin.children.numItems; i++) {
        var item = bin.children[i];
        if (item.name === name) return item;
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
    var nameNoExt = cleanName.replace(/\.[^\.]+$/, "");

    var item = findProjectItem(cleanName);
    if (item) return item;
    item = findProjectItem(nameNoExt);
    if (item) return item;

    var searchPaths = [
      ROOT_DIR + "/" + cleanName,
      ROOT_DIR + "/" + cleanName + ".wav",
      SOURCES_DIR + "/" + cleanName,
      SOURCES_DIR + "/" + nameNoExt + ".mkv",
      SOURCES_DIR + "/" + nameNoExt + ".mp4",
    ];

    for (var i = 0; i < searchPaths.length; i++) {
      var f = new File(searchPaths[i]);
      if (f.exists) {
        app.project.importFiles([f.fsName], true, app.project.rootItem, false);
        item = findProjectItem(f.name);
        if (!item) item = findProjectItem(f.displayName);
        if (!item) item = findProjectItem(nameNoExt);
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

    var seqName = "ATR_Layered_" + Math.floor(Math.random() * 9999);
    var presetFile = new File(SEQUENCE_PRESET_PATH);
    var sequence;

    if (presetFile.exists) {
      qe.project.newSequence(seqName, presetFile.fsName);
      sequence = app.project.activeSequence;
    } else {
      sequence = app.project.createNewSequence(seqName, "ID_1");
    }

    // --- ENSURE TRACKS (V=4, A=2) ---
    ensureVideoTracks(sequence, 4);
    ensureAudioTracks(sequence, 2);

    // Mapping Tracks
    // V1: Index 0 (Back)
    // V2: Index 1 (Border)
    // V3: Index 2 (Main)
    // V4: Index 3 (Subs)

    var v1 = sequence.videoTracks[0];
    var v2 =
      sequence.videoTracks.numTracks > 1 ? sequence.videoTracks[1] : null;
    var v3 =
      sequence.videoTracks.numTracks > 2 ? sequence.videoTracks[2] : null;
    var v4 =
      sequence.videoTracks.numTracks > 3 ? sequence.videoTracks[3] : null;

    var a1 = sequence.audioTracks[0];
    var a2 = sequence.audioTracks.numTracks > 1 ? sequence.audioTracks[1] : a1;

    // --- MUTE A1 (Clip Audio) ---
    try {
      a1.setMute(1);
    } catch (e) {}

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
      return n.replace(/\.[^\.]+$/, "");
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
        sleep(200);
        var v3Item = null;
        var v1Item = null;
        var a1Item = null;
        var a2Item = null;
        if (v3) {
          v3Item = setTrackItemInOut(
            v3,
            startSec,
            s.source_in,
            s.source_out,
            cleanName,
          );
          if (!v3Item) {
            sleep(200);
            v3Item = setTrackItemInOut(
              v3,
              startSec,
              s.source_in,
              s.source_out,
              cleanName,
            );
          }
        }
        if (v1) {
          v1Item = setTrackItemInOut(
            v1,
            startSec,
            s.source_in,
            s.source_out,
            cleanName,
          );
          if (!v1Item) {
            sleep(200);
            setTrackItemInOut(
              v1,
              startSec,
              s.source_in,
              s.source_out,
              cleanName,
            );
          }
        }
        if (a1) {
          a1Item = setTrackItemInOut(
            a1,
            startSec,
            s.source_in,
            s.source_out,
            cleanName,
          );
          if (!a1Item) {
            sleep(200);
            a1Item = setTrackItemInOut(
              a1,
              startSec,
              s.source_in,
              s.source_out,
              cleanName,
            );
          }
        }
        if (a2 && a2 !== a1) {
          a2Item = setTrackItemInOut(
            a2,
            startSec,
            s.source_in,
            s.source_out,
            cleanName,
          );
          if (!a2Item) {
            sleep(200);
            a2Item = setTrackItemInOut(
              a2,
              startSec,
              s.source_in,
              s.source_out,
              cleanName,
            );
          }
        }

        // Force backend update & clear selection to avoid "Invalid TrackItem" assertion
        // The assertion often happens if a previous selection is invalid.
        sleep(1000);
        clearSelection(sequence);
        sleep(200);

        // 3. ENFORCE DURATION (ALL SPEEDS)
        // Always enforce the target timeline duration, even at 1.0x.
        // If in/out fails or speed is exactly 1.0, this prevents huge clip lengths.
        var newDurationSeconds = snapSecondsToFrame(
          s.clip_duration / s.effective_speed,
        );
        if (v3Item) {
          try {
            var newEnd = v3Item.start.seconds + newDurationSeconds;
            v3Item.end = buildTimeFromSeconds(newEnd);
          } catch (e) {}
        }
        if (v1Item) {
          try {
            var newEnd = v1Item.start.seconds + newDurationSeconds;
            v1Item.end = buildTimeFromSeconds(newEnd);
          } catch (e) {}
        }
        if (a1Item) {
          try {
            var newEnd = a1Item.start.seconds + newDurationSeconds;
            a1Item.end = buildTimeFromSeconds(newEnd);
          } catch (e) {}
        }
        if (a2Item) {
          try {
            var newEnd = a2Item.start.seconds + newDurationSeconds;
            a2Item.end = buildTimeFromSeconds(newEnd);
          } catch (e) {}
        }

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
            );
          if (v1)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              0,
              "Video",
              cleanName,
            );
          if (a1 && a1Item)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              0,
              "Audio",
              cleanName,
            );
          if (a2 && a2Item && a2 !== a1)
            safeApplySpeedQE(
              startSec,
              s.effective_speed,
              1,
              "Audio",
              cleanName,
            );
        }

        // 4. APPLY SCALE (Standard API)
        // Need to find the items we just placed.
        if (v3) setScaleAndPosition(v3, startSec, 68); // Main Scaled Down
        if (v1) setScaleAndPosition(v1, startSec, 183); // Background Scaled Up

        sleep(200);
        if (v3) {
          var v3ItemForLog = findTrackItemAtStart(v3, startSec, cleanName);
          if (v3ItemForLog)
            logClipDuration(
              v3ItemForLog,
              s.target_duration,
              "Scene " + s.scene_index,
            );
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

    // --- IMPORT TTS (A2) & CLEANUP A3 ---
    log("Importing TTS to A2...");
    if (a2) {
      var ttsItem = getOrImportClip(AUDIO_FILENAME);
      if (ttsItem) {
        a2.overwriteClip(ttsItem, 0);
      }
    }
    // Cleanup all audio tracks except A1 and A2 (TTS)
    cleanupAudioTracks(a2, 1, AUDIO_FILENAME);

    // --- V4: SUBTITLES (Reserved) ---
    // Subtitles will be added manually later.
    // Logic removed as requested.

    alert("Script Complete (v7 Layered - Fixes Applied).");
  }

  // ========================================================================
  // 4. HELPERS
  // ========================================================================

  function clearSelection(sequence) {
    if (!sequence) return;
    try {
      var tracks = sequence.videoTracks;
      for (var i = 0; i < tracks.numTracks; i++) {
        var track = tracks[i];
        for (var j = 0; j < track.clips.numItems; j++) {
          track.clips[j].setSelected(false, true);
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
  ) {
    try {
      var qeSeq = qe.project.getActiveSequence();
      if (!qeSeq) return;
      var qeTrack;
      if (trackType === "Audio") qeTrack = qeSeq.getAudioTrackAt(trackIndex);
      else qeTrack = qeSeq.getVideoTrackAt(trackIndex);
      if (!qeTrack) return;

      // Search for the item with Name validation and Time tolerance
      // Iterate ALL items to find the best match or correct item
      for (var i = 0; i < qeTrack.numItems; i++) {
        try {
          var item = qeTrack.getItemAt(i);
          // Defensive: access properties safely
          if (!item || typeof item.start === "undefined") continue;

          // Time Check (0.25s tolerance), prefer ticks when available
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
            matchTime =
              Math.abs(startTicks - secondsToTicks(startTime)) <
              secondsToTicks(0.2);
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

              // CRITICAL FIX: Ignore empty names which passed checks previously
              if (itemName.replace(/\s/g, "") === "") {
                // log("Skipping item with empty name at " + startTime);
                continue;
              }

              // Check if one contains the other (handle extensions)
              // clipNameRef is "cleanName" (no extension). itemName might have extension.
              // We need to be careful: "clip" vs "clip.mp4"
              var match = false;

              // 1. Exact or Substring match
              if (itemName.indexOf(clipNameRef) !== -1) match = true;
              if (clipNameRef.indexOf(itemName) !== -1) match = true;

              if (!match) {
                // log("Skipping speed on mismatch: '" + itemName + "' vs '" + clipNameRef + "'");
                continue;
              }
            }

            try {
              // args: speed, stretch, reverse, ripple, flicker
              // Pre-resizing for speed > 1 is handled in main loop (setOutPoint).
              // We just set the speed here.
              item.setSpeed(speed, "", false, false, false);
            } catch (err) {
              log("Speed Apply Error: " + err.message);
            }
            return; // Done
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
    } catch (e) {
      log("QE Speed Fail: " + e.message);
    }
  }

  function setScaleAndPosition(track, startTime, scaleVal) {
    // Find item in Track (Standard API)
    for (var i = 0; i < track.clips.numItems; i++) {
      var item = track.clips[i];
      // Standard API timings are in seconds (usually) or ticks.
      // item.start.seconds is available in 2025?
      // Use ticks if needed, but 'seconds' property usually works.
      var itemStartTicks = getStartTicks(item);
      if (
        (typeof itemStartTicks === "number" &&
          Math.abs(itemStartTicks - secondsToTicks(startTime)) <
            secondsToTicks(0.2)) ||
        (item.start &&
          typeof item.start.seconds === "number" &&
          Math.abs(item.start.seconds - startTime) < 0.2)
      ) {
        var m = item.components[1]; // Motion is usually index 1 (Opacity is 0 or 2?)
        // Actually index varies. Search for "Motion" or "Trajectoire"
        for (var c = 0; c < item.components.numItems; c++) {
          if (
            item.components[c].displayName === "Motion" ||
            item.components[c].displayName === "Trajectoire"
          ) {
            m = item.components[c];
            break;
          }
        }
        if (m) {
          // Scale is usually prop 0 or 1.
          // Position is usually prop 0. Scale prop 1.
          // "Scale" or "Echelle"
          for (var p = 0; p < m.properties.numItems; p++) {
            var prop = m.properties[p];
            if (
              prop.displayName === "Scale" ||
              prop.displayName === "Echelle" ||
              prop.displayName === "\u00c9chelle"
            ) {
              prop.setValue(scaleVal, true);
              break;
            }
          }
        }
        return;
      }
    }
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

  function cleanupAudioTracks(ttsTrack, ttsTrackIndex, ttsName) {
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
