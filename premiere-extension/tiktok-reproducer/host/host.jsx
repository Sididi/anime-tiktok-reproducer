/**
 * Tiktok Reproducer - ExtendScript Host
 *
 * Runs in Premiere Pro's ExtendScript engine.
 * Called from the CEP panel via csInterface.evalScript().
 */

var ATR_EXTENSION_ID = "com.animetiktok.tiktokreproducer.panel";
var __atrEncoderEvents = [];
var __atrEncoderJobProjectMap = {};
var __atrEncoderJobMetaMap = {};
var __atrEncoderCallbacksBound = false;
var __atrCleanupMaxBinPasses = 5;

function __atrSafeString(value) {
    if (value === undefined || value === null) {
        return "";
    }
    try {
        return String(value);
    } catch (e) {
        return "";
    }
}

function __atrNormalizePath(value) {
    return __atrSafeString(value).replace(/\\/g, "/");
}

function __atrNormalizeComparePath(value) {
    var normalized = __atrNormalizePath(value).toLowerCase();
    normalized = normalized.replace(/\/+/g, "/");
    if (normalized.length > 1 && normalized.charAt(normalized.length - 1) === "/") {
        normalized = normalized.substring(0, normalized.length - 1);
    }
    return normalized;
}

function __atrPathStartsWith(pathValue, rootValue) {
    if (!pathValue || !rootValue) {
        return false;
    }
    if (pathValue === rootValue) {
        return true;
    }
    return pathValue.indexOf(rootValue + "/") === 0;
}

function __atrGetProjectItemMediaPath(projectItem) {
    try {
        if (!projectItem || !projectItem.getMediaPath) {
            return "";
        }
        return __atrNormalizeComparePath(projectItem.getMediaPath());
    } catch (e) {
        return "";
    }
}

function __atrGetProjectItemChildCount(projectItem) {
    if (!projectItem || !projectItem.children) {
        return 0;
    }

    try {
        return Number(projectItem.children.numItems || 0);
    } catch (e) {
        return 0;
    }
}

function __atrInspectImportedSubtree(containerItem, normalizedRootPath, depth, report) {
    var importedLeaves = 0;
    var foreignLeaves = 0;
    var childCount = __atrGetProjectItemChildCount(containerItem);

    for (var i = 0; i < childCount; i += 1) {
        var child = containerItem.children[i];
        if (!child) {
            continue;
        }

        var nestedCount = __atrGetProjectItemChildCount(child);
        if (nestedCount > 0) {
            var subtree = __atrInspectImportedSubtree(child, normalizedRootPath, depth + 1, report);
            if (subtree.imported_leaves > 0 && subtree.foreign_leaves === 0) {
                report.deletable_bins.push({
                    item: child,
                    depth: depth + 1
                });
            }
            importedLeaves += Number(subtree.imported_leaves || 0);
            foreignLeaves += Number(subtree.foreign_leaves || 0);
            continue;
        }

        var mediaPath = __atrGetProjectItemMediaPath(child);
        if (mediaPath && __atrPathStartsWith(mediaPath, normalizedRootPath)) {
            report.imported_leaf_items.push(child);
            importedLeaves += 1;
        } else {
            foreignLeaves += 1;
        }
    }

    return {
        imported_leaves: importedLeaves,
        foreign_leaves: foreignLeaves
    };
}

function __atrBuildImportedCleanupScan(normalizedRootPath) {
    var report = {
        imported_leaf_items: [],
        deletable_bins: []
    };

    if (!app || !app.project || !app.project.rootItem) {
        return report;
    }

    __atrInspectImportedSubtree(app.project.rootItem, normalizedRootPath, 0, report);

    report.deletable_bins.sort(function (a, b) {
        return Number(b.depth || 0) - Number(a.depth || 0);
    });

    return report;
}

function __atrDeleteBinItem(projectItem) {
    if (!projectItem || !projectItem.deleteBin) {
        return false;
    }

    try {
        return !!projectItem.deleteBin();
    } catch (e) {
        return false;
    }
}

function __atrDeleteLeafProjectItem(projectItem) {
    if (!projectItem || !projectItem.select || !app || !app.project || !app.project.deleteSelection) {
        return false;
    }

    try {
        projectItem.select();
        return !!app.project.deleteSelection();
    } catch (e) {
        return false;
    }
}

