/**
 * JSX Runner - ExtendScript Host
 *
 * Runs in Premiere Pro's ExtendScript engine.
 * Called from the CEP panel via csInterface.evalScript().
 */

var ATR_EXTENSION_ID = "com.animetiktok.jsxrunner.panel";
var __atrEncoderEvents = [];
var __atrEncoderJobProjectMap = {};
var __atrEncoderCallbacksBound = false;

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

function __atrCollectImportedProjectItems(containerItem, normalizedRootPath, outItems) {
    if (!containerItem || !containerItem.children || !outItems) {
        return;
    }
    var count = 0;
    try {
        count = Number(containerItem.children.numItems || 0);
    } catch (eCount) {
        count = 0;
    }
    for (var i = 0; i < count; i += 1) {
        var child = containerItem.children[i];
        if (!child) {
            continue;
        }

        var mediaPath = __atrGetProjectItemMediaPath(child);
        if (mediaPath && __atrPathStartsWith(mediaPath, normalizedRootPath)) {
            outItems.push(child);
        }

        try {
            if (child.children && Number(child.children.numItems || 0) > 0) {
                __atrCollectImportedProjectItems(child, normalizedRootPath, outItems);
            }
        } catch (eChild) {}
    }
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

function __atrDeleteImportedProjectItems(normalizedRootPath) {
    var targets = [];
    var removed = 0;
    var failed = 0;

    if (!app || !app.project || !app.project.rootItem) {
        return {
            removed: 0,
            failed: 0,
            considered: 0
        };
    }

    __atrCollectImportedProjectItems(app.project.rootItem, normalizedRootPath, targets);

    for (var i = targets.length - 1; i >= 0; i -= 1) {
        var item = targets[i];
        if (!item) {
            continue;
        }

        var deleted = false;
        try {
            if (item.deleteBin) {
                deleted = !!item.deleteBin();
            }
        } catch (eDeleteBin) {
            deleted = false;
        }

        if (!deleted) {
            try {
                if (item.select && app.project.deleteSelection) {
                    item.select();
                    deleted = !!app.project.deleteSelection();
                }
            } catch (eDeleteSelection) {
                deleted = false;
            }
        }

        if (deleted) {
            removed += 1;
        } else {
            failed += 1;
        }
    }

    return {
        removed: removed,
        failed: failed,
        considered: targets.length
    };
}

function __atrPushEncoderEvent(type, jobID, detail) {
    var event = {
        type: __atrSafeString(type),
        job_id: __atrSafeString(jobID),
        project_id: __atrSafeString(__atrEncoderJobProjectMap[jobID] || ""),
        timestamp: (new Date()).toISOString ? (new Date()).toISOString() : "",
        detail: detail || {}
    };
    __atrEncoderEvents.push(event);
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
    try {
        delete __atrEncoderJobProjectMap[jobID];
    } catch (e) {}
}

function ATR_onEncoderJobError(jobID, errorDetail) {
    __atrPushEncoderEvent("error", jobID, {
        error: __atrSafeString(errorDetail)
    });
    try {
        delete __atrEncoderJobProjectMap[jobID];
    } catch (e) {}
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

        var jobID = null;
        try {
            jobID = app.encoder.encodeSequence(
                app.project.activeSequence,
                outputFsPath,
                presetFsPath,
                app.encoder.ENCODE_ENTIRE,
                1
            );
        } catch (eFive) {
            // Some builds expose a 4-argument signature.
            try {
                jobID = app.encoder.encodeSequence(
                    app.project.activeSequence,
                    outputFsPath,
                    presetFsPath,
                    app.encoder.ENCODE_ENTIRE
                );
            } catch (eFour) {
                return "ERROR: encodeSequence failed (5-arg: " + __atrSafeString(eFive.message) + "; 4-arg: " + __atrSafeString(eFour.message) + ")";
            }
        }

        if (!jobID && jobID !== 0) {
            return "ERROR: encodeSequence returned an empty job ID";
        }

        __atrEncoderJobProjectMap[jobID] = __atrSafeString(projectId);
        __atrPushEncoderEvent("queued", jobID, {
            output_path: outputFsPath,
            preset_path: presetFsPath
        });

        return __atrSafeString(jobID);
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
 * Remove imported assets (timeline + project panel) tied to a local project root.
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

        var timeline = __atrRemoveImportedTimelineClips(normalizedRootPath);
        var projectItems = __atrDeleteImportedProjectItems(normalizedRootPath);

        var result = {
            ok: true,
            local_root: normalizedRootPath,
            timeline_removed: Number(timeline.removed || 0),
            timeline_failed: Number(timeline.failed || 0),
            project_items_removed: Number(projectItems.removed || 0),
            project_items_failed: Number(projectItems.failed || 0),
            project_items_considered: Number(projectItems.considered || 0)
        };

        if (JSON && JSON.stringify) {
            return JSON.stringify(result);
        }
        return "OK";
    } catch (e) {
        return "ERROR: " + e.message + " (line " + e.line + ")";
    }
}
