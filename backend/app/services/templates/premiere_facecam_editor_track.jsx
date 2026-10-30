  // V4 is built from video-only source subclips, never from retimed V3 instances.
  function isFacecamDerivedSubclip(item) {
    var name = item && item.name ? String(item.name) : "";
    return name.indexOf("V4_100__") === 0 || name.indexOf("__ATR_RAW_AUDIO__") === 0;
  }

  function facecamEditorSubclipName(part) {
    return "V4_100__" + PROJECT_ID + "__" + PROJECT_EXECUTION_ID + "__" + part.subclip_key;
  }

  function getFacecamEditorSubclip(part) {
    var name = facecamEditorSubclipName(part);
    var existing = getCachedProjectItem(name) || findProjectItem(name);
    if (existing) return existing;
    var source = getOrImportClip(part.clipName);
    if (!source || !source.createSubClip) throw new Error("Source V4 introuvable : " + part.clipName);
    // New episode candidates carry their own native clock (24 != 23.976).
    var sourceIn = part.source_fps ? buildRawTimeFromSeconds(part.source_in) : buildRawTimeFromSourceFrame(part.source_in_frame, false);
    var sourceOut = part.source_fps ? buildRawTimeFromSeconds(part.source_out) : buildRawTimeFromSourceFrame(part.source_out_frame, false);
    // No hard boundaries: the editor can extend the subclip into the episode.
    // Adobe's parameter order is takeVideo, takeAudio. Audio must not touch A2/A4+.
    var subclip = source.createSubClip(name, sourceIn.ticks, sourceOut.ticks, 0, 1, 0);
    if (!subclip) subclip = findProjectItem(name);
    if (!subclip) throw new Error("Création du sous-clip V4 impossible : " + name);
    moveItemToProjectBin(subclip);
    cacheProjectItem(subclip);
    cacheProjectItemByName(name, subclip);
    return subclip;
  }

  function verifyFacecamEditorClip(item, part) {
    if (!item) throw new Error("Clip V4 absent : " + part.id);
    var speed = Number(item.getSpeed());
    if (!isFinite(speed) || Math.abs(speed - 1) > 0.000001 || item.isSpeedReversed()) {
      throw new Error("V4 doit rester strictement à 100 % : " + part.id);
    }
    var start = getTrackItemStartSeconds(item);
    var end = getTrackItemEndSeconds(item);
    if (typeof start !== "number" || typeof end !== "number" ||
        Math.round(start * SEQ_FPS) !== part.start_frame || Math.round(end * SEQ_FPS) !== part.end_frame) {
      throw new Error("Bornes du clip V4 incorrectes : " + part.id);
    }
  }

  function placeFacecamEditorTrack(sequence, qeSeq) {
    if (!FACECAM.editor_track) throw new Error("La sélection V4 manque dans le JSX Facecam.");
    var track = sequence.videoTracks[3];
    var parts = FACECAM.editor_track.clips;
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      var start = part.start_frame / SEQ_FPS;
      var subclip = getFacecamEditorSubclip(part);
      track.overwriteClip(subclip, start);
      var item = resolvePlacedItemFast(track, start, facecamEditorSubclipName(part), TRACK_ITEM_WAIT_MAX_MS);
      // Trim before placing the next clip: overwrite alone can leave an old tail.
      if (!item || !setTrackItemEndSeconds(item, part.end_frame / SEQ_FPS)) {
        throw new Error("Placement du clip V4 impossible : " + part.id);
      }
      var cleanName = stripKnownExtension(part.clipName);
      var geo = getSourceGeometry(cleanName);
      var scale = geo && typeof geo.fg_scale === "number" ? geo.fg_scale : DEFAULT_FG_SCALE;
      if (!setScaleOnItem(item, scale)) throw new Error("Cadrage du clip V4 impossible : " + part.id);
      if (geo && geo.mode === "pure") {
        ensureCropOnSceneItem(item, geo, start, cleanName, qeSeq, QE_TRACK_RESOLVE_CACHE, 3, "V4");
        if (geo.fg_pos) setPositionOnItem(item, geo.fg_pos);
      }
      verifyFacecamEditorClip(item, part);
    }
  }

  function verifyFacecamEditorTrack(sequence) {
    var track = sequence.videoTracks[3];
    var parts = FACECAM.editor_track.clips;
    if (track.clips.numItems !== parts.length) throw new Error("Nombre de clips V4 incorrect après insertion.");
    for (var i = 0; i < parts.length; i++) {
      var item = findTrackItemAtStart(track, parts[i].start_frame / SEQ_FPS, facecamEditorSubclipName(parts[i]));
      verifyFacecamEditorClip(item, parts[i]);
    }
    log("V4 : " + parts.length + " clips vérifiés à 100 %, trous conservés.");
  }

  function finishFacecamEditorTrack(sequence) {
    try { sequence.videoTracks[2].name = "Montage automatique"; } catch (eNameV3) {}
    try { sequence.videoTracks[3].name = "Illustrations 100 %"; } catch (eNameV4) {}
    try {
      sequence.videoTracks[2].setMute(1);
      if (!sequence.videoTracks[2].isMuted()) throw new Error("V3 reste visible");
    } catch (eMute) {
      var message = "V3 n'a pas pu être masquée automatiquement. Désactivez son oeil manuellement.";
      log("Warning: " + message);
      try {
        var previous = String($.global.__ATR_IMPORT_WARNINGS__ || "");
        $.global.__ATR_IMPORT_WARNINGS__ = previous + (previous ? "\n" : "") + message;
      } catch (eWarning) {}
    }
  }
