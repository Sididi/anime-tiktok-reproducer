/**
 * Tiktok Reproducer - ExtendScript Host
 *
 * Runs in Premiere Pro's ExtendScript engine.
 * Called from the CEP panel via csInterface.evalScript().
 */

var ATR_EXTENSION_ID = "com.animetiktok.tiktokreproducer.panel";
// Must stay in sync with ATR_BUILD_ID in client/constants.js.
var ATR_HOST_BUILD_ID = "2026-07-06-panel-cleanup-v9";
var __atrEncoderEvents = [];
var __atrEncoderJobProjectMap = {};
var __atrEncoderJobMetaMap = {};
var __atrTempAudioSequenceByJob = {};
var __atrProxyAttachAttemptMap = {};
var __atrEncoderCallbacksBound = false;
var __atrTempAudioSequencePrefix = "ATR_AUDIO_NO_MUSIC_TMP__";
var __atrProjectPurgeBinName = "__ATR_PURGE__";
var __atrProxyOutputSuffix = "__atr_proxy.mp4";
var __atrProxyRepairOutputSuffix = "__atr_proxy_projectitem.mp4";

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
    if (mediaPath && __atrPathStartsWith(mediaPath, normalizedRootPath)) {
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

function __atrBuildImportedCleanupScan(normalizedRootPath) {
  var report = {
    imported_leaf_items: [],
    deletable_bins: [],
  };

  if (!app || !app.project || !app.project.rootItem) {
    return report;
  }

  __atrInspectImportedSubtree(
    app.project.rootItem,
    normalizedRootPath,
    0,
    report,
  );

  report.deletable_bins.sort(function (a, b) {
    return Number(b.depth || 0) - Number(a.depth || 0);
  });

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

function __atrDetachManagedProxiesForCleanupObject(localRootPath) {
  var normalizedRootPath = __atrNormalizeComparePath(localRootPath);
  var result = {
    ok: true,
    considered_proxy_items: 0,
    detached_proxy_count: 0,
    detach_proxy_unavailable_count: 0,
    detach_proxy_failed_count: 0,
    detach_proxy_warnings: [],
  };

  if (!normalizedRootPath || !app || !app.project || !app.project.rootItem) {
    return result;
  }

  __atrCloseSourceMonitorForCleanup(result);

  try {
    if (app.setEnableProxies) {
      app.setEnableProxies(0);
    }
  } catch (eDisableProxyView) {
    result.detach_proxy_warnings.push(
      "Could not disable proxy view: " +
        __atrSafeString(eDisableProxyView.message || eDisableProxyView),
    );
  }

  var scan = __atrBuildImportedCleanupScan(normalizedRootPath);
  for (var i = 0; i < scan.imported_leaf_items.length; i += 1) {
    var item = scan.imported_leaf_items[i];
    if (!item || !item.hasProxy || !item.getProxyPath) {
      continue;
    }

    var proxyPath = "";
    try {
      if (!item.hasProxy()) {
        continue;
      }
      proxyPath = __atrSafeString(item.getProxyPath());
    } catch (eProxyState) {
      continue;
    }

    // The entire local root is about to be deleted from disk, so any proxy
    // whose file lives under it will disappear. Detach every such proxy - not
    // only the ones matching the managed-name heuristic - because if the
    // attachment survives, Premiere keeps the proxy registered and raises a
    // blocking "link missing proxies" modal the moment the file is removed.
    if (
      !__atrPathStartsWith(proxyPath, normalizedRootPath) &&
      !__atrLooksLikeManagedProxyPath(proxyPath, normalizedRootPath)
    ) {
      continue;
    }

    result.considered_proxy_items += 1;
    if (!item.detachProxy) {
      result.detach_proxy_unavailable_count += 1;
      continue;
    }

    // detachProxy() returns before Premiere commits the change on Windows, so
    // trusting its return value reports a false success: purge then deletes the
    // item and the folder delete removes the proxy file while Premiere still
    // has it attached, which queues the "link missing proxies" modal and holds
    // the media file handles (leaving sources/proxies on disk). Confirm the
    // proxy is actually gone via hasProxy(), mirroring the verified attach path.
    var detachCommitted = false;
    var lastDetachError = null;
    for (
      var detachAttempt = 0;
      detachAttempt < 4 && !detachCommitted;
      detachAttempt += 1
    ) {
      try {
        item.detachProxy();
      } catch (eDetach) {
        lastDetachError = eDetach;
      }
      try {
        if (item.refreshMedia) {
          item.refreshMedia();
        }
      } catch (eRefreshDetach) {}
      if (detachAttempt > 0) {
        try {
          $.sleep(200);
        } catch (eSleepDetach) {}
      }
      try {
        detachCommitted = !item.hasProxy();
      } catch (eHasProxyAfter) {
        // Item can no longer be queried for proxy state; treat as detached.
        detachCommitted = true;
      }
    }

    if (detachCommitted) {
      result.detached_proxy_count += 1;
    } else {
      result.detach_proxy_failed_count += 1;
      if (result.detach_proxy_warnings.length < 5) {
        result.detach_proxy_warnings.push(
          "Proxy still attached after detach for " +
            __atrGetProjectItemName(item) +
            (lastDetachError
              ? ": " +
                __atrSafeString(lastDetachError.message || lastDetachError)
              : ""),
        );
      }
    }
  }

  if (result.detach_proxy_unavailable_count > 0) {
    result.detach_proxy_warnings.push(
      "projectItem.detachProxy is unavailable for " +
        result.detach_proxy_unavailable_count +
        " managed proxy item(s); project purge will release them.",
    );
  }

  result.ok = result.detach_proxy_failed_count === 0;
  return result;
}

function __atrSetImportedMediaOfflineForCleanupObject(localRootPath) {
  var normalizedRootPath = __atrNormalizeComparePath(localRootPath);
  var result = {
    ok: true,
    considered_media_items: 0,
    media_offline_count: 0,
    media_offline_unavailable_count: 0,
    media_offline_failed_count: 0,
    media_release_warnings: [],
  };

  if (!normalizedRootPath || !app || !app.project || !app.project.rootItem) {
    return result;
  }

  __atrCloseSourceMonitorForCleanup(result);

  var scan = __atrBuildImportedCleanupScan(normalizedRootPath);
  for (var i = 0; i < scan.imported_leaf_items.length; i += 1) {
    var item = scan.imported_leaf_items[i];
    if (!item) {
      continue;
    }
    result.considered_media_items += 1;
    if (!item.setOffline) {
      result.media_offline_unavailable_count += 1;
      continue;
    }

    try {
      if (item.isOffline && item.isOffline()) {
        result.media_offline_count += 1;
        continue;
      }
    } catch (eOfflineState) {}

    try {
      var offlineResult = item.setOffline();
      if (
        offlineResult === undefined ||
        offlineResult === 0 ||
        offlineResult === true ||
        offlineResult === "0"
      ) {
        result.media_offline_count += 1;
      } else {
        result.media_offline_failed_count += 1;
      }
    } catch (eSetOffline) {
      result.media_offline_failed_count += 1;
      if (result.media_release_warnings.length < 5) {
        result.media_release_warnings.push(
          "Could not make media offline for " +
            __atrGetProjectItemName(item) +
            ": " +
            __atrSafeString(eSetOffline.message || eSetOffline),
        );
      }
    }
  }

  if (result.media_offline_unavailable_count > 0) {
    result.media_release_warnings.push(
      "projectItem.setOffline is unavailable for " +
        result.media_offline_unavailable_count +
        " imported item(s); project purge will be used instead.",
    );
  }

  result.ok = result.media_offline_failed_count === 0;
  return result;
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
        missing: [],
        error: "No batch sequences to preflight",
      });
    }

    var missing = [];
    var found = 0;
    for (var i = 0; i < entries.length; i += 1) {
      var entry = entries[i] || {};
      var sequenceName = __atrSafeString(entry.sequence_name);
      var projectId = __atrSafeString(entry.project_id);
      if (!sequenceName || !__atrFindSequenceByName(sequenceName)) {
        missing.push({
          project_id: projectId,
          sequence_name: sequenceName,
        });
      } else {
        found += 1;
      }
    }

    return JSON.stringify({
      ok: missing.length === 0,
      checked: entries.length,
      found: found,
      missing: missing,
    });
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function __atrCloneSequence(sourceSequence) {
  var sequences = app && app.project ? app.project.sequences : null;
  if (!sequences) {
    throw new Error("No sequence collection available");
  }

  var beforeCount = 0;
  var beforeKeys = __atrCaptureSequenceKeys();
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

function __atrCaptureSequenceKeys() {
  var out = {};
  var sequences = app && app.project ? app.project.sequences : null;
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

  var sequences = app && app.project ? app.project.sequences : null;
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
        return !!sequence.projectItem.deleteBin();
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

function __atrGetSequenceCount() {
  var sequences = app && app.project ? app.project.sequences : null;
  if (!sequences) {
    return 0;
  }

  try {
    return Number(sequences.numSequences || 0);
  } catch (eCount) {
    return 0;
  }
}

function __atrGetRootChildCount(rootItem) {
  if (!rootItem || !rootItem.children) {
    return 0;
  }

  try {
    return Number(rootItem.children.numItems || 0);
  } catch (eCount) {
    return 0;
  }
}

function __atrDeleteSequenceObject(sequence) {
  if (!sequence || !app || !app.project) {
    return false;
  }

  var deleted = false;
  try {
    if (app.project.deleteSequence && sequence.sequenceID !== undefined) {
      deleted = !!app.project.deleteSequence(sequence.sequenceID);
    }
  } catch (eDeleteById) {}

  if (!deleted) {
    try {
      if (app.project.deleteSequence) {
        deleted = !!app.project.deleteSequence(sequence);
      }
    } catch (eDeleteByObject) {}
  }

  if (!deleted) {
    try {
      if (sequence.projectItem && sequence.projectItem.deleteBin) {
        deleted = !!sequence.projectItem.deleteBin();
      }
    } catch (eDeleteBin) {}
  }

  if (!deleted) {
    try {
      if (
        sequence.projectItem &&
        sequence.projectItem.select &&
        app.project.deleteSelection
      ) {
        sequence.projectItem.select();
        deleted = !!app.project.deleteSelection();
      }
    } catch (eDeleteSelection) {}
  }

  return deleted;
}

function purgeActiveProject() {
  try {
    if (!app || !app.project || !app.project.rootItem) {
      return "ERROR: No active Premiere project is available";
    }

    var root = app.project.rootItem;
    var warnings = [];
    var sequencesDeleted = 0;
    var sequencesFailed = 0;
    var movedToPurgeBin = 0;
    var moveFailures = 0;
    var movePasses = 0;
    var purgeBinDeleted = false;
    var purgeBinCreateFailed = false;
    var purgeBinDeleteFailed = false;

    for (var s = __atrGetSequenceCount() - 1; s >= 0; s -= 1) {
      var sequence = app.project.sequences[s];
      if (!sequence) {
        continue;
      }
      if (__atrDeleteSequenceObject(sequence)) {
        sequencesDeleted += 1;
      } else {
        sequencesFailed += 1;
        warnings.push(
          "Could not delete sequence '" + __atrGetSequenceName(sequence) + "'.",
        );
      }
    }

    var remainingRootItemsBeforePurge = __atrGetRootChildCount(root);
    if (remainingRootItemsBeforePurge > 0) {
      var purgeBin = null;
      try {
        purgeBin = root.createBin(__atrProjectPurgeBinName);
      } catch (eCreate) {
        purgeBin = null;
      }

      if (!purgeBin) {
        purgeBinCreateFailed = true;
      } else {
        var moveGuard = 0;
        while (__atrGetRootChildCount(root) > 1 && moveGuard < 10000) {
          moveGuard += 1;
          movePasses += 1;
          var movedInPass = false;

          for (var i = __atrGetRootChildCount(root) - 1; i >= 0; i -= 1) {
            var child = root.children[i];
            if (!child || child === purgeBin) {
              continue;
            }

            try {
              child.moveBin(purgeBin);
              movedToPurgeBin += 1;
              movedInPass = true;
            } catch (eMove) {
              moveFailures += 1;
              warnings.push(
                "Could not move item '" +
                  __atrSafeString(child.name || "item#" + i) +
                  "' into purge bin.",
              );
            }
          }

          if (!movedInPass) {
            break;
          }
        }

        if (moveGuard >= 10000) {
          warnings.push("Purge guard reached while moving project items.");
        }

        try {
          purgeBinDeleted = !!purgeBin.deleteBin();
        } catch (eDeleteBin0) {
          purgeBinDeleted = false;
        }
        if (!purgeBinDeleted) {
          try {
            if (app.project.deleteBin) {
              app.project.deleteBin(purgeBin);
              purgeBinDeleted = true;
            }
          } catch (eDeleteBin1) {
            purgeBinDeleted = false;
          }
        }
        if (!purgeBinDeleted) {
          purgeBinDeleteFailed = true;
        }
      }
    }

    var remainingSequences = __atrGetSequenceCount();
    var remainingRootItems = __atrGetRootChildCount(root);
    var result = {
      ok: remainingSequences === 0 && remainingRootItems === 0,
      sequences_deleted: sequencesDeleted,
      sequences_failed: sequencesFailed,
      items_moved_to_purge_bin: movedToPurgeBin,
      move_failures: moveFailures,
      move_passes: movePasses,
      remaining_sequences: remainingSequences,
      remaining_root_items: remainingRootItems,
      purge_bin_create_failed: purgeBinCreateFailed,
      purge_bin_delete_failed: purgeBinDeleteFailed,
      warning_count: warnings.length,
      warnings: warnings,
    };

    if (!result.ok) {
      if (purgeBinCreateFailed) {
        result.error =
          "Could not create purge bin '" + __atrProjectPurgeBinName + "'.";
      } else if (purgeBinDeleteFailed) {
        result.error =
          "Could not delete purge bin '" + __atrProjectPurgeBinName + "'.";
      } else {
        result.error = "Premiere project purge incomplete";
      }
    }

    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function cleanupImportedProjectsForLocalRoots(localRootsJson) {
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
    if (!roots || !roots.length) {
      roots = [];
    }

    var detachSummaries = [];
    var mediaReleaseSummaries = [];
    var detachedProxyCount = 0;
    var detachFailedCount = 0;
    var mediaOfflineCount = 0;
    var detachWarnings = [];
    var mediaReleaseWarnings = [];
    for (var i = 0; i < roots.length; i += 1) {
      var rootPath = __atrSafeString(roots[i]);
      if (!rootPath) {
        continue;
      }
      var detachSummary = __atrDetachManagedProxiesForCleanupObject(rootPath);
      detachSummary.local_root = rootPath;
      detachSummaries.push(detachSummary);
      detachedProxyCount += Number(detachSummary.detached_proxy_count || 0);
      detachFailedCount += Number(detachSummary.detach_proxy_failed_count || 0);
      if (detachSummary.detach_proxy_warnings && detachSummary.detach_proxy_warnings.length) {
        for (var w = 0; w < detachSummary.detach_proxy_warnings.length; w += 1) {
          if (detachWarnings.length < 10) {
            detachWarnings.push(detachSummary.detach_proxy_warnings[w]);
          }
        }
      }

      var mediaReleaseSummary =
        __atrSetImportedMediaOfflineForCleanupObject(rootPath);
      mediaReleaseSummary.local_root = rootPath;
      mediaReleaseSummaries.push(mediaReleaseSummary);
      mediaOfflineCount += Number(mediaReleaseSummary.media_offline_count || 0);
      if (
        mediaReleaseSummary.media_release_warnings &&
        mediaReleaseSummary.media_release_warnings.length
      ) {
        for (
          var mw = 0;
          mw < mediaReleaseSummary.media_release_warnings.length;
          mw += 1
        ) {
          if (mediaReleaseWarnings.length < 10) {
            mediaReleaseWarnings.push(
              mediaReleaseSummary.media_release_warnings[mw],
            );
          }
        }
      }
    }

    try {
      __atrCloseSourceMonitorForCleanup(null);
    } catch (eCloseBeforePurge) {}
    try {
      if ($ && $.sleep) {
        $.sleep(300);
      }
    } catch (eSleepBeforePurge) {}

    var purgeRaw = purgeActiveProject();
    if (purgeRaw && String(purgeRaw).indexOf("ERROR:") === 0) {
      return purgeRaw;
    }
    var purgeSummary = {};
    try {
      purgeSummary = JSON.parse(purgeRaw || "{}");
    } catch (ePurgeParse) {
      purgeSummary = {
        ok: true,
        raw: __atrSafeString(purgeRaw),
      };
    }

    try {
      __atrCloseSourceMonitorForCleanup(null);
    } catch (eCloseAfterPurge) {}
    try {
      if ($ && $.sleep) {
        $.sleep(300);
      }
    } catch (eSleepAfterPurge) {}

    purgeSummary.detach_proxy_summaries = detachSummaries;
    purgeSummary.media_release_summaries = mediaReleaseSummaries;
    purgeSummary.detached_proxy_count = detachedProxyCount;
    purgeSummary.detach_failed_count = detachFailedCount;
    purgeSummary.media_offline_count = mediaOfflineCount;
    purgeSummary.detach_proxy_warnings = detachWarnings;
    purgeSummary.media_release_warnings = mediaReleaseWarnings;
    purgeSummary.warning_count =
      Number(purgeSummary.warning_count || 0) +
      detachWarnings.length +
      mediaReleaseWarnings.length;
    if (detachWarnings.length > 0 || mediaReleaseWarnings.length > 0) {
      if (!purgeSummary.warnings) {
        purgeSummary.warnings = [];
      }
      for (var j = 0; j < detachWarnings.length; j += 1) {
        purgeSummary.warnings.push(detachWarnings[j]);
      }
      for (var k = 0; k < mediaReleaseWarnings.length; k += 1) {
        purgeSummary.warnings.push(mediaReleaseWarnings[k]);
      }
    }
    return JSON.stringify(purgeSummary);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
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
    // The panel shares one persistent ExtendScript engine across every run.
    // import_project.jsx is a large (~200 KB) script, so reclaim its transient
    // memory after each run to curb the slowdown seen over successive imports.
    try {
      $.gc();
    } catch (eGc) {}
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
 * @param {string} sequenceName
 * @param {string} outputPath
 * @param {string} presetPath
 * @returns {string} jobID or ERROR message
 */
function startManagedExport(projectId, sequenceName, outputPath, presetPath) {
  try {
    if (!app || !app.project) {
      return "ERROR: No active Premiere project";
    }

    var targetSequenceName = __atrSafeString(sequenceName);
    if (!targetSequenceName) {
      return "ERROR: Missing sequence name";
    }
    var targetSequence = __atrFindSequenceByName(targetSequenceName);
    if (!targetSequence) {
      return "ERROR: Sequence not found: " + targetSequenceName;
    }

    var exportAudioNoMusic =
      Number(arguments.length >= 5 ? arguments[4] : 0) === 1;
    var audioOutputPath =
      arguments.length >= 6 ? __atrSafeString(arguments[5]) : "";
    var audioPresetPath =
      arguments.length >= 7 ? __atrSafeString(arguments[6]) : "";

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

      var tempSequence = __atrCloneSequence(targetSequence);
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

  __atrPushHostTrace(
    "",
    "Canceled proxy rendering and cleared Adobe Media Encoder queue",
    "info",
    response,
  );
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

