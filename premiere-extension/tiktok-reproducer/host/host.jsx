/**
 * Tiktok Reproducer - ExtendScript Host
 *
 * Runs in Premiere Pro's ExtendScript engine.
 * Called from the CEP panel via csInterface.evalScript().
 */

var ATR_EXTENSION_ID = "com.animetiktok.tiktokreproducer.panel";
// Must stay in sync with ATR_BUILD_ID in client/constants.js.
var ATR_HOST_BUILD_ID = "2026-08-29-close-first-cleanup-v21";
// Separates runScript()'s status from the non-fatal warnings the executed
// script published. Must stay in sync with HOST_RUN_WARNING_SEPARATOR in
// client/main.js.
var ATR_RUN_WARNING_SEPARATOR = "::ATR_WARN::";
var __atrEncoderEvents = [];
var __atrEncoderJobProjectMap = {};
var __atrEncoderJobMetaMap = {};
var __atrTempAudioSequenceByJob = {};
var __atrProxyAttachAttemptMap = {};
var __atrImportedProjectRefs = [];
var __atrCleanupClosingProjectIdentities = [];
var __atrManagedExportTargetsByProjectId = {};
var __atrEncoderCallbacksBound = false;
var __atrTempAudioSequencePrefix = "ATR_AUDIO_NO_MUSIC_TMP__";
var __atrProxyOutputSuffix = "__atr_proxy.mp4";
var __atrProxyRepairOutputSuffix = "__atr_proxy_projectitem.mp4";

// Cleanup closes the now-empty ATR project to release Premiere's internal
// media/proxy records. Keep the panel host alive while no project is open.
try {
  if (app && app.setExtensionPersistent) {
    app.setExtensionPersistent(ATR_EXTENSION_ID, 1);
  }
} catch (ePersistentExtension) {}

/**
 * JSON.stringify polyfill for ExtendScript (ES3).
 * ExtendScript lacks native JSON support in some Premiere Pro versions.
 * Based on Douglas Crockford's JSON2 (public domain).
 */
if (typeof JSON === "undefined") {
  JSON = {};
}
if (typeof JSON.stringify !== "function") {
  JSON.stringify = function (value) {
    var type = typeof value;
    if (type === "string") {
      return '"' + value.replace(/[\\\"\x00-\x1f]/g, function (c) {
        var hex = c.charCodeAt(0).toString(16);
        return c === '"' ? '\\"' : c === "\\" ? "\\\\" : "\\u" + ("0000" + hex).slice(-4);
      }) + '"';
    }
    if (type === "number" || type === "boolean") {
      return String(value);
    }
    if (value === null) {
      return "null";
    }
    if (value instanceof Array) {
      var arrResult = [];
      for (var i = 0; i < value.length; i++) {
        arrResult.push(JSON.stringify(value[i]));
      }
      return "[" + arrResult.join(",") + "]";
    }
    if (type === "object") {
      var objResult = [];
      for (var key in value) {
        if (value.hasOwnProperty(key)) {
          objResult.push(JSON.stringify(key) + ":" + JSON.stringify(value[key]));
        }
      }
      return "{" + objResult.join(",") + "}";
    }
    return "null";
  };
}
if (typeof JSON.parse !== "function") {
  JSON.parse = function (text) {
    return eval("(" + String(text || "") + ")");
  };
}

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
  var normalized = __atrNormalizePath(value);
  try {
    normalized = decodeURI(normalized);
  } catch (eDecode) {}
  normalized = normalized.toLowerCase();
  normalized = normalized.replace(/\/+/g, "/");
  normalized = normalized.replace(/^file:\/+/, "");
  if (/^\/[a-z]:/.test(normalized)) {
    normalized = normalized.substring(1);
  }
  if (
    normalized.length > 1 &&
    normalized.charAt(normalized.length - 1) === "/"
  ) {
    normalized = normalized.substring(0, normalized.length - 1);
  }
  return normalized;
}

function __atrPathStartsWith(pathValue, rootValue) {
  var normalizedPath = __atrNormalizeComparePath(pathValue);
  var normalizedRoot = __atrNormalizeComparePath(rootValue);
  if (!normalizedPath || !normalizedRoot) {
    return false;
  }
  if (normalizedPath === normalizedRoot) {
    return true;
  }
  return normalizedPath.indexOf(normalizedRoot + "/") === 0;
}

function __atrTrimTrailingSlash(pathValue) {
  var normalized = __atrNormalizePath(pathValue);
  if (
    normalized.length > 1 &&
    normalized.charAt(normalized.length - 1) === "/"
  ) {
    return normalized.substring(0, normalized.length - 1);
  }
  return normalized;
}

function __atrJoinPath(basePath, relativePath) {
  var left = __atrTrimTrailingSlash(basePath);
  var right = __atrNormalizePath(relativePath).replace(/^\/+/, "");
  if (!left) {
    return right;
  }
  if (!right) {
    return left;
  }
  return left + "/" + right;
}

function __atrGetParentPath(filePath) {
  var normalized = __atrTrimTrailingSlash(filePath);
  var slash = normalized.lastIndexOf("/");
  if (slash <= 0) {
    return normalized;
  }
  return normalized.substring(0, slash);
}

function __atrGetBasename(filePath) {
  var normalized = __atrTrimTrailingSlash(filePath);
  var slash = normalized.lastIndexOf("/");
  if (slash === -1) {
    return normalized;
  }
  return normalized.substring(slash + 1);
}

function __atrStripExtension(fileName) {
  var value = __atrSafeString(fileName);
  var dot = value.lastIndexOf(".");
  if (dot <= 0) {
    return value;
  }
  return value.substring(0, dot);
}

function __atrWalkProjectItems(containerItem, visitor) {
  var childCount = __atrGetProjectItemChildCount(containerItem);
  for (var i = 0; i < childCount; i += 1) {
    var child = containerItem.children[i];
    if (!child) {
      continue;
    }
    visitor(child);
    if (__atrGetProjectItemChildCount(child) > 0) {
      __atrWalkProjectItems(child, visitor);
    }
  }
}

function __atrGetProjectIdentity(projectObject) {
  if (!projectObject) {
    return "";
  }
  var documentId = "";
  var projectPath = "";
  var projectName = "";
  try {
    documentId = __atrSafeString(projectObject.documentID || "");
  } catch (eDocumentId) {}
  try {
    projectPath = __atrNormalizeComparePath(projectObject.path || "");
  } catch (eProjectPath) {}
  try {
    projectName = __atrSafeString(projectObject.name || "");
  } catch (eProjectName) {}
  return documentId || projectPath || projectName;
}

function __atrGetProjectObjectScore(projectObject) {
  if (!projectObject) {
    return -1;
  }

  var score = 0;
  try {
    if (projectObject.rootItem) {
      score += 1000;
      score += __atrGetProjectItemChildCount(projectObject.rootItem) * 100;
    }
  } catch (eRootItem) {}
  try {
    if (projectObject.sequences) {
      score += 1000;
      score += Number(projectObject.sequences.numSequences || 0) * 1000;
    }
  } catch (eSequences) {}
  try {
    if (projectObject.activeSequence) {
      score += 10000;
    }
  } catch (eActiveSequence) {}
  try {
    if (projectObject.path) {
      score += 10;
    }
  } catch (ePath) {}
  try {
    if (projectObject.name) {
      score += 1;
    }
  } catch (eName) {}
  return score;
}

function __atrPushUniqueProject(projects, projectObject) {
  if (!projectObject) {
    return;
  }
  var candidateIdentity = __atrGetProjectIdentity(projectObject);
  for (var i = 0; i < projects.length; i += 1) {
    try {
      if (projects[i] === projectObject) {
        return;
      }
    } catch (eSameProject) {}
    var existingIdentity = __atrGetProjectIdentity(projects[i]);
    if (
      candidateIdentity &&
      existingIdentity &&
      candidateIdentity === existingIdentity
    ) {
      // Premiere can retain multiple ExtendScript wrappers for a project that
      // has been closed and reopened at the same path. documentID/path are
      // project identities, not wrapper-instance identities. Keep the wrapper
      // that exposes the richest current project state instead of whichever
      // stale wrapper app.projects happened to return first.
      if (
        __atrGetProjectObjectScore(projectObject) >
        __atrGetProjectObjectScore(projects[i])
      ) {
        projects[i] = projectObject;
      }
      return;
    }
  }
  projects.push(projectObject);
}

function __atrGetOpenProjects() {
  var projects = [];
  // Prefer the front Project object. app.projects may still expose an older
  // same-identity wrapper after the scratch project is recycled.
  try {
    if (app && app.project) {
      __atrPushUniqueProject(projects, app.project);
    }
  } catch (eActiveProject) {}

  // Project view IDs provide a second, documented route to the Project
  // objects currently represented in Premiere's Project panels.
  try {
    if (app && app.getProjectViewIDs && app.getProjectFromViewID) {
      var viewIds = app.getProjectViewIDs();
      var viewCount = Number(viewIds && viewIds.length || 0);
      for (var viewIndex = 0; viewIndex < viewCount; viewIndex += 1) {
        var viewProject = app.getProjectFromViewID(viewIds[viewIndex]);
        if (viewProject) {
          __atrPushUniqueProject(projects, viewProject);
        }
      }
    }
  } catch (eProjectViews) {}

  try {
    if (app && app.projects) {
      var count = Number(app.projects.numProjects || app.projects.length || 0);
      for (var i = 0; i < count; i += 1) {
        __atrPushUniqueProject(projects, app.projects[i]);
      }
    }
  } catch (eProjects) {}

  return projects;
}

function __atrProjectIsOpen(projectObject, openProjects) {
  if (!projectObject) {
    return false;
  }
  var projects = openProjects || __atrGetOpenProjects();
  var identity = __atrGetProjectIdentity(projectObject);
  for (var i = 0; i < projects.length; i += 1) {
    try {
      if (projects[i] === projectObject) {
        return true;
      }
    } catch (eSameProject) {}
    if (identity && identity === __atrGetProjectIdentity(projects[i])) {
      return true;
    }
  }
  return false;
}

function __atrRememberImportedProject(projectObject) {
  if (!projectObject) {
    return;
  }
  __atrPushUniqueProject(__atrImportedProjectRefs, projectObject);
}

function __atrPushUniqueProjectItem(items, seen, item) {
  if (!item) {
    return;
  }
  var key = "";
  try {
    key = __atrSafeString(item.nodeId || "");
  } catch (eNode) {
    key = "";
  }
  if (!key) {
    key =
      __atrGetProjectItemMediaPath(item) +
      "::" +
      __atrSafeString(item.name || "");
  }
  if (!key || seen[key]) {
    return;
  }
  seen[key] = true;
  items.push(item);
}

function __atrGetProjectItemName(projectItem) {
  try {
    return __atrSafeString(projectItem && projectItem.name ? projectItem.name : "");
  } catch (eName) {
    return "";
  }
}

function __atrIsAtrRawAudioProjectItem(projectItem) {
  var itemName = __atrGetProjectItemName(projectItem);
  return itemName.indexOf("__ATR_RAW_AUDIO__") === 0;
}

function __atrProjectItemCanAcceptProxy(projectItem) {
  try {
    return !!(projectItem && projectItem.canProxy && projectItem.canProxy());
  } catch (eCanProxy) {
    return false;
  }
}

function __atrNameMatchesMediaPath(nameValue, mediaPath) {
  // Exact (optionally extension-stripped) basename equality only. The old
  // substring match could attach a proxy to the wrong item whenever one
  // source's basename was contained in another item's name.
  var itemName = __atrSafeString(nameValue).toLowerCase();
  var baseName = __atrGetBasename(mediaPath).toLowerCase();
  var strippedBaseName = __atrStripExtension(baseName).toLowerCase();
  var strippedItemName = __atrStripExtension(itemName).toLowerCase();
  return !!(
    itemName &&
    baseName &&
    (itemName === baseName || strippedItemName === strippedBaseName)
  );
}

function __atrGetTrackCollectionCount(trackCollection) {
  try {
    return Number(trackCollection ? trackCollection.numTracks || trackCollection.length || 0 : 0);
  } catch (eTrackCount) {
    return 0;
  }
}

function __atrGetTrackClipCount(track) {
  try {
    return Number(track && track.clips ? track.clips.numItems || track.clips.length || 0 : 0);
  } catch (eClipCount) {
    return 0;
  }
}

