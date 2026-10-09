"""Facecam-only specialization; the shared JSX and standard exports stay unchanged."""
from pathlib import Path


def specialize_facecam_jsx(content: str) -> str:
    def replace(old, new):
        nonlocal content
        if content.count(old) != 1:
            raise RuntimeError(f"Point d'insertion JSX Facecam introuvable ou ambigu : {old[:90]}")
        content = content.replace(old, new, 1)

    replace("ensureVideoTracks(sequence, FACECAM ? 7 : 6);", "ensureVideoTracks(sequence, 8);")
    replace("// V4: Index 3 (Subs)\n    // V5: Index 4 (Reserved)\n    // V6: Index 5 (Reserved)",
            "// V4: Index 3 (Editor illustrations, 100%)\n    // V5: Index 4 (Facecam + final card)\n"
            "    // V6: Index 5 (Subs)\n    // V7/V8: Index 6/7 (Overlays)")
    replace("sequence.videoTracks[FACECAM ? 5 : 4]", "sequence.videoTracks[6]")
    replace("sequence.videoTracks[FACECAM ? 6 : 5]", "sequence.videoTracks[7]")
    replace("overlayFadeTrackIndexes[overlayFadeIdx] + (FACECAM ? 1 : 0)",
            "overlayFadeTrackIndexes[overlayFadeIdx] + 2")
    for old, new in ((4, 6), (5, 7)):
        replace(f"applyVideoPresetToTrackItems(\n        {old},\n        CATEGORY_TITLE_PRESET_NAME,",
                f"applyVideoPresetToTrackItems(\n        {new},\n        CATEGORY_TITLE_PRESET_NAME,")
    replace("      FACECAM ? 4 : 3,\n      SUBTITLE_MOGRT_DIR,", "      5,\n      SUBTITLE_MOGRT_DIR,")
    replace('    // --- APPLY VIDEO PRESETS ---',
            '    placeFacecamEditorTrack(sequence, qeSeq);\n\n    // --- APPLY VIDEO PRESETS ---')
    replace('    perfEnd("presets_v3");',
            '    perfEnd("presets_v3");\n'
            '    applyVideoPresetToTrackItems(3, FOREGROUND_PRESET_NAME,\n'
            '      FOREGROUND_PRESET_FILE_PATH, qeSeq, QE_TRACK_RESOLVE_CACHE);\n'
            '    verifyFacecamEditorTrack(sequence);')
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
    camera = original.replace("var track = sequence.videoTracks[3];", "var track = sequence.videoTracks[4];")
    camera = camera.replace("var main = sequence.videoTracks[2];", "var main = sequence.videoTracks[4];")
    helpers = (Path(__file__).parent / "templates/premiere_facecam_editor_track.jsx").read_text(encoding="utf-8")
    replace(original, helpers + "\n" + camera)
    return content