function __atrDeleteImportedProjectItems(normalizedRootPath) {
    var binsDeleted = 0;
    var leafDeleted = 0;
    var failed = 0;
    var passesExecuted = 0;
    var fallbackBins = [];
    var scan = __atrBuildImportedCleanupScan(normalizedRootPath);

    while (passesExecuted < __atrCleanupMaxBinPasses && scan.deletable_bins.length > 0) {
        passesExecuted += 1;
        var passDeleted = 0;
        fallbackBins = scan.deletable_bins.slice(0);

        for (var i = 0; i < scan.deletable_bins.length; i += 1) {
            var binRecord = scan.deletable_bins[i];
            if (__atrDeleteBinItem(binRecord.item)) {
                binsDeleted += 1;
                passDeleted += 1;
            } else {
                failed += 1;
            }
        }

        if (passDeleted <= 0) {
            break;
        }

        scan = __atrBuildImportedCleanupScan(normalizedRootPath);
    }

    if (scan.imported_leaf_items.length > 0) {
        for (var leafIndex = scan.imported_leaf_items.length - 1; leafIndex >= 0; leafIndex -= 1) {
            var leafItem = scan.imported_leaf_items[leafIndex];
            if (__atrDeleteLeafProjectItem(leafItem)) {
                leafDeleted += 1;
            } else {
                failed += 1;
            }
        }

        if (fallbackBins.length > 0) {
            for (var retryIndex = 0; retryIndex < fallbackBins.length; retryIndex += 1) {
                if (__atrDeleteBinItem(fallbackBins[retryIndex].item)) {
                    binsDeleted += 1;
                }
            }
        }
    }

    scan = __atrBuildImportedCleanupScan(normalizedRootPath);

    return {
        ok: scan.imported_leaf_items.length === 0,
        bins_deleted: binsDeleted,
        leaf_items_deleted: leafDeleted,
        project_items_failed: failed,
        project_items_remaining: Number(scan.imported_leaf_items.length || 0),
        project_items_considered: Number(scan.imported_leaf_items.length || 0) + leafDeleted,
        passes_executed: passesExecuted
    };
}

function __atrRemoveImportedTimelineClips(normalizedRootPath) {
    var removed = 0;
    var failed = 0;
    var sequenceCount = 0;
    var sequences = app && app.project ? app.project.sequences : null;

    if (!sequences) {
        return {
            removed: 0,
            failed: 0,
            sequences: 0
        };
    }

    try {
        sequenceCount = Number(sequences.numSequences || 0);
    } catch (eSeq) {
        sequenceCount = 0;
    }

    for (var s = 0; s < sequenceCount; s += 1) {
        var sequence = sequences[s];
        if (!sequence) {
            continue;
        }

        var trackGroups = [
            sequence.videoTracks,
            sequence.audioTracks
        ];

        for (var tg = 0; tg < trackGroups.length; tg += 1) {
            var tracks = trackGroups[tg];
            if (!tracks) {
                continue;
            }

            var trackCount = 0;
            try {
                trackCount = Number(tracks.numTracks || 0);
            } catch (eTrackCount) {
                trackCount = 0;
            }

            for (var t = 0; t < trackCount; t += 1) {
                var track = tracks[t];
                if (!track || !track.clips) {
                    continue;
                }

                var clipCount = 0;
                try {
                    clipCount = Number(track.clips.numItems || 0);
                } catch (eClipCount) {
                    clipCount = 0;
                }

                for (var c = clipCount - 1; c >= 0; c -= 1) {
                    var clip = track.clips[c];
                    if (!clip) {
                        continue;
                    }

                    var clipMediaPath = __atrGetProjectItemMediaPath(clip.projectItem);
                    if (!clipMediaPath || !__atrPathStartsWith(clipMediaPath, normalizedRootPath)) {
                        continue;
                    }

                    try {
                        clip.remove(0, 1);
                        removed += 1;
                    } catch (eRemove) {
                        failed += 1;
                    }
                }
            }
        }
    }

    return {
        removed: removed,
        failed: failed,
        sequences: sequenceCount
    };
}