function __atrGetTrackItemProjectItem(trackItem) {
  try {
    if (trackItem && trackItem.projectItem) {
      return trackItem.projectItem;
    }
  } catch (eProjectItem) {}
  return null;
}

// One walk of the project tree plus one pass over the timelines, reusable
// across every media-path lookup in a reconcile invocation. Previously each
// lookup re-walked the whole project (O(targets x project size)).
function __atrBuildMediaPathLeafIndex() {
  var index = {
    leaves: [], // { item, name, media_path } for leaf project items
    timeline: [], // { item, name, clip_name, media_path } from video tracks
  };

  var root = app && app.project ? app.project.rootItem : null;
  if (root) {
    __atrWalkProjectItems(root, function (item) {
      if (__atrGetProjectItemChildCount(item) > 0) {
        return;
      }
      index.leaves.push({
        item: item,
        name: __atrGetProjectItemName(item),
        media_path: __atrGetProjectItemMediaPath(item),
      });
    });
  }

  var sequenceCount = __atrGetSequenceCount();
  for (var s = 0; s < sequenceCount; s += 1) {
    var sequence = null;
    try {
      sequence = app.project.sequences[s];
    } catch (eSequence) {
      sequence = null;
    }
    if (!sequence || !sequence.videoTracks) {
      continue;
    }
    var trackCount = __atrGetTrackCollectionCount(sequence.videoTracks);
    for (var t = 0; t < trackCount; t += 1) {
      var track = null;
      try {
        track = sequence.videoTracks[t];
      } catch (eTrack) {
        track = null;
      }
      if (!track || !track.clips) {
        continue;
      }
      var clipCount = __atrGetTrackClipCount(track);
      for (var c = 0; c < clipCount; c += 1) {
        var clip = null;
        try {
          clip = track.clips[c];
        } catch (eClip) {
          clip = null;
        }
        var projectItem = __atrGetTrackItemProjectItem(clip);
        if (!projectItem) {
          continue;
        }
        var clipName = "";
        try {
          clipName = __atrSafeString(clip && clip.name ? clip.name : "");
        } catch (eClipName) {
          clipName = "";
        }
        index.timeline.push({
          item: projectItem,
          name: __atrGetProjectItemName(projectItem),
          clip_name: clipName,
          media_path: __atrGetProjectItemMediaPath(projectItem),
        });
      }
    }
  }

  return index;
}

function __atrCollectProjectItemCandidateNames(items, limit) {
  var names = [];
  var maxNames = Math.max(0, Number(limit || 0));
  for (var i = 0; i < items.length && names.length < maxNames; i += 1) {
    var itemName = __atrGetProjectItemName(items[i]);
    if (itemName) {
      names.push(itemName);
    }
  }
  return names.join(", ");
}

function __atrFindProjectItemsByMediaPath(mediaPath, existingIndex) {
  var root = app && app.project ? app.project.rootItem : null;
  var normalizedMediaPath = __atrNormalizePath(mediaPath);
  var wanted = __atrNormalizeComparePath(normalizedMediaPath);
  var found = [];
  var seen = {};
  if (!root || !wanted) {
    return found;
  }

  var index = existingIndex || __atrBuildMediaPathLeafIndex();

  var candidatePaths = [];
  candidatePaths.push(normalizedMediaPath);
  try {
    var mediaFile = new File(normalizedMediaPath);
    var fsName = __atrSafeString(mediaFile.fsName || "");
    if (fsName) {
      candidatePaths.push(fsName);
    }
  } catch (eFile) {}

  if (root.findItemsMatchingMediaPath) {
    for (var i = 0; i < candidatePaths.length; i += 1) {
      var candidate = __atrSafeString(candidatePaths[i]);
      if (!candidate) {
        continue;
      }
      try {
        var matches = root.findItemsMatchingMediaPath(candidate, 1);
        var matchCount = 0;
        try {
          matchCount = Number(matches && (matches.numItems || matches.length) || 0);
        } catch (eMatchCount) {
          matchCount = 0;
        }
        if (matches && matchCount > 0) {
          for (var j = 0; j < matchCount; j += 1) {
            __atrPushUniqueProjectItem(found, seen, matches[j]);
          }
        }
      } catch (eFind) {}
    }
  }

  var e;
  for (e = 0; e < index.leaves.length; e += 1) {
    if (index.leaves[e].media_path === wanted) {
      __atrPushUniqueProjectItem(found, seen, index.leaves[e].item);
    }
  }
  for (e = 0; e < index.timeline.length; e += 1) {
    if (index.timeline[e].media_path === wanted) {
      __atrPushUniqueProjectItem(found, seen, index.timeline[e].item);
    }
  }

  // Exact media-path matches win outright; name-based matching is only a
  // last resort for items whose media path is unavailable (offline media),
  // and now requires exact basename equality.
  if (found.length > 0) {
    return found;
  }

  for (e = 0; e < index.leaves.length; e += 1) {
    if (__atrNameMatchesMediaPath(index.leaves[e].name, normalizedMediaPath)) {
      __atrPushUniqueProjectItem(found, seen, index.leaves[e].item);
    }
  }
  for (e = 0; e < index.timeline.length; e += 1) {
    if (
      __atrNameMatchesMediaPath(index.timeline[e].name, normalizedMediaPath) ||
      __atrNameMatchesMediaPath(
        index.timeline[e].clip_name,
        normalizedMediaPath,
      )
    ) {
      __atrPushUniqueProjectItem(found, seen, index.timeline[e].item);
    }
  }

  return found;
}

function __atrNormalizeProxyTarget(rawTarget) {
  var mediaPath = __atrNormalizePath(rawTarget && rawTarget.media_path);
  if (!mediaPath) {
    return null;
  }
  return {
    media_path: mediaPath,
    relative_source_path: __atrNormalizePath(
      rawTarget && rawTarget.relative_source_path,
    ),
    source_codec: __atrSafeString(rawTarget && rawTarget.source_codec).toLowerCase(),
    source_width: Math.max(0, Number(rawTarget && rawTarget.source_width || 0)),
    source_height: Math.max(0, Number(rawTarget && rawTarget.source_height || 0)),
    source_fps: Number(rawTarget && rawTarget.source_fps || 0),
    source_audio_codec: __atrSafeString(
      rawTarget && rawTarget.source_audio_codec,
    ).toLowerCase(),
    source_audio_channels: Math.max(
      0,
      Number(rawTarget && rawTarget.source_audio_channels || 0),
    ),
    source_audio_sample_rate: Math.max(
      0,
      Number(rawTarget && rawTarget.source_audio_sample_rate || 0),
    ),
    source_audio_stream_count: Math.max(
      0,
      Number(rawTarget && rawTarget.source_audio_stream_count || 0),
    ),
    expected_proxy_width: Math.max(
      0,
      Number(rawTarget && rawTarget.expected_proxy_width || 0),
    ),
    expected_proxy_height: Math.max(
      0,
      Number(rawTarget && rawTarget.expected_proxy_height || 0),
    ),
    needs_proxy: !!(rawTarget && rawTarget.needs_proxy),
  };
}

function __atrParseProxyPlan(proxyPlanJson) {
  var parsed = null;
  if (!proxyPlanJson) {
    return {
      enabled: false,
      targets: [],
    };
  }
  try {
    parsed =
      typeof proxyPlanJson === "string" ? JSON.parse(proxyPlanJson) : proxyPlanJson;
  } catch (e) {
    parsed = null;
  }
  if (!parsed) {
    return {
      enabled: false,
      targets: [],
    };
  }
  var plan = {
    enabled: !!parsed.enabled,
    project_id: __atrSafeString(parsed.project_id),
    auto_enable_proxy_view: !!parsed.auto_enable_proxy_view,
    required_codec: __atrSafeString(parsed.required_codec).toLowerCase(),
    required_scale_divisor: Math.max(
      1,
      Number(parsed.required_scale_divisor || 4),
    ),
    targets: [],
  };
  if (parsed.ffprobe_warning) {
    plan.ffprobe_warning = __atrSafeString(parsed.ffprobe_warning);
  }
  if (parsed.targets && parsed.targets.length) {
    for (var i = 0; i < parsed.targets.length; i += 1) {
      var normalizedTarget = __atrNormalizeProxyTarget(parsed.targets[i]);
      if (normalizedTarget) {
        plan.targets.push(normalizedTarget);
      }
    }
  }
  return plan;
}

function __atrLooksLikeManagedProxyPath(proxyPath, localRootPath) {
  var normalizedProxy = __atrNormalizeComparePath(proxyPath);
  var normalizedRoot = __atrNormalizeComparePath(localRootPath);
  var proxyName = __atrGetBasename(normalizedProxy);
  return !!(
    normalizedProxy &&
    normalizedRoot &&
    __atrPathStartsWith(normalizedProxy, normalizedRoot) &&
    normalizedProxy.indexOf("/proxies/") !== -1 &&
    (proxyName.indexOf(__atrProxyOutputSuffix.toLowerCase()) !== -1 ||
      proxyName.indexOf(__atrProxyRepairOutputSuffix.toLowerCase()) !== -1 ||
      proxyName.indexOf("__atr_proxy") !== -1)
  );
}

function __atrComputeManagedProxyOutputPath(localRootPath, target) {
  var rootPath = __atrTrimTrailingSlash(localRootPath);
  var relativeSourcePath = __atrNormalizePath(target && target.relative_source_path);
  var sourceFileName = __atrGetBasename(relativeSourcePath || target.media_path);
  var baseName = __atrStripExtension(sourceFileName);
  var parentRelative = __atrGetParentPath(relativeSourcePath);
  var relativeProxyDir = "proxies";
  if (parentRelative && parentRelative !== "." && parentRelative !== "/") {
    relativeProxyDir = __atrJoinPath(relativeProxyDir, parentRelative);
  }
  return __atrJoinPath(
    rootPath,
    __atrJoinPath(relativeProxyDir, baseName + __atrProxyOutputSuffix),
  );
}

function __atrComputeManagedProxyRepairOutputPath(localRootPath, target) {
  var rootPath = __atrTrimTrailingSlash(localRootPath);
  var relativeSourcePath = __atrNormalizePath(target && target.relative_source_path);
  var sourceFileName = __atrGetBasename(relativeSourcePath || target.media_path);
  var baseName = __atrStripExtension(sourceFileName);
  var parentRelative = __atrGetParentPath(relativeSourcePath);
  var relativeProxyDir = "proxies";
  if (parentRelative && parentRelative !== "." && parentRelative !== "/") {
    relativeProxyDir = __atrJoinPath(relativeProxyDir, parentRelative);
  }
  return __atrJoinPath(
    rootPath,
    __atrJoinPath(relativeProxyDir, baseName + __atrProxyRepairOutputSuffix),
  );
}

function __atrIsSuccessfulAttachResult(result) {
  return result === 0 || result === true || result === "0";
}

function __atrBuildProjectItemStableKey(projectItem) {
  var key = "";
  try {
    key = __atrSafeString(projectItem && projectItem.nodeId);
  } catch (eNode) {
    key = "";
  }
  if (key) {
    return key;
  }
  return (
    __atrNormalizeComparePath(__atrGetProjectItemMediaPath(projectItem)) +
    "::" +
    __atrSafeString(__atrGetProjectItemName(projectItem))
  );
}

function __atrBuildProxyAttachAttemptKey(projectItem, proxyPath) {
  return (
    __atrBuildProjectItemStableKey(projectItem) +
    "::" +
    __atrNormalizeComparePath(proxyPath)
  );
}

function __atrRememberProxyAttachAttempt(projectItem, proxyPath) {
  var key = __atrBuildProxyAttachAttemptKey(projectItem, proxyPath);
  if (!key) {
    return;
  }
  __atrProxyAttachAttemptMap[key] = new Date().getTime();
}

function __atrProxyAttachAttemptIsCoolingDown(projectItem, proxyPath) {
  var key = __atrBuildProxyAttachAttemptKey(projectItem, proxyPath);
  var attemptedAt = key ? Number(__atrProxyAttachAttemptMap[key] || 0) : 0;
  if (!attemptedAt) {
    return false;
  }
  return new Date().getTime() - attemptedAt < 30000;
}

