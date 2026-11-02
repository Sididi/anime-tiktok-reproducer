"""Facecam-only specialization; the shared JSX and standard exports stay unchanged."""
from pathlib import Path


def specialize_facecam_jsx(content: str) -> str:
    def replace(old, new):
        nonlocal content
        if content.count(old) != 1:
            raise RuntimeError(f"Point d'insertion JSX Facecam introuvable ou ambigu : {old[:90]}")
        content = content.replace(old, new, 1)

    def replace_section(start, end, new):
        if content.count(start) != 1 or content.count(end) != 1:
            raise RuntimeError(f"Section JSX Facecam introuvable ou ambiguë : {start}")
        begin = content.index(start)
        replace(content[begin:content.index(end, begin)], new)

    replace("// --- ENSURE TRACKS (V=6, A=4+) ---", "// --- ENSURE TRACKS (V=5, optional overlays on V6/V7, A=4+) ---")
    replace("ensureVideoTracks(sequence, FACECAM ? 7 : 6);", "ensureVideoTracks(sequence, OVERLAY_ENABLED ? 7 : 5);")
    replace_section("    // Mapping Tracks\n", "    var a1 = sequence.audioTracks[0];", """    // Mapping Tracks
    // V1: Index 0 (Editor illustrations, 100%, gaps preserved)
    // V2: Index 1 (Empty)
    // V3: Index 2 (Facecam + final card)
    // V4: Index 3 (Empty)
    // V5: Index 4 (Subs)
    // V6/V7: Index 5/6 (Optional overlays)
    // A1: Index 0 (Muted)
    // A2: Index 1 (Narration)
    // A3: Index 2 (Music bed)
    // A4+: Index 3+ (Raw scene audio reserved zone - active)
    var categoryOverlayTrack =
      sequence.videoTracks.numTracks > 5 ? sequence.videoTracks[5] : null;
    var titleOverlayTrack =
      sequence.videoTracks.numTracks > 6 ? sequence.videoTracks[6] : null;
""")

    # Build only the native-speed illustrations. The former background and
    # retimed montage must never be placed, even temporarily (linked audio/QE).
    replace_section("    // --- INTERLEAVED PROCESSING (V1 & V3) ---\n",
                    "    // --- IMPORT TTS (A2), THEN BUILD OVERLAYS + MUSIC FROM TTS DURATION ---\n",
                    '    perfStart("scenes");\n'
                    '    placeFacecamEditorTrack(sequence, qeSeq);\n'
                    '    perfEnd("scenes", "Scenes");\n\n')
    replace_section("    // Give Premiere an early chance to materialize the border image before\n",
                    "    clearRawAudioZone(sequence, RAW_AUDIO_TRACK_START_INDEX, rawAudioZoneWidth);", "")
    replace_section("    // Verify the early border after the Motion Graphics work has settled. If\n",
                    "    // Run optional audio effects last, after all placement and video QE work.\n", "")

    replace("          v5,\n          CATEGORY_OVERLAY_FILENAME,", "          categoryOverlayTrack,\n          CATEGORY_OVERLAY_FILENAME,")
    replace("          v6,\n          TITLE_OVERLAY_FILENAME,", "          titleOverlayTrack,\n          TITLE_OVERLAY_FILENAME,")
    replace("overlayFadeTrackIndexes[overlayFadeIdx] + (FACECAM ? 1 : 0)",
            "overlayFadeTrackIndexes[overlayFadeIdx] + 1")
    for old, new in ((5, 6), (4, 5)):
        replace(f"applyVideoPresetToTrackItems(\n        {old},\n        CATEGORY_TITLE_PRESET_NAME,",
                f"applyVideoPresetToTrackItems(\n        {new},\n        CATEGORY_TITLE_PRESET_NAME,")
    for old, new in (
        ("Adding overlays on V5/V6", "Adding overlays on V6/V7"),
        ('CATEGORY_OVERLAY_FILENAME + " on V5."', 'CATEGORY_OVERLAY_FILENAME + " on V6."'),
        ('TITLE_OVERLAY_FILENAME + " on V6."', 'TITLE_OVERLAY_FILENAME + " on V7."'),
        ("Skipping V5/V6 overlays", "Skipping V6/V7 overlays"),
        ("Applying Category Title preset on V6", "Applying Category Title preset on V7"),
        ("Applying Category Title preset on V5", "Applying Category Title preset on V6"),
    ):
        replace(old, new)
    replace_section('    log("Applying Background preset on V1...");\n',
                    '    if (OVERLAY_ENABLED && CATEGORY_OVERLAY_ENABLED) {\n      log("Applying Category Title preset',
                    '    log("Applying Foreground preset on V1...");\n'
                    '    perfStart("presets_v1");\n'
                    '    applyVideoPresetToTrackItems(0, FOREGROUND_PRESET_NAME,\n'
                    '      FOREGROUND_PRESET_FILE_PATH, qeSeq, QE_TRACK_RESOLVE_CACHE);\n'
                    '    perfEnd("presets_v1");\n'
                    '    verifyFacecamEditorTrack(sequence);\n')
    replace("// --- V4: SUBTITLES", "// --- V5: SUBTITLES")
    replace("Loading subtitle MOGRT files to V4", "Loading subtitle MOGRT files to V5")
    replace("      FACECAM ? 4 : 3,\n      SUBTITLE_MOGRT_DIR,", "      4,\n      SUBTITLE_MOGRT_DIR,")
    replace("    if (FACECAM) placeFacecam(sequence);",
            "    if (FACECAM) {\n      placeFacecam(sequence);\n      finishFacecamEditorTrack(sequence);\n    }")
    replace("    preloadNames[AUDIO_FILENAME] = true;",
            "    for (var editorPreload = 0; editorPreload < FACECAM.editor_track.clips.length; editorPreload++) {\n"
            "      preloadNames[FACECAM.editor_track.clips[editorPreload].clipName] = true;\n"
            "    }\n    preloadNames[AUDIO_FILENAME] = true;")
    # Subclips share the episode's media path. They must never replace the
    # master source in that index, including when importing again in the same project.
    replace("  function cacheProjectItemByMediaPath(mediaPath, item) {\n    if (!item) return;",
            "  function cacheProjectItemByMediaPath(mediaPath, item) {\n"
            "    if (!item || isFacecamDerivedSubclip(item)) return;")
    replace("      if (found || isBinItem(item)) return;\n      if (getProjectItemMediaPath(item) === normalizedPath)",
            "      if (found || isBinItem(item) || isFacecamDerivedSubclip(item)) return;\n"
            "      if (getProjectItemMediaPath(item) === normalizedPath)")

    # Modify only the camera helper, not other occurrences of these track indices.
    start = content.index("  function placeFacecam(sequence) {")
    end = content.index("\n  function ", start + 1)
    original = content[start:end]
    camera = original.replace("var track = sequence.videoTracks[3];", "var track = sequence.videoTracks[2];")
    helpers = (Path(__file__).parent / "templates/premiere_facecam_editor_track.jsx").read_text(encoding="utf-8")
    replace(original, helpers + "\n" + camera)
    return content