function __atrPushEncoderEvent(type, jobID, detail) {
    var meta = __atrEncoderJobMetaMap[jobID] || null;
    var event = {
        type: __atrSafeString(type),
        job_id: __atrSafeString(jobID),
        project_id: __atrSafeString((meta && meta.project_id) || __atrEncoderJobProjectMap[jobID] || ""),
        timestamp: (new Date()).toISOString ? (new Date()).toISOString() : "",
        detail: detail || {}
    };
    if (meta && meta.render_kind) {
        event.detail.render_kind = __atrSafeString(meta.render_kind);
    }
    __atrEncoderEvents.push(event);
}

function __atrRememberEncoderJob(jobID, projectId, renderKind, outputPath, presetPath) {
    __atrEncoderJobProjectMap[jobID] = __atrSafeString(projectId);
    __atrEncoderJobMetaMap[jobID] = {
        project_id: __atrSafeString(projectId),
        render_kind: __atrSafeString(renderKind || "video"),
        output_path: __atrSafeString(outputPath),
        preset_path: __atrSafeString(presetPath)
    };
}

function __atrForgetEncoderJob(jobID) {
    try {
        delete __atrEncoderJobProjectMap[jobID];
    } catch (e) {}
    try {
        delete __atrEncoderJobMetaMap[jobID];
    } catch (e2) {}
}

function __atrBindEncoderCallbacks() {
    if (__atrEncoderCallbacksBound) {
        return true;
    }

    if (!app || !app.encoder || !app.encoder.bind) {
        return false;
    }

    app.encoder.bind("onEncoderJobQueued", "ATR_onEncoderJobQueued");
    app.encoder.bind("onEncoderJobProgress", "ATR_onEncoderJobProgress");
    app.encoder.bind("onEncoderJobComplete", "ATR_onEncoderJobComplete");
    app.encoder.bind("onEncoderJobError", "ATR_onEncoderJobError");

    __atrEncoderCallbacksBound = true;
    return true;
}

function ATR_onEncoderJobQueued(jobID) {
    __atrPushEncoderEvent("queued", jobID, {});
}

function ATR_onEncoderJobProgress(jobID, progress) {
    var numericProgress = Number(progress);
    if (isNaN(numericProgress)) {
        numericProgress = -1;
    }
    __atrPushEncoderEvent("progress", jobID, {
        progress: numericProgress
    });
}

function ATR_onEncoderJobComplete(jobID, outputPath) {
    __atrPushEncoderEvent("complete", jobID, {
        output_path: __atrSafeString(outputPath)
    });
    __atrForgetEncoderJob(jobID);
}

function ATR_onEncoderJobError(jobID, errorDetail) {
    __atrPushEncoderEvent("error", jobID, {
        error: __atrSafeString(errorDetail)
    });
    __atrForgetEncoderJob(jobID);
}

function __atrEncodeSequence(sequence, outputFsPath, presetFsPath) {
    var jobID = null;
    try {
        jobID = app.encoder.encodeSequence(
            sequence,
            outputFsPath,
            presetFsPath,
            app.encoder.ENCODE_ENTIRE,
            1
        );
    } catch (eFive) {
        jobID = app.encoder.encodeSequence(
            sequence,
            outputFsPath,
            presetFsPath,
            app.encoder.ENCODE_ENTIRE
        );
    }

    if (!jobID && jobID !== 0) {
        throw new Error("encodeSequence returned an empty job ID");
    }

    return jobID;
}

function __atrRemoveAllTrackClips(tracks) {
    if (!tracks) {
        return;
    }
    var trackCount = 0;
    try {
        trackCount = Number(tracks.numTracks || 0);
    } catch (eCount) {
        trackCount = 0;
    }

    for (var t = 0; t < trackCount; t += 1) {
        var track = tracks[t];
        if (!track || !track.clips) {
            continue;
        }

        var clipCount = 0;
        try {
            clipCount = Number(track.clips.numItems || 0);
        } catch (eClipCount) {
            clipCount = 0;
        }

        for (var c = clipCount - 1; c >= 0; c -= 1) {
            try {
                track.clips[c].remove(0, 1);
            } catch (eRemove) {}
        }
    }
}