function __atrProjectItemProxyMatchState(projectItem, proxyPath) {
  if (!projectItem || !proxyPath) {
    return 0;
  }
  if (!projectItem.hasProxy || !projectItem.getProxyPath) {
    return -1;
  }
  try {
    if (!projectItem.hasProxy()) {
      return 0;
    }
    var currentRawPath = projectItem.getProxyPath();
    var currentProxyPath = __atrNormalizeComparePath(currentRawPath);
    var expectedProxyPath = __atrNormalizeComparePath(proxyPath);
    if (currentProxyPath && expectedProxyPath && currentProxyPath === expectedProxyPath) {
      return 1;
    }
    try {
      var proxyFile = new File(proxyPath);
      var proxyFsPath = __atrNormalizeComparePath(proxyFile.fsName || "");
      if (proxyFsPath && currentProxyPath === proxyFsPath) {
        return 1;
      }
    } catch (eFile) {}
    try {
      var currentFile = new File(currentRawPath);
      var currentFsPath = __atrNormalizeComparePath(currentFile.fsName || "");
      if (currentFsPath && expectedProxyPath && currentFsPath === expectedProxyPath) {
        return 1;
      }
    } catch (eCurrentFile) {}
    return 0;
  } catch (eProxyPath) {
    return -1;
  }
}