function __atrRemoveTrackByIndex(tracks, indexToRemove) {
    if (!tracks) {
        return;
    }
    var idx = Number(indexToRemove || 0);
    if (idx < 0) {
        return;
    }

    var track = tracks[idx];
    if (!track || !track.clips) {
        return;
    }

    var clipCount = 0;
    try {
        clipCount = Number(track.clips.numItems || 0);
    } catch (eClipCount) {
        clipCount = 0;
    }

    for (var c = clipCount - 1; c >= 0; c -= 1) {
        try {
            track.clips[c].remove(0, 1);
        } catch (eRemove) {}
    }
}

function __atrCloneActiveSequence() {
    var sequences = app && app.project ? app.project.sequences : null;
    if (!sequences) {
        throw new Error("No sequence collection available");
    }

    var beforeCount = 0;
    try {
        beforeCount = Number(sequences.numSequences || 0);
    } catch (eBefore) {
        beforeCount = 0;
    }

    var sourceSequence = app.project.activeSequence;
    if (!sourceSequence || !sourceSequence.clone) {
        throw new Error("Active sequence cannot be cloned");
    }

    var cloned = sourceSequence.clone();
    if (!cloned) {
        throw new Error("Sequence clone() returned false");
    }

    var afterCount = 0;
    try {
        afterCount = Number(sequences.numSequences || 0);
    } catch (eAfter) {
        afterCount = beforeCount;
    }

    if (afterCount <= beforeCount) {
        throw new Error("Clone did not create a new sequence");
    }

    var cloneSequence = sequences[afterCount - 1];
    if (!cloneSequence) {
        throw new Error("Unable to access cloned sequence");
    }

    return cloneSequence;
}

/**
 * Execute a .jsx script file with error handling.
 * @param {string} scriptPath - Absolute path to the .jsx file (forward slashes)
 * @returns {string} "OK" on success, "ERROR: ..." on failure
 */
function runScript(scriptPath) {
    try {
        var file = new File(scriptPath);
        if (!file.exists) {
            return "ERROR: File not found: " + scriptPath;
        }
        $.evalFile(file);
        return "OK";
    } catch (e) {
        return "ERROR: " + e.message + " (line " + e.line + ")";
    }
}

/**
 * Keep CEP panel alive across app lifecycle.
 */
function setPanelPersistent() {
    try {
        if (app && app.setExtensionPersistent) {
            app.setExtensionPersistent(ATR_EXTENSION_ID, 1);
            return "OK";
        }
        return "ERROR: app.setExtensionPersistent is unavailable";
    } catch (e) {
        return "ERROR: " + e.message;
    }
}

/**
 * Start managed export via Adobe Media Encoder and return job ID.
 *
 * @param {string} projectId
 * @param {string} outputPath
 * @param {string} presetPath
 * @returns {string} jobID or ERROR message
 */