function __atrTryAttachProxy(projectItem, proxyPath) {
  var response = {
    ok: false,
    pending: false,
    error: "",
    attached_path: "",
    last_result: "",
    attempted: false,
  };
  if (!projectItem || !proxyPath) {
    response.error = "Missing project item or proxy path";
    return response;
  }

  var normalizedProxyPath = __atrNormalizePath(proxyPath);
  var proxyFile = new File(normalizedProxyPath);
  var candidatePaths = [];
  var fsPath = __atrSafeString(proxyFile.fsName || "");
  if (fsPath) {
    candidatePaths.push(fsPath);
  }
  if (normalizedProxyPath) {
    candidatePaths.push(normalizedProxyPath);
    candidatePaths.push(normalizedProxyPath.replace(/\//g, "\\"));
  }

  var seen = {};
  for (var i = 0; i < candidatePaths.length; i += 1) {
    var candidate = __atrSafeString(candidatePaths[i]);
    if (!candidate || seen[candidate]) {
      continue;
    }
    seen[candidate] = true;

    if (__atrProjectItemProxyMatchState(projectItem, candidate) === 1) {
      response.ok = true;
      response.attached_path = candidate;
      return response;
    }

    if (__atrProxyAttachAttemptIsCoolingDown(projectItem, candidate)) {
      response.pending = true;
      response.error = "Proxy attach is settling in Premiere";
      continue;
    }

    try {
      if (projectItem.refreshMedia) {
        projectItem.refreshMedia();
      }
    } catch (eRefresh) {}

    try {
      response.attempted = true;
      __atrRememberProxyAttachAttempt(projectItem, candidate);
      var attachResult = projectItem.attachProxy(candidate, 0);
      response.last_result = __atrSafeString(attachResult);
      for (var verifyAttempt = 0; verifyAttempt < 4; verifyAttempt += 1) {
        try {
          if (projectItem.refreshMedia) {
            projectItem.refreshMedia();
          }
        } catch (eRefreshAfterAttach) {}
        if (verifyAttempt > 0) {
          try {
            $.sleep(250);
          } catch (eSleepVerify) {}
        }
        var matchState = __atrProjectItemProxyMatchState(projectItem, candidate);
        if (matchState === 1) {
          response.ok = true;
          response.attached_path = candidate;
          return response;
        }
      }
      if (__atrIsSuccessfulAttachResult(attachResult)) {
        response.ok = true;
        response.attached_path = candidate;
        return response;
      }
      if (proxyFile.exists) {
        response.pending = true;
        response.error = "attachProxy did not verify yet";
        continue;
      }
      response.error =
        "attachProxy returned " +
        (response.last_result ? response.last_result : "empty/undefined") +
        " and proxy path did not verify";
    } catch (eAttachCandidate) {
      response.error = __atrSafeString(
        eAttachCandidate.message || eAttachCandidate,
      );
    }
  }

  if (!response.error) {
    response.error = "attachProxy returned false";
  }
  return response;
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

function __atrInspectImportedSubtree(
  containerItem,
  normalizedRootPath,
  depth,
  report,
  includeProxyReferences,
) {
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
      var subtree = __atrInspectImportedSubtree(
        child,
        normalizedRootPath,
        depth + 1,
        report,
        includeProxyReferences,
      );
      if (subtree.imported_leaves > 0 && subtree.foreign_leaves === 0) {
        report.deletable_bins.push({
          item: child,
          depth: depth + 1,
        });
      }
      importedLeaves += Number(subtree.imported_leaves || 0);
      foreignLeaves += Number(subtree.foreign_leaves || 0);
      continue;
    }

    var mediaPath = __atrGetProjectItemMediaPath(child);
    var proxyPath = "";
    if (includeProxyReferences && child.hasProxy && child.getProxyPath) {
      try {
        if (child.hasProxy()) {
          proxyPath = __atrSafeString(child.getProxyPath());
        }
      } catch (eProxyPath) {}
    }
    if (
      (mediaPath && __atrPathStartsWith(mediaPath, normalizedRootPath)) ||
      (proxyPath && __atrPathStartsWith(proxyPath, normalizedRootPath))
    ) {
      report.imported_leaf_items.push(child);
      importedLeaves += 1;
    } else {
      foreignLeaves += 1;
    }
  }

  return {
    imported_leaves: importedLeaves,
    foreign_leaves: foreignLeaves,
  };
}

function __atrBuildImportedCleanupScan(
  normalizedRootPath,
  projectObject,
  includeProxyReferences,
) {
  var report = {
    imported_leaf_items: [],
    deletable_bins: [],
  };

  var targetProject = projectObject || (app && app.project ? app.project : null);
  if (!targetProject || !targetProject.rootItem) {
    return report;
  }

  __atrInspectImportedSubtree(
    targetProject.rootItem,
    normalizedRootPath,
    0,
    report,
    !!includeProxyReferences,
  );

  report.deletable_bins.sort(function (a, b) {
    return Number(b.depth || 0) - Number(a.depth || 0);
  });

  return report;
}

function __atrProjectReferencesAnyCleanupRoot(projectObject, normalizedRoots) {
  if (!projectObject || !projectObject.rootItem) {
    return false;
  }
  for (var i = 0; i < normalizedRoots.length; i += 1) {
    var scan = __atrBuildImportedCleanupScan(
      normalizedRoots[i],
      projectObject,
      true,
    );
    if (scan.imported_leaf_items.length > 0) {
      return true;
    }
  }
  return false;
}

function __atrResolveCleanupTargetProjects(normalizedRoots) {
  if (!normalizedRoots || normalizedRoots.length === 0) {
    return [];
  }
  var openProjects = __atrGetOpenProjects();
  var resolved = [];

  for (var i = 0; i < openProjects.length; i += 1) {
    if (__atrProjectReferencesAnyCleanupRoot(openProjects[i], normalizedRoots)) {
      __atrPushUniqueProject(resolved, openProjects[i]);
    }
  }

  // runScript() records the owning project before and after every import. Use
  // that as a fallback only when it identifies one unambiguous open project.
  // Reusing every historical import here could close an unrelated project
  // during a later disk-only cleanup retry.
  if (resolved.length === 0) {
    var openImportedProjects = [];
    for (
      var imported = 0;
      imported < __atrImportedProjectRefs.length;
      imported += 1
    ) {
      if (__atrProjectIsOpen(__atrImportedProjectRefs[imported], openProjects)) {
        __atrPushUniqueProject(
          openImportedProjects,
          __atrImportedProjectRefs[imported],
        );
      }
    }
    if (openImportedProjects.length === 1) {
      __atrPushUniqueProject(resolved, openImportedProjects[0]);
    }
  }

  return resolved;
}

function __atrInspectOpenProjectCleanupReferences(normalizedRoots) {
  var report = {
    ok: true,
    open_project_count: 0,
    referencing_project_count: 0,
    remaining_media_references: 0,
    remaining_proxy_references: 0,
    samples: [],
  };
  var openProjects = __atrGetOpenProjects();
  report.open_project_count = openProjects.length;

  for (var p = 0; p < openProjects.length; p += 1) {
    var projectObject = openProjects[p];
    var projectHasReference = false;
    if (!projectObject || !projectObject.rootItem) {
      continue;
    }
    __atrWalkProjectItems(projectObject.rootItem, function (item) {
      if (!item || __atrGetProjectItemChildCount(item) > 0) {
        return;
      }
      var mediaPath = __atrGetProjectItemMediaPath(item);
      var proxyPath = "";
      try {
        if (item.hasProxy && item.getProxyPath && item.hasProxy()) {
          proxyPath = __atrSafeString(item.getProxyPath());
        }
      } catch (eProxyState) {}

      for (var r = 0; r < normalizedRoots.length; r += 1) {
        var rootPath = normalizedRoots[r];
        var mediaMatches =
          mediaPath && __atrPathStartsWith(mediaPath, rootPath);
        var proxyMatches =
          proxyPath && __atrPathStartsWith(proxyPath, rootPath);
        if (!mediaMatches && !proxyMatches) {
          continue;
        }
        projectHasReference = true;
        if (mediaMatches) {
          report.remaining_media_references += 1;
        }
        if (proxyMatches) {
          report.remaining_proxy_references += 1;
        }
        if (report.samples.length < 8) {
          report.samples.push(
            (__atrGetProjectIdentity(projectObject) || "project") +
              ":" +
              (__atrGetProjectItemName(item) || "item"),
          );
        }
        break;
      }
    });
    if (projectHasReference) {
      report.referencing_project_count += 1;
    }
  }

  report.ok =
    report.remaining_media_references === 0 &&
    report.remaining_proxy_references === 0;
  return report;
}

function __atrCloseSourceMonitorForCleanup(result) {
  if (!app || !app.sourceMonitor) {
    return;
  }
  try {
    if (app.sourceMonitor.closeAllClips) {
      app.sourceMonitor.closeAllClips();
      if (result) {
        result.source_monitor_close_attempted = true;
      }
      return;
    }
  } catch (eCloseAll) {
    if (result && result.detach_proxy_warnings) {
      result.detach_proxy_warnings.push(
        "Could not close all Source Monitor clips: " +
          __atrSafeString(eCloseAll.message || eCloseAll),
      );
    }
  }

  try {
    if (app.sourceMonitor.closeClip) {
      for (var i = 0; i < 25; i += 1) {
        app.sourceMonitor.closeClip();
      }
      if (result) {
        result.source_monitor_close_attempted = true;
      }
    }
  } catch (eCloseClip) {
    if (result && result.detach_proxy_warnings) {
      result.detach_proxy_warnings.push(
        "Could not close Source Monitor clips: " +
          __atrSafeString(eCloseClip.message || eCloseClip),
      );
    }
  }
}

function __atrPushEncoderEvent(type, jobID, detail) {
  var meta = __atrEncoderJobMetaMap[jobID] || null;
  var event = {
    type: __atrSafeString(type),
    job_id: __atrSafeString(jobID),
    project_id: __atrSafeString(
      (detail && detail.project_id) ||
        (meta && meta.project_id) ||
        __atrEncoderJobProjectMap[jobID] ||
        "",
    ),
    timestamp: new Date().toISOString ? new Date().toISOString() : "",
    detail: detail || {},
  };
  if (meta && meta.render_kind) {
    event.detail.render_kind = __atrSafeString(meta.render_kind);
  }
  __atrEncoderEvents.push(event);
}

function __atrPushHostTrace(projectId, message, level, detail) {
  var payload = detail || {};
  payload.project_id = __atrSafeString(projectId);
  payload.message = __atrSafeString(message);
  payload.level = __atrSafeString(level || "info");
  payload.host_build_id = ATR_HOST_BUILD_ID;
  __atrPushEncoderEvent("trace", "", payload);
}

function __atrRememberEncoderJob(
  jobID,
  projectId,
  renderKind,
  outputPath,
  presetPath,
) {
  __atrEncoderJobProjectMap[jobID] = __atrSafeString(projectId);
  __atrEncoderJobMetaMap[jobID] = {
    project_id: __atrSafeString(projectId),
    render_kind: __atrSafeString(renderKind || "video"),
    output_path: __atrSafeString(outputPath),
    preset_path: __atrSafeString(presetPath),
  };
}

function __atrAttachProxyToMatchingItems(
  projectId,
  mediaPath,
  proxyPath,
  localRootPath,
  existingIndex,
) {
  var response = {
    proxy_attached: false,
    proxy_attach_pending: false,
    proxy_attach_error: "",
    media_path: __atrSafeString(mediaPath),
    output_path: __atrSafeString(proxyPath),
    item_count: 0,
    eligible_count: 0,
    ignored_count: 0,
    attached_count: 0,
    already_compliant_count: 0,
    completed_count: 0,
    attach_pending_count: 0,
    unverified_attach_count: 0,
    attach_pending_samples: [],
    errors: [],
  };

  var proxyFile = new File(__atrNormalizePath(proxyPath));
  var proxyFsPath = __atrSafeString(proxyFile.fsName || proxyPath);
  var items = __atrFindProjectItemsByMediaPath(mediaPath, existingIndex);
  response.item_count = items.length;
  if (!items.length) {
    response.proxy_attach_pending = true;
    response.proxy_attach_error =
      "Unable to find imported project item for " + mediaPath;
    __atrPushHostTrace(
      projectId,
      "Proxy attach could not find project item for " + mediaPath,
      "warn",
    );
    return response;
  }

  for (var i = 0; i < items.length; i += 1) {
    var item = items[i];
    var itemName = __atrGetProjectItemName(item);
    if (!item || __atrIsAtrRawAudioProjectItem(item)) {
      response.ignored_count += 1;
      continue;
    }
    if (!__atrProjectItemCanAcceptProxy(item)) {
      response.ignored_count += 1;
      continue;
    }

    response.eligible_count += 1;
    if (localRootPath && item.hasProxy && item.getProxyPath) {
      try {
        if (
          item.hasProxy() &&
          __atrLooksLikeManagedProxyPath(item.getProxyPath(), localRootPath)
        ) {
          response.already_compliant_count += 1;
          response.completed_count += 1;
          continue;
        }
      } catch (eManagedProxyState) {}
    }

    var matchState = __atrProjectItemProxyMatchState(item, proxyFsPath);
    if (matchState === 1) {
      response.already_compliant_count += 1;
      response.completed_count += 1;
      continue;
    }

    try {
      var attachAttempt = __atrTryAttachProxy(item, proxyFsPath);
      if (attachAttempt.ok) {
        response.attached_count += 1;
        response.completed_count += 1;
      } else if (attachAttempt.pending) {
        response.attach_pending_count += 1;
        response.unverified_attach_count += 1;
        if (response.attach_pending_samples.length < 4) {
          response.attach_pending_samples.push(
            (itemName || __atrGetBasename(mediaPath)) +
              ": " +
              __atrSafeString(attachAttempt.error || "attach pending"),
          );
        }
      } else {
        response.errors.push(
          "Failed to attach proxy" +
            (itemName ? " to " + itemName : "") +
            ": " +
            __atrSafeString(attachAttempt.error || "attachProxy returned false"),
        );
      }
    } catch (eAttach) {
      response.errors.push(
        "Failed to attach proxy" +
          (itemName ? " to " + itemName : "") +
          ": " +
          __atrSafeString(eAttach.message || eAttach),
      );
    }
  }

  response.proxy_attached =
    response.eligible_count > 0 &&
    response.completed_count >= response.eligible_count;
  if (!response.proxy_attached) {
    response.proxy_attach_pending = true;
    response.proxy_attach_error =
      response.errors.length > 0
        ? response.errors.join(" | ")
        : response.attach_pending_samples.join(" | ");
    if (!response.proxy_attach_error && response.eligible_count <= 0) {
      response.proxy_attach_error =
        "No proxy-capable video project item found yet for " +
        __atrGetBasename(mediaPath) +
        (items.length > 0
          ? "; ignored candidates: " +
            __atrCollectProjectItemCandidateNames(items, 6)
          : "");
    }
  }
  return response;
}

function __atrForgetEncoderJob(jobID) {
  var tempSequenceRecord = __atrTempAudioSequenceByJob[jobID] || null;
  if (tempSequenceRecord) {
    __atrDeleteTempAudioSequenceRecord(tempSequenceRecord);
  }
  try {
    delete __atrTempAudioSequenceByJob[jobID];
  } catch (e0) {}
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
  var projectId = __atrSafeString(__atrEncoderJobProjectMap[jobID] || "");
  __atrPushHostTrace(projectId, "AME queued job " + __atrSafeString(jobID), "info");
  try {
    if (app && app.encoder && app.encoder.startBatch) {
      app.encoder.startBatch();
    }
  } catch (eStartBatch) {}
  __atrPushEncoderEvent("queued", jobID, {});
}

function ATR_onEncoderJobProgress(jobID, progress) {
  var numericProgress = Number(progress);
  if (isNaN(numericProgress)) {
    numericProgress = -1;
  }
  __atrPushEncoderEvent("progress", jobID, {
    progress: numericProgress,
  });
}

function ATR_onEncoderJobComplete(jobID, outputPath) {
  var projectId = __atrSafeString(__atrEncoderJobProjectMap[jobID] || "");
  __atrPushHostTrace(
    projectId,
    "AME completed job " + __atrSafeString(jobID),
    "info",
  );
  var detail = {
    output_path: __atrSafeString(outputPath),
  };
  __atrPushEncoderEvent("complete", jobID, detail);
  __atrForgetEncoderJob(jobID);
}

function ATR_onEncoderJobError(jobID, errorDetail) {
  var projectId = __atrSafeString(__atrEncoderJobProjectMap[jobID] || "");
  __atrPushHostTrace(
    projectId,
    "AME error for job " +
      __atrSafeString(jobID) +
      ": " +
      __atrSafeString(errorDetail),
    "error",
  );
  __atrPushEncoderEvent("error", jobID, {
    error: __atrSafeString(errorDetail),
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
      1,
    );
  } catch (eFive) {
    jobID = app.encoder.encodeSequence(
      sequence,
      outputFsPath,
      presetFsPath,
      app.encoder.ENCODE_ENTIRE,
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

function __atrFindSequenceByName(sequenceName) {
  var targetName = __atrSafeString(sequenceName);
  if (!targetName) {
    return null;
  }

  var sequences = app && app.project ? app.project.sequences : null;
  if (!sequences) {
    return null;
  }

  var count = 0;
  try {
    count = Number(sequences.numSequences || 0);
  } catch (eCount) {
    count = 0;
  }

  // Pass 1: exact name match
  for (var i = 0; i < count; i += 1) {
    var sequence = sequences[i];
    if (!sequence) {
      continue;
    }
    if (__atrGetSequenceName(sequence) === targetName) {
      return sequence;
    }
  }

  // Pass 2: fallback via projectItem.name (handles bin corruption where
  // sequence.name may be unavailable but projectItem retains the name)
  for (var j = 0; j < count; j += 1) {
    var seq2 = sequences[j];
    if (!seq2) {
      continue;
    }
    try {
      if (seq2.projectItem && __atrSafeString(seq2.projectItem.name) === targetName) {
        return seq2;
      }
    } catch (eFallback) {}
  }

  return null;
}

function __atrGetProjectName(projectObject) {
  try {
    return __atrSafeString(projectObject && projectObject.name);
  } catch (eName) {
    return "";
  }
}

function __atrGetProjectPath(projectObject) {
  try {
    return __atrNormalizePath(projectObject && projectObject.path);
  } catch (ePath) {
    return "";
  }
}

function __atrGetProjectSequences(projectObject) {
  try {
    return projectObject && projectObject.sequences
      ? projectObject.sequences
      : null;
  } catch (eSequences) {
    return null;
  }
}

function __atrPushUniqueSequence(sequences, sequence) {
  if (!sequence) {
    return;
  }
  var candidateId = __atrGetSequenceId(sequence);
  for (var i = 0; i < sequences.length; i += 1) {
    try {
      if (sequences[i] === sequence) {
        return;
      }
    } catch (eSameSequence) {}
    var existingId = __atrGetSequenceId(sequences[i]);
    if (candidateId && existingId && candidateId === existingId) {
      return;
    }
  }
  sequences.push(sequence);
}

function __atrGetProjectSequenceCandidates(projectObject) {
  var candidates = [];
  if (!projectObject) {
    return candidates;
  }

  // activeSequence can remain valid while a stale SequenceCollection wrapper
  // reports zero entries after a project close/reopen cycle.
  try {
    __atrPushUniqueSequence(candidates, projectObject.activeSequence || null);
  } catch (eActiveSequence) {}

  var sequenceCollection = __atrGetProjectSequences(projectObject);
  var count = 0;
  try {
    count = Number(sequenceCollection && sequenceCollection.numSequences || 0);
  } catch (eSequenceCount) {
    count = 0;
  }
  for (var i = 0; i < count; i += 1) {
    try {
      __atrPushUniqueSequence(candidates, sequenceCollection[i]);
    } catch (eSequenceItem) {}
  }
  return candidates;
}

function __atrFindDirectChildByName(containerItem, itemName) {
  var targetName = __atrSafeString(itemName);
  var count = __atrGetProjectItemChildCount(containerItem);
  for (var i = 0; i < count; i += 1) {
    var child = containerItem.children[i];
    if (child && __atrGetProjectItemName(child) === targetName) {
      return child;
    }
  }
  return null;
}

function __atrGetProjectItemNodeId(projectItem) {
  try {
    return __atrSafeString(projectItem && projectItem.nodeId);
  } catch (eNodeId) {
    return "";
  }
}

function __atrCollectProjectItemNodeIds(containerItem, nodeIds) {
  var target = nodeIds || {};
  var count = __atrGetProjectItemChildCount(containerItem);
  for (var i = 0; i < count; i += 1) {
    var child = containerItem.children[i];
    if (!child) {
      continue;
    }
    var nodeId = __atrGetProjectItemNodeId(child);
    if (nodeId) {
      target[nodeId] = true;
    }
    if (__atrGetProjectItemChildCount(child) > 0) {
      __atrCollectProjectItemNodeIds(child, target);
    }
  }
  return target;
}

function __atrSequenceBelongsToBin(sequence, automationBin, binNodeIds) {
  if (!sequence || !automationBin) {
    return false;
  }
  var sequenceItem = null;
  try {
    sequenceItem = sequence.projectItem || null;
  } catch (eSequenceItem) {}
  if (!sequenceItem) {
    return false;
  }
  try {
    if (sequenceItem === automationBin || sequenceItem.parent === automationBin) {
      return true;
    }
  } catch (eParent) {}
  var nodeId = __atrGetProjectItemNodeId(sequenceItem);
  return !!(nodeId && binNodeIds && binNodeIds[nodeId]);
}

function __atrDescribeManagedSequence(projectId, projectObject, sequence) {
  return {
    project_id: __atrSafeString(projectId),
    project_identity: __atrGetProjectIdentity(projectObject),
    project_name: __atrGetProjectName(projectObject),
    project_path: __atrGetProjectPath(projectObject),
    sequence_id: __atrGetSequenceId(sequence),
    sequence_name: __atrGetSequenceName(sequence),
  };
}

function __atrDescribeManagedProject(projectObject, hasAutomationBin, rootMatch) {
  var sequenceDescriptions = [];
  var sequences = __atrGetProjectSequenceCandidates(projectObject);
  var sequenceCount = sequences.length;
  for (var i = 0; i < sequenceCount && i < 20; i += 1) {
    if (!sequences[i]) {
      continue;
    }
    sequenceDescriptions.push({
      sequence_id: __atrGetSequenceId(sequences[i]),
      sequence_name: __atrGetSequenceName(sequences[i]),
    });
  }
  return {
    project_identity: __atrGetProjectIdentity(projectObject),
    project_name: __atrGetProjectName(projectObject),
    project_path: __atrGetProjectPath(projectObject),
    has_automation_bin: !!hasAutomationBin,
    local_root_match: !!rootMatch,
    sequence_count: sequenceCount,
    sequences: sequenceDescriptions,
  };
}

function __atrDescribeManagedCandidates(candidates) {
  var descriptions = [];
  for (var i = 0; candidates && i < candidates.length; i += 1) {
    descriptions.push(candidates[i].descriptor);
  }
  return descriptions;
}

function __atrResolveManagedSequence(entry, allowRepair) {
  var projectId = __atrSafeString(entry && entry.project_id);
  var expectedName = __atrSafeString(entry && entry.sequence_name);
  var localRoot = __atrNormalizePath(entry && entry.local_root);
  var automationBinName = "__ATR_PROJECT__" + projectId;
  var registeredTarget = __atrManagedExportTargetsByProjectId[projectId];
  if (
    registeredTarget &&
    registeredTarget.project &&
    registeredTarget.sequence &&
    (!expectedName ||
      __atrGetSequenceName(registeredTarget.sequence) === expectedName)
  ) {
    var registeredDescriptor = __atrDescribeManagedSequence(
      projectId,
      registeredTarget.project,
      registeredTarget.sequence,
    );
    if (registeredDescriptor.sequence_id && registeredDescriptor.sequence_name) {
      registeredDescriptor.repaired_name = false;
      registeredDescriptor.registered_target = true;
      return {
        status: "resolved",
        target: registeredTarget,
        descriptor: registeredDescriptor,
        projects: [],
      };
    }
  }
  var projects = __atrGetOpenProjects();
  // runScript() captures the exact Project object used by every managed
  // import. Include those references only for sequence recovery; treating them
  // as globally open would break cleanup's post-close verification.
  for (var remembered = 0;
    remembered < __atrImportedProjectRefs.length;
    remembered += 1) {
    __atrPushUniqueProject(projects, __atrImportedProjectRefs[remembered]);
  }
  var exactCandidates = [];
  var binCandidates = [];
  var projectDiagnostics = [];

  for (var p = 0; p < projects.length; p += 1) {
    var projectObject = projects[p];
    var rootItem = null;
    try {
      rootItem = projectObject.rootItem || null;
    } catch (eRootItem) {}
    var automationBin = __atrFindDirectChildByName(rootItem, automationBinName);
    var rootMatch = false;
    if (localRoot) {
      rootMatch = __atrProjectReferencesAnyCleanupRoot(projectObject, [localRoot]);
    }
    projectDiagnostics.push(
      __atrDescribeManagedProject(projectObject, !!automationBin, rootMatch),
    );
    if (!automationBin && !rootMatch) {
      continue;
    }
    var binNodeIds = automationBin
      ? __atrCollectProjectItemNodeIds(automationBin, {})
      : {};
    var sequences = __atrGetProjectSequenceCandidates(projectObject);
    var sequenceCount = sequences.length;
    for (var s = 0; s < sequenceCount; s += 1) {
      var sequence = sequences[s];
      if (!sequence) {
        continue;
      }
      var sequenceName = __atrGetSequenceName(sequence);
      var candidate = {
        project: projectObject,
        sequence: sequence,
        descriptor: __atrDescribeManagedSequence(
          projectId,
          projectObject,
          sequence,
        ),
      };
      if (sequenceName === expectedName) {
        exactCandidates.push(candidate);
      }
      if (
        automationBin &&
        __atrSequenceBelongsToBin(sequence, automationBin, binNodeIds)
      ) {
        binCandidates.push(candidate);
      }
    }
  }

  if (exactCandidates.length === 1) {
    exactCandidates[0].descriptor.repaired_name = false;
    return {
      status: "resolved",
      target: exactCandidates[0],
      descriptor: exactCandidates[0].descriptor,
      projects: projectDiagnostics,
    };
  }
  if (exactCandidates.length > 1) {
    return {
      status: "ambiguous",
      project_id: projectId,
      sequence_name: expectedName,
      local_root: localRoot,
      projects: projectDiagnostics,
      candidates: __atrDescribeManagedCandidates(exactCandidates),
      reason: "Multiple exact managed sequence matches",
    };
  }

  if (allowRepair && binCandidates.length === 1 && expectedName) {
    var repaired = binCandidates[0];
    try {
      repaired.sequence.name = expectedName;
    } catch (eRenameSequence) {}
    try {
      if (repaired.sequence.projectItem) {
        repaired.sequence.projectItem.name = expectedName;
      }
    } catch (eRenameProjectItem) {}
    repaired.descriptor = __atrDescribeManagedSequence(
      projectId,
      repaired.project,
      repaired.sequence,
    );
    if (repaired.descriptor.sequence_name === expectedName) {
      repaired.descriptor.repaired_name = true;
      return {
        status: "resolved",
        target: repaired,
        descriptor: repaired.descriptor,
        projects: projectDiagnostics,
      };
    }
  }

  if (binCandidates.length > 1) {
    return {
      status: "ambiguous",
      project_id: projectId,
      sequence_name: expectedName,
      local_root: localRoot,
      projects: projectDiagnostics,
      candidates: __atrDescribeManagedCandidates(binCandidates),
      reason: "Multiple sequences belong to the automation bin",
    };
  }

  return {
    status: "missing",
    project_id: projectId,
    sequence_name: expectedName,
    local_root: localRoot,
    projects: projectDiagnostics,
    candidates: __atrDescribeManagedCandidates(binCandidates),
    reason:
      binCandidates.length === 1
        ? "Unique automation sequence could not be renamed"
        : "No managed sequence found",
  };
}

function preflightManagedBatchExport(batchJson) {
  try {
    var entries = [];
    try {
      entries =
        typeof batchJson === "string" ? JSON.parse(batchJson || "[]") : batchJson;
    } catch (eParse) {
      return "ERROR: Invalid batch preflight payload";
    }
    if (!entries || !entries.length) {
      return JSON.stringify({
        ok: false,
        checked: 0,
        found: 0,
        resolved: [],
        missing: [],
        ambiguous: [],
        error: "No batch sequences to preflight",
      });
    }

    var resolved = [];
    var missing = [];
    var ambiguous = [];
    // Keep targets captured immediately after import. Rebuilding this map here
    // used to discard the only reliable direct references before preflight
    // searched Premiere's occasionally stale Project/Sequence collections.
    for (var i = 0; i < entries.length; i += 1) {
      var entry = entries[i] || {};
      var projectId = __atrSafeString(entry.project_id);
      var resolution = __atrResolveManagedSequence(entry, true);
      if (resolution.status === "resolved") {
        resolved.push(resolution.descriptor);
        __atrManagedExportTargetsByProjectId[projectId] = resolution.target;
      } else if (resolution.status === "ambiguous") {
        ambiguous.push(resolution);
      } else {
        missing.push(resolution);
      }
    }

    return JSON.stringify({
      ok: missing.length === 0 && ambiguous.length === 0,
      checked: entries.length,
      found: resolved.length,
      resolved: resolved,
      missing: missing,
      ambiguous: ambiguous,
    });
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function __atrCloneSequence(sourceSequence, owningProject) {
  var sequences = __atrGetProjectSequences(
    owningProject || (app && app.project ? app.project : null),
  );
  if (!sequences) {
    throw new Error("No sequence collection available");
  }

  var beforeCount = 0;
  var beforeKeys = __atrCaptureSequenceKeys(owningProject);
  try {
    beforeCount = Number(sequences.numSequences || 0);
  } catch (eBefore) {
    beforeCount = 0;
  }

  if (!sourceSequence || !sourceSequence.clone) {
    throw new Error("Sequence cannot be cloned");
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

  var cloneSequence = null;
  for (var i = 0; i < afterCount; i += 1) {
    var candidate = sequences[i];
    if (!candidate) {
      continue;
    }
    if (!beforeKeys[__atrBuildSequenceObjectKey(candidate, i)]) {
      cloneSequence = candidate;
      break;
    }
  }
  if (!cloneSequence) {
    cloneSequence = sequences[afterCount - 1];
  }
  if (!cloneSequence) {
    throw new Error("Unable to access cloned sequence");
  }

  return cloneSequence;
}

function __atrGetSequenceName(sequence) {
  if (!sequence) {
    return "";
  }
  try {
    return __atrSafeString(sequence.name || sequence.sequenceID || "");
  } catch (e) {
    return "";
  }
}

function __atrGetSequenceId(sequence) {
  if (!sequence) {
    return "";
  }
  try {
    return __atrSafeString(sequence.sequenceID || "");
  } catch (e) {
    return "";
  }
}

function __atrSequenceNameHasTempAudioPrefix(sequenceName) {
  return __atrSafeString(sequenceName).indexOf(__atrTempAudioSequencePrefix) === 0;
}

function __atrCaptureSequenceKeys(projectObject) {
  var out = {};
  var sequences = __atrGetProjectSequences(
    projectObject || (app && app.project ? app.project : null),
  );
  var count = 0;
  if (!sequences) {
    return out;
  }
  try {
    count = Number(sequences.numSequences || 0);
  } catch (eCount) {
    count = 0;
  }
  for (var i = 0; i < count; i += 1) {
    var sequence = sequences[i];
    if (!sequence) {
      continue;
    }
    out[__atrBuildSequenceObjectKey(sequence, i)] = true;
  }
  return out;
}

function __atrBuildSequenceObjectKey(sequence, index) {
  var sequenceId = __atrGetSequenceId(sequence);
  if (sequenceId) {
    return "id:" + sequenceId;
  }
  return "name:" + __atrGetSequenceName(sequence) + "::index:" + __atrSafeString(index);
}

function __atrFindSequenceByTempRecord(record) {
  var sequenceName = "";
  var sequenceId = "";
  if (typeof record === "string") {
    sequenceName = __atrSafeString(record);
  } else if (record) {
    sequenceName = __atrSafeString(record.name);
    sequenceId = __atrSafeString(record.sequence_id);
  }
  if (!__atrSequenceNameHasTempAudioPrefix(sequenceName)) {
    return null;
  }

  var recordProject = record && typeof record !== "string" ? record.project : null;
  var sequences = __atrGetProjectSequences(
    recordProject || (app && app.project ? app.project : null),
  );
  var count = 0;
  if (!sequences) {
    return null;
  }
  try {
    count = Number(sequences.numSequences || 0);
  } catch (eCount) {
    count = 0;
  }
  for (var i = 0; i < count; i += 1) {
    var sequence = sequences[i];
    if (!sequence) {
      continue;
    }
    if (sequenceId && __atrGetSequenceId(sequence) === sequenceId) {
      return sequence;
    }
    if (__atrGetSequenceName(sequence) === sequenceName) {
      return sequence;
    }
  }
  return null;
}

function __atrDeleteTempAudioSequenceRecord(record) {
  var sequence = __atrFindSequenceByTempRecord(record);
  if (!sequence) {
    return false;
  }
  if (!__atrSequenceNameHasTempAudioPrefix(__atrGetSequenceName(sequence))) {
    return false;
  }
  return __atrDeleteSequenceObject(sequence);
}

function __atrDeleteSequenceByName(sequenceName) {
  var targetName = __atrSafeString(sequenceName);
  if (!targetName || !__atrSequenceNameHasTempAudioPrefix(targetName)) {
    return false;
  }

  var sequences = app && app.project ? app.project.sequences : null;
  if (!sequences) {
    return false;
  }

  var count = 0;
  try {
    count = Number(sequences.numSequences || 0);
  } catch (eCount) {
    count = 0;
  }

  for (var i = 0; i < count; i += 1) {
    var sequence = sequences[i];
    if (!sequence) {
      continue;
    }
    if (__atrGetSequenceName(sequence) !== targetName) {
      continue;
    }

    try {
      if (sequence.projectItem && sequence.projectItem.deleteBin) {
        var deleteBinResult = sequence.projectItem.deleteBin();
        return (
          deleteBinResult === 0 ||
          deleteBinResult === "0" ||
          deleteBinResult === true
        );
      }
    } catch (eDeleteBin) {}

    try {
      if (
        sequence.projectItem &&
        sequence.projectItem.select &&
        app.project &&
        app.project.deleteSelection
      ) {
        sequence.projectItem.select();
        return !!app.project.deleteSelection();
      }
    } catch (eDeleteSelection) {}
  }

  return false;
}

function cleanupOrphanTempAudioSequences() {
  try {
    var sequences = app && app.project ? app.project.sequences : null;
    if (!sequences) {
      return "0";
    }

    var count = 0;
    try {
      count = Number(sequences.numSequences || 0);
    } catch (eCount) {
      count = 0;
    }

    var removed = 0;
    for (var i = count - 1; i >= 0; i -= 1) {
      var sequence = sequences[i];
      var name = __atrGetSequenceName(sequence);
      if (!name || name.indexOf(__atrTempAudioSequencePrefix) !== 0) {
        continue;
      }
      if (__atrDeleteSequenceByName(name)) {
        removed += 1;
      }
    }

    return __atrSafeString(removed);
  } catch (e) {
    return "ERROR: " + e.message;
  }
}

function __atrGetSequenceCount(projectObject) {
  var targetProject = projectObject || (app && app.project ? app.project : null);
  var sequences = targetProject ? targetProject.sequences : null;
  if (!sequences) {
    return 0;
  }

  try {
    return Number(sequences.numSequences || 0);
  } catch (eCount) {
    return 0;
  }
}

function __atrDeleteSequenceObject(sequence, projectObject) {
  var targetProject = projectObject || (app && app.project ? app.project : null);
  if (!sequence || !targetProject) {
    return false;
  }

  var deleted = false;
  try {
    if (targetProject.deleteSequence && sequence.sequenceID !== undefined) {
      deleted = !!targetProject.deleteSequence(sequence.sequenceID);
    }
  } catch (eDeleteById) {}

  if (!deleted) {
    try {
      if (targetProject.deleteSequence) {
        deleted = !!targetProject.deleteSequence(sequence);
      }
    } catch (eDeleteByObject) {}
  }

  if (!deleted) {
    try {
      if (sequence.projectItem && sequence.projectItem.deleteBin) {
        var deleteBinResult = sequence.projectItem.deleteBin();
        // ProjectItem.deleteBin() reports success as numeric 0.
        deleted =
          deleteBinResult === 0 ||
          deleteBinResult === "0" ||
          deleteBinResult === true;
      }
    } catch (eDeleteBin) {}
  }

  if (!deleted) {
    try {
      if (
        sequence.projectItem &&
        sequence.projectItem.select &&
        targetProject.deleteSelection
      ) {
        sequence.projectItem.select();
        deleted = !!targetProject.deleteSelection();
      }
    } catch (eDeleteSelection) {}
  }

  return deleted;
}

function closeImportedProjectsForCleanup(localRootsJson) {
  try {
    var roots = [];
    try {
      roots =
        typeof localRootsJson === "string"
          ? JSON.parse(localRootsJson || "[]")
          : localRootsJson;
    } catch (eParse) {
      roots = [];
    }
    var normalizedRoots = [];
    for (var i = 0; roots && i < roots.length; i += 1) {
      var normalizedRoot = __atrNormalizeComparePath(roots[i]);
      if (normalizedRoot) {
        normalizedRoots.push(normalizedRoot);
      }
    }
    if (normalizedRoots.length === 0) {
      return "ERROR: No local project roots were provided for project release";
    }

    // Resolve the owning project before touching any ProjectItem. In
    // particular, do not call the undocumented detachProxy() method: on some
    // Windows Premiere builds it reports detached but asynchronously queues a
    // Link Media dialog when the proxy disappears.
    var targetProjects = __atrResolveCleanupTargetProjects(normalizedRoots);
    __atrCloseSourceMonitorForCleanup(null);
    var result = {
      ok: true,
      strategy: "close_project_before_disk_cleanup",
      target_project_count: targetProjects.length,
      close_requested_count: 0,
      closed_project_count: 0,
      close_pending_count: 0,
      already_released: targetProjects.length === 0,
      project_identities: [],
      project_paths: [],
      warnings: [],
    };
    __atrCleanupClosingProjectIdentities = [];

    for (var p = 0; p < targetProjects.length; p += 1) {
      var targetProject = targetProjects[p];
      if (!targetProject) {
        continue;
      }
      var identity = __atrGetProjectIdentity(targetProject);
      var projectPath = "";
      try {
        projectPath = __atrSafeString(targetProject.path || "");
      } catch (eProjectPath) {}
      result.project_identities.push(identity);
      result.project_paths.push(projectPath);
      __atrCleanupClosingProjectIdentities.push(identity);

      if (!targetProject.closeDocument) {
        result.ok = false;
        result.warnings.push(
          "Project.closeDocument is unavailable for " +
            (identity || "cleanup project"),
        );
        continue;
      }

      try {
        // Both original and proxy files still exist at this point. Closing the
        // transient document without saving releases their records as one
        // supported operation, instead of trying to mutate each proxy item.
        var closeResult = targetProject.closeDocument(0, 0);
        var stillOpen = __atrProjectIsOpen(
          targetProject,
          __atrGetOpenProjects(),
        );
        var closeAccepted =
          closeResult === 0 ||
          closeResult === "0" ||
          closeResult === true;
        if (!stillOpen) {
          result.close_requested_count += 1;
          result.closed_project_count += 1;
        } else if (closeAccepted) {
          // closeDocument() can be committed after this ExtendScript call
          // returns. The panel yields, and openCleanupScratchProject() refuses
          // to continue until this identity is actually absent.
          result.close_requested_count += 1;
          result.close_pending_count += 1;
        } else {
          result.ok = false;
          result.warnings.push(
            "Premiere rejected closeDocument for " +
              (identity || "cleanup project"),
          );
        }
      } catch (eCloseProject) {
        result.ok = false;
        result.warnings.push(
          "Could not close " +
            (identity || "cleanup project") +
            ": " +
            __atrSafeString(eCloseProject.message || eCloseProject),
        );
      }
    }

    result.ok =
      result.ok &&
      result.close_requested_count === result.target_project_count;
    if (!result.ok) {
      result.error = "Premiere cleanup project close was not accepted";
    }
    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function openCleanupScratchProject(scratchProjectPath) {
  try {
    var result = {
      ok: true,
      scratch_project_path: __atrSafeString(scratchProjectPath),
      pending_cleanup_project_count: 0,
      closed_existing_scratch_count: 0,
      removed_existing_scratch: false,
      created_scratch: false,
      warnings: [],
    };
    var openProjects = __atrGetOpenProjects();
    for (var i = 0; i < openProjects.length; i += 1) {
      var openIdentity = __atrGetProjectIdentity(openProjects[i]);
      for (
        var closing = 0;
        closing < __atrCleanupClosingProjectIdentities.length;
        closing += 1
      ) {
        if (
          openIdentity &&
          openIdentity === __atrCleanupClosingProjectIdentities[closing]
        ) {
          result.pending_cleanup_project_count += 1;
          break;
        }
      }
    }
    if (result.pending_cleanup_project_count > 0) {
      result.ok = false;
      result.error = "Premiere still has a cleanup project open";
      return JSON.stringify(result);
    }

    var normalizedScratchPath = __atrNormalizePath(scratchProjectPath);
    if (!normalizedScratchPath) {
      return "ERROR: Cleanup scratch project path is empty";
    }
    var scratchComparePath = __atrNormalizeComparePath(normalizedScratchPath);

    // The extension-owned scratch can already be open after an interrupted
    // cleanup. Close it without saving before replacing its on-disk file.
    openProjects = __atrGetOpenProjects();
    for (var p = 0; p < openProjects.length; p += 1) {
      var candidatePath = "";
      try {
        candidatePath = __atrNormalizeComparePath(openProjects[p].path || "");
      } catch (eCandidatePath) {}
      if (candidatePath !== scratchComparePath) {
        continue;
      }
      try {
        if (openProjects[p].closeDocument) {
          openProjects[p].closeDocument(0, 0);
          result.closed_existing_scratch_count += 1;
        }
      } catch (eCloseScratch) {
        result.warnings.push(
          "Could not recycle the existing ATR scratch project: " +
            __atrSafeString(eCloseScratch.message || eCloseScratch),
        );
      }
    }

    var scratchFile = new File(normalizedScratchPath);
    if (scratchFile.exists) {
      try {
        result.removed_existing_scratch = !!scratchFile.remove();
      } catch (eRemoveScratch) {
        result.warnings.push(
          "Could not remove the previous ATR scratch project: " +
            __atrSafeString(eRemoveScratch.message || eRemoveScratch),
        );
      }
    }
    if (scratchFile.exists) {
      result.ok = false;
      result.error =
        "The previous ATR scratch project is still locked; cleanup will retry";
      return JSON.stringify(result);
    }

    var createAttempted = false;
    var createResult = false;
    if (app.newProject) {
      try {
        if (!scratchFile.parent.exists) {
          scratchFile.parent.create();
        }
        createAttempted = true;
        createResult = !!app.newProject(scratchFile.fsName);
      } catch (eNewScratch) {
        result.warnings.push(
          "Could not create the ATR scratch project: " +
            __atrSafeString(eNewScratch.message || eNewScratch),
        );
      }
    }

    var activeProjectPath = "";
    try {
      activeProjectPath = __atrNormalizeComparePath(
        app && app.project ? app.project.path || "" : "",
      );
    } catch (eActiveScratchPath) {}
    result.active_project_path = activeProjectPath;
    result.created_scratch =
      createAttempted &&
      createResult &&
      activeProjectPath === scratchComparePath;
    result.ok = result.created_scratch;
    if (!result.ok) {
      result.error = "Premiere could not activate the ATR cleanup scratch project";
    } else {
      __atrCleanupClosingProjectIdentities = [];
      __atrImportedProjectRefs = [];
      __atrManagedExportTargetsByProjectId = {};
    }
    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function verifyImportedProjectsCleanup(localRootsJson) {
  try {
    var roots = [];
    try {
      roots =
        typeof localRootsJson === "string"
          ? JSON.parse(localRootsJson || "[]")
          : localRootsJson;
    } catch (eParse) {
      roots = [];
    }
    var normalizedRoots = [];
    for (var i = 0; roots && i < roots.length; i += 1) {
      var normalizedRoot = __atrNormalizeComparePath(roots[i]);
      if (normalizedRoot) {
        normalizedRoots.push(normalizedRoot);
      }
    }
    if (normalizedRoots.length === 0) {
      return "ERROR: No local project roots were provided for cleanup verification";
    }
    __atrCloseSourceMonitorForCleanup(null);
    return JSON.stringify(
      __atrInspectOpenProjectCleanupReferences(normalizedRoots),
    );
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

/**
 * Execute a .jsx script file with error handling.
 * @param {string} scriptPath - Absolute path to the .jsx file (forward slashes)
 * @param {string} managedProjectId - Optional project ID requiring sequence validation
 * @param {string} managedSequenceName - Optional expected managed sequence name
 * @param {string} managedLocalRoot - Optional downloaded project root for resolution
 * @returns {string} "OK", "OK::ATR_WARN::<warnings>" on success (non-fatal
 *   issues the script published), or "ERROR: ..." on failure
 */
function runScript(
  scriptPath,
  managedProjectId,
  managedSequenceName,
  managedLocalRoot
) {
  try {
    var normalizedManagedProjectId = __atrSafeString(managedProjectId);
    var normalizedManagedSequenceName = __atrSafeString(managedSequenceName);
    var normalizedManagedLocalRoot = __atrNormalizePath(managedLocalRoot);
    if (normalizedManagedProjectId) {
      delete __atrManagedExportTargetsByProjectId[normalizedManagedProjectId];
    }
    var file = new File(scriptPath);
    if (!file.exists) {
      return "ERROR: File not found: " + scriptPath;
    }
    // Clear before every run so a previous script's warnings can never leak
    // into this run's report (the ExtendScript engine is persistent).
    try {
      $.global.__ATR_IMPORT_WARNINGS__ = "";
    } catch (eClearImportWarnings) {}
    // Keep the owning Project object. app.project means "currently active",
    // which may be a different open project by the time upload and cleanup
    // finish.
    try {
      __atrRememberImportedProject(app && app.project ? app.project : null);
    } catch (eRememberBefore) {}
    $.evalFile(file);
    try {
      __atrRememberImportedProject(app && app.project ? app.project : null);
    } catch (eRememberAfter) {}

    if (normalizedManagedProjectId) {
      var expectedManagedSequenceName =
        normalizedManagedSequenceName ||
        "ATR_BATCH__" + normalizedManagedProjectId;
      var managedResolution = __atrResolveManagedSequence(
        {
          project_id: normalizedManagedProjectId,
          sequence_name: expectedManagedSequenceName,
          local_root: normalizedManagedLocalRoot,
        },
        true,
      );
      if (!managedResolution || managedResolution.status !== "resolved") {
        return (
          "ERROR: Managed import created no resolvable sequence " +
          expectedManagedSequenceName +
          " (" +
          __atrSafeString(
            managedResolution && managedResolution.reason || "not resolved",
          ) +
          ")"
        );
      }
      __atrManagedExportTargetsByProjectId[normalizedManagedProjectId] =
        managedResolution.target;
    }

    // Report the warnings in THIS round-trip. The panel used to fetch them with
    // a second evalScript right after a multi-minute import; that follow-up call
    // can be swallowed once Premiere returns to its event loop, which stranded
    // the whole download/import phase even though the import itself succeeded.
    var warningsText = ATR_getLastImportWarnings();
    // The panel shares one persistent ExtendScript engine across every run.
    // import_project.jsx is a large (~200 KB) script, so reclaim its transient
    // memory after each run to curb the slowdown seen over successive imports.
    try {
      $.gc();
    } catch (eGc) {}
    if (warningsText) {
      return "OK" + ATR_RUN_WARNING_SEPARATOR + warningsText;
    }
    return "OK";
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

/**
 * Return the warnings the last runScript() execution published (empty string
 * when none), then clear them. Generated import_project.jsx scripts set
 * $.global.__ATR_IMPORT_WARNINGS__ for non-fatal issues such as a missing
 * V2 border; older generated scripts never set it.
 *
 * runScript() calls this itself and folds the result into its own return value.
 * It stays exported for panel builds <= v11, which still call it directly; they
 * now get an empty string because runScript() already consumed the warnings.
 */
function ATR_getLastImportWarnings() {
  var warningsText = "";
  try {
    if ($.global.__ATR_IMPORT_WARNINGS__) {
      warningsText = String($.global.__ATR_IMPORT_WARNINGS__);
    }
  } catch (eGetImportWarnings) {}
  try {
    $.global.__ATR_IMPORT_WARNINGS__ = "";
  } catch (eResetImportWarnings) {}
  return warningsText;
}

/**
 * Return the loaded host script build so the panel can reject stale mixed
 * client/host deployments before accepting automated work.
 */
function ATR_getHostBuildId() {
  return ATR_HOST_BUILD_ID;
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

function __atrFindSequenceByIdInProject(projectObject, sequenceId) {
  var targetId = __atrSafeString(sequenceId);
  var sequences = __atrGetProjectSequenceCandidates(projectObject);
  var count = sequences.length;
  for (var i = 0; i < count; i += 1) {
    if (sequences[i] && __atrGetSequenceId(sequences[i]) === targetId) {
      return sequences[i];
    }
  }
  return null;
}

function __atrResolvePreflightedExportTarget(descriptor) {
  var resolvedDescriptor = descriptor || {};
  var projectId = __atrSafeString(resolvedDescriptor.project_id);
  var projectIdentity = __atrSafeString(resolvedDescriptor.project_identity);
  var sequenceId = __atrSafeString(resolvedDescriptor.sequence_id);
  if (!projectId || !projectIdentity || !sequenceId) {
    return null;
  }

  var openProjects = __atrGetOpenProjects();
  var cached = __atrManagedExportTargetsByProjectId[projectId];
  if (
    cached &&
    cached.project &&
    cached.sequence &&
    __atrGetProjectIdentity(cached.project) === projectIdentity &&
    __atrGetSequenceId(cached.sequence) === sequenceId
  ) {
    // The direct Sequence object captured at import/preflight is authoritative.
    // Do not require it to reappear in project.sequences: that collection is
    // the stale-wrapper failure mode this cache exists to survive.
    try {
      if (
        __atrGetSequenceName(cached.sequence) &&
        __atrGetProjectIdentity(cached.project)
      ) {
        return {
          project: cached.project,
          sequence: cached.sequence,
        };
      }
    } catch (eCachedTarget) {}
  }

  if (
    cached &&
    cached.project &&
    __atrProjectIsOpen(cached.project, openProjects) &&
    __atrGetProjectIdentity(cached.project) === projectIdentity
  ) {
    var cachedSequence = __atrFindSequenceByIdInProject(
      cached.project,
      sequenceId,
    );
    if (cachedSequence) {
      return {
        project: cached.project,
        sequence: cachedSequence,
      };
    }
  }

  for (var i = 0; i < openProjects.length; i += 1) {
    if (__atrGetProjectIdentity(openProjects[i]) !== projectIdentity) {
      continue;
    }
    var sequence = __atrFindSequenceByIdInProject(openProjects[i], sequenceId);
    if (sequence) {
      return {
        project: openProjects[i],
        sequence: sequence,
      };
    }
  }
  return null;
}

/**
 * Start managed export via Adobe Media Encoder from a descriptor returned by
 * preflightManagedBatchExport(). The owning Project object and sequence ID are
 * authoritative; app.project may point at another open project.
 *
 * @param {string} exportJson JSON export payload
 * @returns {string} job IDs as JSON or an ERROR message
 */
function startManagedExport(exportJson) {
  try {
    var payload = null;
    try {
      payload =
        typeof exportJson === "string"
          ? JSON.parse(exportJson || "{}")
          : exportJson;
    } catch (ePayloadParse) {
      return "ERROR: Invalid managed export payload";
    }
    var descriptor = payload && payload.resolved_sequence
      ? payload.resolved_sequence
      : null;
    var target = __atrResolvePreflightedExportTarget(descriptor);
    if (!target) {
      return "ERROR: Preflighted project or sequence is no longer open";
    }
    var owningProject = target.project;
    var targetSequence = target.sequence;
    var projectId = __atrSafeString(descriptor.project_id);
    var sequenceId = __atrGetSequenceId(targetSequence);
    if (!owningProject.openSequence) {
      return "ERROR: Owning project cannot activate sequence " + sequenceId;
    }
    var openResult = owningProject.openSequence(sequenceId);
    if (openResult === false) {
      return "ERROR: Owning project failed to activate sequence " + sequenceId;
    }

    var outputPath = __atrSafeString(payload.output_path);
    var presetPath = __atrSafeString(payload.preset_path);
    var audioOptions = payload.audio_export || {};
    var exportAudioNoMusic = !!audioOptions.enabled;
    var audioOutputPath = __atrSafeString(audioOptions.output_path);
    var audioPresetPath = __atrSafeString(audioOptions.preset_path);

    var normalizedPresetPath = __atrNormalizePath(presetPath);
    var presetFile = new File(normalizedPresetPath);
    if (!presetFile.exists) {
      return "ERROR: Preset file not found: " + normalizedPresetPath;
    }
    var presetFsPath = __atrSafeString(
      presetFile.fsName || normalizedPresetPath,
    );

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

    var videoJobID = __atrEncodeSequence(
      targetSequence,
      outputFsPath,
      presetFsPath,
    );
    __atrRememberEncoderJob(
      videoJobID,
      projectId,
      "video",
      outputFsPath,
      presetFsPath,
    );
    __atrPushEncoderEvent("queued", videoJobID, {
      output_path: outputFsPath,
      preset_path: presetFsPath,
      render_kind: "video",
    });

    var audioJobID = "";
    if (exportAudioNoMusic) {
      if (!audioPresetPath) {
        return "ERROR: Missing audio preset path";
      }
      var normalizedAudioPresetPath = __atrNormalizePath(audioPresetPath);
      var audioPresetFile = new File(normalizedAudioPresetPath);
      if (!audioPresetFile.exists) {
        return (
          "ERROR: Audio preset file not found: " + normalizedAudioPresetPath
        );
      }
      var audioPresetFsPath = __atrSafeString(
        audioPresetFile.fsName || normalizedAudioPresetPath,
      );

      var normalizedAudioOutputPath = __atrNormalizePath(audioOutputPath);
      if (!normalizedAudioOutputPath) {
        return "ERROR: Missing audio output path";
      }
      var audioOutFile = new File(normalizedAudioOutputPath);
      var audioOutFolder = audioOutFile.parent;
      if (audioOutFolder && !audioOutFolder.exists) {
        if (!audioOutFolder.create()) {
          return (
            "ERROR: Unable to create audio output folder: " +
            audioOutFolder.fsName
          );
        }
      }
      var audioOutputFsPath = __atrSafeString(
        audioOutFile.fsName || normalizedAudioOutputPath,
      );

      var tempSequence = __atrCloneSequence(targetSequence, owningProject);
      var tempSequenceName =
        __atrTempAudioSequencePrefix +
        __atrSafeString(projectId) +
        "__" +
        new Date().getTime();
      try {
        tempSequence.name = tempSequenceName;
      } catch (eRename) {
        tempSequenceName = __atrGetSequenceName(tempSequence);
      }
      __atrRemoveAllTrackClips(tempSequence.videoTracks);
      __atrRemoveTrackByIndex(tempSequence.audioTracks, 2);

      audioJobID = __atrEncodeSequence(
        tempSequence,
        audioOutputFsPath,
        audioPresetFsPath,
      );
      __atrRememberEncoderJob(
        audioJobID,
        projectId,
        "audio_no_music",
        audioOutputFsPath,
        audioPresetFsPath,
      );
      __atrTempAudioSequenceByJob[audioJobID] = {
        name: __atrSafeString(tempSequenceName),
        sequence_id: __atrGetSequenceId(tempSequence),
        project: owningProject,
      };
      __atrPushEncoderEvent("queued", audioJobID, {
        output_path: audioOutputFsPath,
        preset_path: audioPresetFsPath,
        render_kind: "audio_no_music",
      });
    }

    if (app.encoder && app.encoder.startBatch) {
      try {
        app.encoder.startBatch();
      } catch (eStartBatch) {
        return (
          "ERROR: Failed to start AME batch: " +
          __atrSafeString(eStartBatch.message || eStartBatch)
        );
      }
    }

    return JSON.stringify({
      video_job_id: __atrSafeString(videoJobID),
      audio_job_id: __atrSafeString(audioJobID),
      audio_enabled: !!exportAudioNoMusic,
    });
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function __atrEncodeProjectItem(projectItem, outputFsPath, presetFsPath) {
  var jobID = null;
  try {
    jobID = app.encoder.encodeProjectItem(
      projectItem,
      outputFsPath,
      presetFsPath,
      0,
      0,
    );
  } catch (eFive) {
    try {
      jobID = app.encoder.encodeProjectItem(
        projectItem,
        outputFsPath,
        presetFsPath,
        0,
      );
    } catch (eFour) {
      jobID = app.encoder.encodeProjectItem(
        projectItem,
        outputFsPath,
        presetFsPath,
      );
    }
  }

  if (!jobID && jobID !== 0) {
    throw new Error("encodeProjectItem returned an empty job ID");
  }
  return jobID;
}

function reconcileAtrProjectProxies(localRootPath, proxyPlanJson) {
  try {
    if (!app || !app.project) {
      return "ERROR: No active Premiere project";
    }

    var normalizedRootPath = __atrNormalizePath(localRootPath);
    if (!normalizedRootPath) {
      return "ERROR: Missing local project root";
    }

    var plan = __atrParseProxyPlan(proxyPlanJson);
    var result = {
      ok: true,
      attached: 0,
      already_compliant: 0,
      pending: 0,
      missing_items: 0,
      missing_outputs: 0,
      attach_pending: 0,
      unverified_attach_count: 0,
      eligible_items: 0,
      ignored_items: 0,
      errors: [],
      attach_pending_errors: [],
      completed_targets: 0,
      total_targets: 0,
      repair_queued: 0,
      job_ids: [],
    };
    if (!plan.enabled || !plan.targets.length) {
      return JSON.stringify(result);
    }

    // One project/timeline scan shared by every target in this invocation.
    var mediaIndex = null;
    function getMediaIndex() {
      if (!mediaIndex) {
        mediaIndex = __atrBuildMediaPathLeafIndex();
      }
      return mediaIndex;
    }

    for (var i = 0; i < plan.targets.length; i += 1) {
      var target = plan.targets[i];
      if (!target || !target.needs_proxy) {
        continue;
      }
      result.total_targets += 1;

      var desiredProxyPath = __atrComputeManagedProxyOutputPath(
        normalizedRootPath,
        target,
      );
      var repairProxyPath = __atrComputeManagedProxyRepairOutputPath(
        normalizedRootPath,
        target,
      );
      var repairProxyFile = new File(repairProxyPath);
      var desiredProxyFile = new File(desiredProxyPath);
      if (!desiredProxyFile.exists && !repairProxyFile.exists) {
        result.pending += 1;
        result.missing_outputs += 1;
        continue;
      }

      try {
        var attachResult = null;
        if (repairProxyFile.exists) {
          var repairProxyFsPath = __atrSafeString(
            repairProxyFile.fsName || repairProxyPath,
          );
          attachResult = __atrAttachProxyToMatchingItems(
            plan.project_id,
            target.media_path,
            repairProxyFsPath,
            normalizedRootPath,
            getMediaIndex(),
          );
        }

        if (
          (!attachResult || !attachResult.proxy_attached) &&
          desiredProxyFile.exists
        ) {
          var desiredProxyFsPath = __atrSafeString(
            desiredProxyFile.fsName || desiredProxyPath,
          );
          attachResult = __atrAttachProxyToMatchingItems(
            plan.project_id,
            target.media_path,
            desiredProxyFsPath,
            normalizedRootPath,
            getMediaIndex(),
          );
        }

        if (!attachResult) {
          result.pending += 1;
          continue;
        }

        result.attached += Number(attachResult.attached_count || 0);
        result.already_compliant += Number(
          attachResult.already_compliant_count || 0,
        );
        result.eligible_items += Number(attachResult.eligible_count || 0);
        result.ignored_items += Number(attachResult.ignored_count || 0);
        if (Number(attachResult.item_count || 0) <= 0) {
          result.missing_items += 1;
        }
        if (attachResult.proxy_attached) {
          result.completed_targets += 1;
          continue;
        }
        if (attachResult.proxy_attach_pending) {
          result.pending += 1;
          result.attach_pending += Math.max(
            1,
            Number(attachResult.attach_pending_count || 0),
          );
          result.unverified_attach_count += Number(
            attachResult.unverified_attach_count || 0,
          );
          if (attachResult.proxy_attach_error) {
            result.attach_pending_errors.push(
              target.media_path +
                ": " +
                __atrSafeString(attachResult.proxy_attach_error),
            );
          }
        } else {
          result.ok = false;
          if (attachResult.proxy_attach_error) {
            result.errors.push(
              "Failed to attach proxy for " +
                target.media_path +
                ": " +
                __atrSafeString(attachResult.proxy_attach_error),
            );
          }
        }
      } catch (eAttach) {
        result.errors.push(
          "Failed to attach proxy for " +
            target.media_path +
            ": " +
            __atrSafeString(eAttach.message || eAttach),
        );
        result.ok = false;
        result.pending += 1;
        result.attach_pending += 1;
      }
    }

    if (
      plan.auto_enable_proxy_view &&
      (result.attached > 0 || result.already_compliant > 0) &&
      app.setEnableProxies
    ) {
      try {
        app.setEnableProxies(1);
      } catch (eEnableProxyView) {}
    }

    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function __atrClearTrackedProxyState() {
  var canceledJobs = 0;
  for (var metaJobID in __atrEncoderJobMetaMap) {
    if (
      __atrEncoderJobMetaMap.hasOwnProperty(metaJobID) &&
      __atrEncoderJobMetaMap[metaJobID] &&
      __atrEncoderJobMetaMap[metaJobID].render_kind === "proxy"
    ) {
      canceledJobs += 1;
      try {
        delete __atrEncoderJobProjectMap[metaJobID];
      } catch (eDeleteProject2) {}
      try {
        delete __atrEncoderJobMetaMap[metaJobID];
      } catch (eDeleteMeta2) {}
    }
  }

  __atrProxyAttachAttemptMap = {};

  return {
    canceled_proxy_jobs: canceledJobs,
    canceled_scheduled_kickoffs: 0,
  };
}

function __atrBuildAmeClearQueueScript() {
  return [
    "(function(){",
    "function s(v){try{return String(v||'');}catch(e){return '';}}",
    "function q(v){return '\"'+s(v).replace(/\\\\/g,'\\\\\\\\').replace(/\"/g,'\\\\\"').replace(/\\r/g,'\\\\r').replace(/\\n/g,'\\\\n')+'\"';}",
    "function a(values){var out=[];for(var i=0;i<values.length;i++){out.push(q(values[i]));}return '['+out.join(',')+']';}",
    "function out(r){if(typeof JSON!=='undefined'&&JSON.stringify){return JSON.stringify(r);}return '{\"ok\":'+(r.ok?'true':'false')+',\"stopped\":'+(r.stopped?'true':'false')+',\"cleared_queue\":'+(r.cleared_queue?'true':'false')+',\"errors\":'+a(r.errors)+',\"warnings\":'+a(r.warnings)+'}';}",
    "var r={ok:true,stopped:false,cleared_queue:false,errors:[],warnings:[]};",
    "try{var h=app&&app.getEncoderHost?app.getEncoderHost():null;if(h&&h.stopBatch){h.stopBatch();r.stopped=true;}}catch(eStopHost){r.warnings.push('AME encoderHost stopBatch failed: '+s(eStopHost.message||eStopHost));}",
    "try{if(!r.stopped){var f=app&&app.getFrontend?app.getFrontend():null;if(f&&f.stopBatch){var stopped=f.stopBatch();r.stopped=(stopped===undefined)||!!stopped;}}if(!r.stopped){r.warnings.push('AME stopBatch unavailable or returned false');}}catch(eStop){r.ok=false;r.errors.push('AME stopBatch failed: '+s(eStop.message||eStop));}",
    "try{if(r.stopped&&app&&app.wait){app.wait(500);}}catch(eWait){}",
    "try{var ex=app&&app.getExporter?app.getExporter():null;if(ex&&ex.removeAllBatchItems){var cleared=ex.removeAllBatchItems();r.cleared_queue=(cleared===undefined)||!!cleared;if(!r.cleared_queue){r.ok=false;r.errors.push('AME removeAllBatchItems returned false');}}else{r.ok=false;r.errors.push('AME removeAllBatchItems unavailable');}}catch(eClear){r.ok=false;r.errors.push('AME removeAllBatchItems failed: '+s(eClear.message||eClear));}",
    "return out(r);",
    "}())",
  ].join("");
}

function __atrResolveAmeBridgeTalkTarget() {
  var candidates = ["ame"];
  if (typeof BridgeTalk === "undefined") {
    return candidates;
  }
  try {
    if (BridgeTalk.getTargets) {
      var targets = BridgeTalk.getTargets();
      for (var i = 0; targets && i < targets.length; i += 1) {
        var target = __atrSafeString(targets[i]).toLowerCase();
        if (target.indexOf("ame") === 0 && candidates.join("|").indexOf(target) === -1) {
          candidates.push(target);
        }
      }
    }
  } catch (eTargets) {}
  try {
    if (BridgeTalk.getSpecifier) {
      var specifier = __atrSafeString(BridgeTalk.getSpecifier("ame"));
      if (specifier && candidates.join("|").indexOf(specifier.toLowerCase()) === -1) {
        candidates.push(specifier);
      }
    }
  } catch (eSpecifier) {}
  return candidates;
}

function __atrBridgeTalkTargetIsRunning(targets) {
  if (typeof BridgeTalk === "undefined" || !BridgeTalk.isRunning) {
    return false;
  }
  for (var i = 0; targets && i < targets.length; i += 1) {
    try {
      if (BridgeTalk.isRunning(targets[i])) {
        return true;
      }
    } catch (eRunning) {}
  }
  return false;
}

function __atrEnsureAmeRunning(result) {
  if (typeof BridgeTalk === "undefined") {
    result.errors.push("BridgeTalk is unavailable");
    return false;
  }

  var targets = __atrResolveAmeBridgeTalkTarget();
  var alreadyRunning = false;
  try {
    alreadyRunning = __atrBridgeTalkTargetIsRunning(targets);
  } catch (eRunningCheck) {
    alreadyRunning = false;
  }

  if (!alreadyRunning) {
    var launched = false;
    try {
      if (typeof app !== "undefined" && app && app.encoder && app.encoder.launchEncoder) {
        app.encoder.launchEncoder();
        launched = true;
      }
    } catch (eLaunchEncoder) {
      result.warnings.push(
        "app.encoder.launchEncoder failed: " +
          __atrSafeString(eLaunchEncoder.message || eLaunchEncoder),
      );
    }
    if (!launched) {
      try {
        if (BridgeTalk.launch) {
          BridgeTalk.launch("ame");
          launched = true;
        }
      } catch (eLaunchBt) {
        result.warnings.push(
          "BridgeTalk.launch('ame') failed: " +
            __atrSafeString(eLaunchBt.message || eLaunchBt),
        );
      }
    }
  }

  // Each pass blocks Premiere's ExtendScript engine (and UI); keep it short
  // and let the panel retry the whole clear instead of one 90s host wait.
  var deadline = new Date().getTime() + 20000;
  while (new Date().getTime() < deadline) {
    if (__atrBridgeTalkTargetIsRunning(targets)) {
      return true;
    }
    try {
      if (BridgeTalk.pump) {
        BridgeTalk.pump();
      }
    } catch (ePumpLaunch) {}
    try {
      $.sleep(250);
    } catch (eSleepLaunch) {}
  }

  result.errors.push("Adobe Media Encoder did not start within 20 seconds");
  return false;
}

function __atrClearAmeQueueThroughBridgeTalk() {
  var result = {
    ok: false,
    stopped: false,
    cleared_queue: false,
    errors: [],
    warnings: [],
  };

  if (typeof BridgeTalk === "undefined") {
    result.errors.push("BridgeTalk is unavailable");
    return result;
  }

  if (!__atrEnsureAmeRunning(result)) {
    return result;
  }

  var done = false;
  var responseText = "";
  var targets = __atrResolveAmeBridgeTalkTarget();
  var sent = false;
  var sendErrors = [];
  try {
    var sendDeadline = new Date().getTime() + 10000;
    while (!sent && new Date().getTime() < sendDeadline) {
      for (var targetIndex = 0; targetIndex < targets.length && !sent; targetIndex += 1) {
        var bt = new BridgeTalk();
        bt.target = targets[targetIndex];
        bt.body = __atrBuildAmeClearQueueScript();
        bt.onResult = function (response) {
          responseText = __atrSafeString(response && response.body);
          done = true;
        };
        bt.onError = function (error) {
          result.errors.push(
            "BridgeTalk AME clear failed: " +
              __atrSafeString(error && (error.body || error.message || error)),
          );
          done = true;
        };
        try {
          sent = !!bt.send(60);
          if (!sent) {
            sendErrors.push("send returned false for " + targets[targetIndex]);
          }
        } catch (eSendAttempt) {
          sendErrors.push(
            targets[targetIndex] +
              ": " +
              __atrSafeString(eSendAttempt.message || eSendAttempt),
          );
        }
      }
      if (!sent) {
        try {
          if (BridgeTalk.pump) {
            BridgeTalk.pump();
          }
        } catch (ePumpSend) {}
        try {
          $.sleep(500);
        } catch (eSleepSend) {}
      }
    }
    if (!sent) {
      result.errors.push(
        "BridgeTalk could not send AME clear request" +
          (sendErrors.length > 0 ? ": " + sendErrors.slice(0, 4).join(" | ") : ""),
      );
      return result;
    }
  } catch (eSend) {
    result.errors.push(
      "BridgeTalk AME clear crashed: " + __atrSafeString(eSend.message || eSend),
    );
    return result;
  }

  var deadline = new Date().getTime() + 10000;
  while (!done && new Date().getTime() < deadline) {
    try {
      if (BridgeTalk.pump) {
        BridgeTalk.pump();
      }
    } catch (ePump) {}
    try {
      $.sleep(100);
    } catch (eSleep) {}
  }

  if (!done) {
    result.errors.push("Timed out while clearing Adobe Media Encoder queue");
    return result;
  }

  if (!responseText) {
    if (result.errors.length <= 0) {
      result.errors.push("Adobe Media Encoder returned an empty clear response");
    }
    return result;
  }

  try {
    var parsed = JSON.parse(responseText);
    result.ok = !!parsed.ok;
    result.stopped = !!parsed.stopped;
    result.cleared_queue = !!parsed.cleared_queue;
    result.errors = parsed.errors && parsed.errors.length ? parsed.errors : [];
    result.warnings =
      parsed.warnings && parsed.warnings.length ? parsed.warnings : [];
  } catch (eParse) {
    result.errors.push(
      "Invalid Adobe Media Encoder clear response: " + responseText,
    );
  }
  return result;
}

function cancelAtrProxyRenderingAndClearMediaEncoder() {
  var response = {
    ok: true,
    canceled_proxy_jobs: 0,
    canceled_scheduled_kickoffs: 0,
    cleared_queue: false,
    errors: [],
    warnings: [],
  };

  var localClear = __atrClearTrackedProxyState();
  response.canceled_proxy_jobs = Number(localClear.canceled_proxy_jobs || 0);
  response.canceled_scheduled_kickoffs = Number(
    localClear.canceled_scheduled_kickoffs || 0,
  );

  var clearResult = __atrClearAmeQueueThroughBridgeTalk();
  response.cleared_queue = !!clearResult.cleared_queue;
  response.warnings = clearResult.warnings || [];
  response.errors = clearResult.errors || [];
  if (!clearResult.ok || !clearResult.cleared_queue) {
    response.ok = false;
    if (response.errors.length <= 0) {
      response.errors.push("Adobe Media Encoder queue was not cleared");
    }
    return JSON.stringify(response);
  }

  return JSON.stringify(response);
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
    return JSON.stringify(events);
  } catch (e) {
    return "[]";
  }
}