function startManagedExport(projectId, outputPath, presetPath) {
    try {
        if (!app || !app.project || !app.project.activeSequence) {
            return "ERROR: No active sequence in current project";
        }

        var exportAudioNoMusic = Number(arguments.length >= 4 ? arguments[3] : 0) === 1;
        var audioOutputPath = arguments.length >= 5 ? __atrSafeString(arguments[4]) : "";
        var audioPresetPath = arguments.length >= 6 ? __atrSafeString(arguments[5]) : "";

        var normalizedPresetPath = __atrNormalizePath(presetPath);
        var presetFile = new File(normalizedPresetPath);
        if (!presetFile.exists) {
            return "ERROR: Preset file not found: " + normalizedPresetPath;
        }
        var presetFsPath = __atrSafeString(presetFile.fsName || normalizedPresetPath);

        var normalizedOutputPath = __atrNormalizePath(outputPath);
        var outFile = new File(normalizedOutputPath);
        var outFolder = outFile.parent;
        if (outFolder && !outFolder.exists) {
            if (!outFolder.create()) {
                return "ERROR: Unable to create output folder: " + outFolder.fsName;
            }
        }
        var outputFsPath = __atrSafeString(outFile.fsName || normalizedOutputPath);

        if (!app.encoder) {
            return "ERROR: app.encoder is unavailable";
        }

        app.encoder.launchEncoder();

        if (!__atrBindEncoderCallbacks()) {
            return "ERROR: Unable to bind AME encoder callbacks";
        }

        var videoJobID = __atrEncodeSequence(app.project.activeSequence, outputFsPath, presetFsPath);
        __atrRememberEncoderJob(videoJobID, projectId, "video", outputFsPath, presetFsPath);
        __atrPushEncoderEvent("queued", videoJobID, {
            output_path: outputFsPath,
            preset_path: presetFsPath,
            render_kind: "video"
        });

        var audioJobID = "";
        if (exportAudioNoMusic) {
            var normalizedAudioPresetPath = __atrNormalizePath(audioPresetPath || presetPath);
            var audioPresetFile = new File(normalizedAudioPresetPath);
            if (!audioPresetFile.exists) {
                return "ERROR: Audio preset file not found: " + normalizedAudioPresetPath;
            }
            var audioPresetFsPath = __atrSafeString(audioPresetFile.fsName || normalizedAudioPresetPath);

            var normalizedAudioOutputPath = __atrNormalizePath(audioOutputPath);
            if (!normalizedAudioOutputPath) {
                return "ERROR: Missing audio output path";
            }
            var audioOutFile = new File(normalizedAudioOutputPath);
            var audioOutFolder = audioOutFile.parent;
            if (audioOutFolder && !audioOutFolder.exists) {
                if (!audioOutFolder.create()) {
                    return "ERROR: Unable to create audio output folder: " + audioOutFolder.fsName;
                }
            }
            var audioOutputFsPath = __atrSafeString(audioOutFile.fsName || normalizedAudioOutputPath);

            var tempSequence = __atrCloneActiveSequence();
            __atrRemoveAllTrackClips(tempSequence.videoTracks);
            __atrRemoveTrackByIndex(tempSequence.audioTracks, 2);

            audioJobID = __atrEncodeSequence(tempSequence, audioOutputFsPath, audioPresetFsPath);
            __atrRememberEncoderJob(audioJobID, projectId, "audio_no_music", audioOutputFsPath, audioPresetFsPath);
            __atrPushEncoderEvent("queued", audioJobID, {
                output_path: audioOutputFsPath,
                preset_path: audioPresetFsPath,
                render_kind: "audio_no_music"
            });
        }

        if (JSON && JSON.stringify) {
            return JSON.stringify({
                video_job_id: __atrSafeString(videoJobID),
                audio_job_id: __atrSafeString(audioJobID),
                audio_enabled: !!exportAudioNoMusic
            });
        }
        return __atrSafeString(videoJobID);
    } catch (e) {
        return "ERROR: " + e.message + " (line " + e.line + ")";
    }
}

/**
 * Pull pending encoder events and flush queue.
 *
 * @returns {string} JSON array of events
 */
function pullEncoderEvents() {
    try {
        var events = __atrEncoderEvents.slice(0);
        __atrEncoderEvents = [];
        if (JSON && JSON.stringify) {
            return JSON.stringify(events);
        }
        return "[]";
    } catch (e) {
        return "[]";
    }
}

/**
 * Remove imported assets (project panel + timeline fallback) tied to a local project root.
 *
 * @param {string} localRootPath
 * @returns {string} JSON result or ERROR
 */
function cleanupImportedProjectMedia(localRootPath) {
    try {
        var normalizedRootPath = __atrNormalizeComparePath(localRootPath);
        if (!normalizedRootPath) {
            return "ERROR: Missing local root path";
        }

        var projectItems = __atrDeleteImportedProjectItems(normalizedRootPath);
        var timeline = __atrRemoveImportedTimelineClips(normalizedRootPath);
        var result = {
            ok: !!projectItems.ok,
            local_root: normalizedRootPath,
            bins_deleted: Number(projectItems.bins_deleted || 0),
            leaf_items_deleted: Number(projectItems.leaf_items_deleted || 0),
            project_items_failed: Number(projectItems.project_items_failed || 0),
            project_items_remaining: Number(projectItems.project_items_remaining || 0),
            project_items_considered: Number(projectItems.project_items_considered || 0),
            cleanup_passes_executed: Number(projectItems.passes_executed || 0),
            timeline_removed: Number(timeline.removed || 0),
            timeline_failed: Number(timeline.failed || 0)
        };

        if (!result.ok) {
            result.error = "Imported Premiere project items remain after cleanup";
        }

        if (JSON && JSON.stringify) {
            return JSON.stringify(result);
        }
        return "OK";
    } catch (e) {
        return "ERROR: " + e.message + " (line " + e.line + ")";
    }
}
