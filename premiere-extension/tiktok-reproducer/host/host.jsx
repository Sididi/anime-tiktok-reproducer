/**
 * Tiktok Reproducer - ExtendScript Host
 *
 * Runs in Premiere Pro's ExtendScript engine.
 * Called from the CEP panel via csInterface.evalScript().
 */

var ATR_EXTENSION_ID = "com.animetiktok.tiktokreproducer.panel";
var ATR_HOST_BUILD_ID = "2026-04-29-async-proxy-v8";
var __atrEncoderEvents = [];
var __atrEncoderJobProjectMap = {};
var __atrEncoderJobMetaMap = {};
var __atrTempAudioSequenceByJob = {};
var __atrProxyJobMap = {};
var __atrProxyRepairQueuedMap = {};
var __atrProxyRepairJobKeyMap = {};
var __atrPendingProxyKickoffs = {};
var __atrProxyPresetPathCache = {};
var __atrProxyAttachAttemptMap = {};
var __atrEncoderCallbacksBound = false;
var __atrCleanupMaxBinPasses = 5;
var __atrTempAudioSequencePrefix = "ATR_AUDIO_NO_MUSIC_TMP__";
var __atrProjectPurgeBinName = "__ATR_PURGE__";
var __atrProxyPresetTemplateName = "ATR Proxy H264.epr";
var __atrProxyOutputSuffix = "__atr_proxy.mp4";
var __atrProxyRepairOutputSuffix = "__atr_proxy_projectitem.mp4";
var __atrTicksPerSecond = 254016000000;
var __atrProxyKickoffSeq = 0;

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

function __atrGetExtensionRootPath() {
  try {
    return __atrNormalizePath(new File($.fileName).parent.parent.fsName);
  } catch (e) {
    return "";
  }
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

function __atrEnsureFolder(folderPath) {
  if (!folderPath) {
    return false;
  }
  var folder = new Folder(folderPath);
  if (folder.exists) {
    return true;
  }
  var parent = folder.parent;
  if (parent && !parent.exists && !__atrEnsureFolder(parent.fsName)) {
    return false;
  }
  return !!folder.create();
}

function __atrReadTextFile(filePath) {
  if (!filePath) {
    return "";
  }
  var file = new File(filePath);
  if (!file.exists || !file.open("r")) {
    return "";
  }
  var content = "";
  try {
    content = file.read();
  } catch (e) {
    content = "";
  }
  file.close();
  return content;
}

function __atrWriteTextFile(filePath, content) {
  if (!filePath) {
    return false;
  }
  var parentPath = __atrGetParentPath(filePath);
  if (parentPath && !__atrEnsureFolder(parentPath)) {
    return false;
  }
  var file = new File(filePath);
  if (!file.open("w")) {
    return false;
  }
  try {
    file.encoding = "UTF8";
    file.write(content || "");
  } catch (e) {
    try {
      file.close();
    } catch (eClose0) {}
    return false;
  }
  file.close();
  return true;
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
  var itemName = __atrSafeString(nameValue).toLowerCase();
  var baseName = __atrGetBasename(mediaPath).toLowerCase();
  var strippedBaseName = __atrStripExtension(baseName).toLowerCase();
  var strippedItemName = __atrStripExtension(itemName).toLowerCase();
  return !!(
    itemName &&
    baseName &&
    (itemName === baseName ||
      strippedItemName === strippedBaseName ||
      itemName.indexOf(baseName) !== -1 ||
      itemName.indexOf(strippedBaseName) !== -1)
  );
}

function __atrProjectItemNameMatchesMediaPath(projectItem, mediaPath) {
  return __atrNameMatchesMediaPath(__atrGetProjectItemName(projectItem), mediaPath);
}

function __atrGetSequenceCount() {
  try {
    return Number(app && app.project && app.project.sequences ? app.project.sequences.numSequences || app.project.sequences.length || 0 : 0);
  } catch (eSeqCount) {
    return 0;
  }
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

function __atrFindTimelineVideoProjectItemsByMediaPath(mediaPath, items, seen) {
  var wanted = __atrNormalizeComparePath(mediaPath);
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
        var itemMediaPath = __atrGetProjectItemMediaPath(projectItem);
        var clipName = "";
        try {
          clipName = __atrSafeString(clip && clip.name ? clip.name : "");
        } catch (eClipName) {
          clipName = "";
        }
        if (
          itemMediaPath === wanted ||
          __atrProjectItemNameMatchesMediaPath(projectItem, mediaPath) ||
          __atrNameMatchesMediaPath(clipName, mediaPath)
        ) {
          __atrPushUniqueProjectItem(items, seen, projectItem);
        }
      }
    }
  }
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

function __atrFindProjectItemsByMediaPath(mediaPath) {
  var root = app && app.project ? app.project.rootItem : null;
  var normalizedMediaPath = __atrNormalizePath(mediaPath);
  var wanted = __atrNormalizeComparePath(normalizedMediaPath);
  var found = [];
  var seen = {};
  if (!root || !wanted) {
    return found;
  }

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

  __atrWalkProjectItems(root, function (item) {
    if (__atrGetProjectItemChildCount(item) > 0) {
      return;
    }
    if (__atrGetProjectItemMediaPath(item) === wanted) {
      __atrPushUniqueProjectItem(found, seen, item);
    }
  });

  __atrFindTimelineVideoProjectItemsByMediaPath(normalizedMediaPath, found, seen);

  __atrWalkProjectItems(root, function (itemByName) {
    if (__atrGetProjectItemChildCount(itemByName) > 0) {
      return;
    }
    if (__atrProjectItemNameMatchesMediaPath(itemByName, normalizedMediaPath)) {
      __atrPushUniqueProjectItem(found, seen, itemByName);
    }
  });

  return found;
}

function __atrFindProjectItemByMediaPath(mediaPath) {
  var items = __atrFindProjectItemsByMediaPath(mediaPath);
  return items.length > 0 ? items[0] : null;
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

function __atrEscapeRegex(value) {
  return __atrSafeString(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function __atrReplaceExporterParamValue(xmlText, paramId, nextValue) {
  var blockPattern = /<ExporterParam\b[\s\S]*?<\/ExporterParam>/g;
  var identifierPattern = new RegExp(
    "<ParamIdentifier>" + __atrEscapeRegex(paramId) + "</ParamIdentifier>",
  );
  var replaced = false;

  return __atrSafeString(xmlText).replace(blockPattern, function (blockText) {
    if (replaced || !identifierPattern.test(blockText)) {
      return blockText;
    }
    replaced = true;
    return blockText.replace(
      /<ParamValue>[^<]*<\/ParamValue>/,
      "<ParamValue>" + __atrSafeString(nextValue) + "</ParamValue>",
    );
  });
}

function __atrReplaceExporterParamTag(xmlText, paramId, tagName, nextValue) {
  var blockPattern = /<ExporterParam\b[\s\S]*?<\/ExporterParam>/g;
  var identifierPattern = new RegExp(
    "<ParamIdentifier>" + __atrEscapeRegex(paramId) + "</ParamIdentifier>",
  );
  var tagPattern = new RegExp(
    "<" + __atrEscapeRegex(tagName) + ">[^<]*</" + __atrEscapeRegex(tagName) + ">",
  );
  var replaced = false;

  return __atrSafeString(xmlText).replace(blockPattern, function (blockText) {
    if (replaced || !identifierPattern.test(blockText)) {
      return blockText;
    }
    replaced = true;
    return blockText.replace(
      tagPattern,
      "<" +
        __atrSafeString(tagName) +
        ">" +
        __atrSafeString(nextValue) +
        "</" +
        __atrSafeString(tagName) +
        ">",
    );
  });
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

function __atrResolveProxyPresetTemplatePath(localRootPath, explicitTemplatePath) {
  var candidates = [
    __atrNormalizePath(explicitTemplatePath),
    __atrJoinPath(localRootPath, "assets/" + __atrProxyPresetTemplateName),
    __atrJoinPath(
      __atrGetExtensionRootPath(),
      "assets/" + __atrProxyPresetTemplateName,
    ),
    __atrJoinPath(
      __atrNormalizePath(new File($.fileName).parent.fsName),
      "../assets/" + __atrProxyPresetTemplateName,
    ),
  ];

  for (var i = 0; i < candidates.length; i += 1) {
    var candidatePath = __atrNormalizePath(candidates[i]);
    if (!candidatePath) {
      continue;
    }
    var candidateFile = new File(candidatePath);
    if (candidateFile.exists) {
      return {
        path: candidatePath,
        error: "",
      };
    }
  }

  var checkedCandidates = [];
  for (var j = 0; j < candidates.length; j += 1) {
    if (candidates[j]) {
      checkedCandidates.push(__atrNormalizePath(candidates[j]));
    }
  }

  return {
    path: "",
    error:
      "Proxy preset template not found in bundle or extension assets (" +
      __atrProxyPresetTemplateName +
      "). Checked: " +
      checkedCandidates.join(" | "),
  };
}

function __atrBuildProxyPresetPath(localRootPath, target, explicitTemplatePath) {
  var width = Math.max(
    2,
    Number(target && target.expected_proxy_width || 0) || 480,
  );
  var height = Math.max(
    2,
    Number(target && target.expected_proxy_height || 0) || 270,
  );
  var fps = Number(target && target.source_fps || 0);
  if (!fps || fps <= 0) {
    fps = 23.976;
  }
  var fpsTicks = Math.max(1, Math.round(__atrTicksPerSecond / fps));
  var audioChannels = Math.max(
    0,
    Number(target && target.source_audio_channels || 0),
  );
  var audioSampleRate = Math.max(
    0,
    Number(target && target.source_audio_sample_rate || 0),
  );
  var audioStreamCount = Math.max(
    0,
    Number(target && target.source_audio_stream_count || 0),
  );
  var hasAudio = audioStreamCount > 0 && audioChannels > 0;
  var audioBitrate = audioChannels >= 6 ? "256" : audioChannels > 2 ? "192" : "96";
  var audioKey = hasAudio
    ? "a" + audioChannels + "_" + (audioSampleRate || 0)
    : "noaudio";
  var cacheKey = [
    __atrNormalizeComparePath(localRootPath),
    width,
    height,
    fpsTicks,
    audioStreamCount,
    audioChannels,
    audioSampleRate,
  ].join("x");
  if (__atrProxyPresetPathCache[cacheKey]) {
    return {
      path: __atrProxyPresetPathCache[cacheKey],
      error: "",
    };
  }

  var templateResult = __atrResolveProxyPresetTemplatePath(
    localRootPath,
    explicitTemplatePath,
  );
  if (!templateResult.path) {
    return {
      path: "",
      error: templateResult.error,
    };
  }

  var templateXml = __atrReadTextFile(templateResult.path);
  if (!templateXml) {
    return {
      path: "",
      error: "Proxy preset template is unreadable: " + templateResult.path,
    };
  }

  var presetXml = templateXml;
  presetXml = presetXml.replace(
    /<PresetName>[^<]*<\/PresetName>/,
    "<PresetName>ATR Proxy H264 " +
      width +
      "x" +
      height +
      " " +
      audioKey +
      "</PresetName>",
  );
  presetXml = presetXml.replace(
    /<DoAudio>[^<]*<\/DoAudio>/,
    "<DoAudio>" + (hasAudio ? "true" : "false") + "</DoAudio>",
  );
  presetXml = presetXml.replace(
    /<UseMaximumRenderQuality>[^<]*<\/UseMaximumRenderQuality>/,
    "<UseMaximumRenderQuality>false</UseMaximumRenderQuality>",
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEVideoWidth",
    width,
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEVideoHeight",
    height,
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEVideoFPS",
    fpsTicks,
  );
  presetXml = __atrReplaceExporterParamTag(
    presetXml,
    "ADBEVideoWidth",
    "ParamIsDisabled",
    "false",
  );
  presetXml = __atrReplaceExporterParamTag(
    presetXml,
    "ADBEVideoHeight",
    "ParamIsDisabled",
    "false",
  );
  presetXml = __atrReplaceExporterParamTag(
    presetXml,
    "ADBEVideoFPS",
    "ParamIsDisabled",
    "false",
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEVideoMinBitrate",
    "0.4",
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEVideoTargetBitrate",
    "1.2",
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEVideoMaxBitrate",
    "2.0",
  );
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEAudioBitrate",
    audioBitrate,
  );
  if (audioChannels > 0) {
    presetXml = __atrReplaceExporterParamValue(
      presetXml,
      "ADBEAudioNumChannels",
      String(audioChannels),
    );
  }
  if (audioSampleRate > 0) {
    presetXml = __atrReplaceExporterParamValue(
      presetXml,
      "ADBEAudioRatePerSecond",
      String(audioSampleRate),
    );
  }
  presetXml = __atrReplaceExporterParamValue(
    presetXml,
    "ADBEMPEGKeyframeRate",
    "30",
  );

  var presetPath = __atrJoinPath(
    localRootPath,
    "proxies/__atr_proxy_presets/atr_proxy_" +
      width +
      "x" +
      height +
      "_" +
      fpsTicks +
      "_" +
      audioKey +
      ".epr",
  );
  if (!__atrWriteTextFile(presetPath, presetXml)) {
    return {
      path: "",
      error: "Failed to write generated proxy preset: " + presetPath,
    };
  }
  __atrProxyPresetPathCache[cacheKey] = presetPath;
  return {
    path: presetPath,
    error: "",
  };
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

    if (!__atrLooksLikeManagedProxyPath(proxyPath, normalizedRootPath)) {
      continue;
    }

    result.considered_proxy_items += 1;
    if (!item.detachProxy) {
      result.detach_proxy_unavailable_count += 1;
      continue;
    }

    try {
      var detached = item.detachProxy();
      if (detached === undefined || detached === 0 || detached === true || detached === "0") {
        result.detached_proxy_count += 1;
      } else {
        result.detach_proxy_failed_count += 1;
      }
    } catch (eDetach) {
      result.detach_proxy_failed_count += 1;
      if (result.detach_proxy_warnings.length < 5) {
        result.detach_proxy_warnings.push(
          "Could not detach proxy from " +
            __atrGetProjectItemName(item) +
            ": " +
            __atrSafeString(eDetach.message || eDetach),
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

function detachManagedProxiesForCleanup(localRootPath) {
  try {
    return JSON.stringify(__atrDetachManagedProxiesForCleanupObject(localRootPath));
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
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
  if (
    !projectItem ||
    !projectItem.select ||
    !app ||
    !app.project ||
    !app.project.deleteSelection
  ) {
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

  while (
    passesExecuted < __atrCleanupMaxBinPasses &&
    scan.deletable_bins.length > 0
  ) {
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
    for (
      var leafIndex = scan.imported_leaf_items.length - 1;
      leafIndex >= 0;
      leafIndex -= 1
    ) {
      var leafItem = scan.imported_leaf_items[leafIndex];
      if (__atrDeleteLeafProjectItem(leafItem)) {
        leafDeleted += 1;
      } else {
        failed += 1;
      }
    }

    if (fallbackBins.length > 0) {
      for (
        var retryIndex = 0;
        retryIndex < fallbackBins.length;
        retryIndex += 1
      ) {
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
    project_items_considered:
      Number(scan.imported_leaf_items.length || 0) + leafDeleted,
    passes_executed: passesExecuted,
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
      sequences: 0,
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

    var trackGroups = [sequence.videoTracks, sequence.audioTracks];

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
          if (
            !clipMediaPath ||
            !__atrPathStartsWith(clipMediaPath, normalizedRootPath)
          ) {
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
    sequences: sequenceCount,
  };
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

function __atrPushProxySummary(projectId, summary) {
  var payload = summary || {};
  payload.project_id = __atrSafeString(projectId);
  payload.host_build_id = ATR_HOST_BUILD_ID;
  __atrPushEncoderEvent("proxy_summary", "", payload);
}

function __atrEscapeForCodeString(value) {
  return __atrSafeString(value)
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n");
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

function __atrRememberProxyJob(jobID, projectId, mediaPath, proxyPath, localRootPath, autoEnableProxyView) {
  __atrProxyJobMap[jobID] = {
    project_id: __atrSafeString(projectId),
    media_path: __atrNormalizePath(mediaPath),
    proxy_path: __atrNormalizePath(proxyPath),
    local_root_path: __atrNormalizePath(localRootPath),
    auto_enable_proxy_view: !!autoEnableProxyView,
  };
}

function __atrAttachProxyToMatchingItems(projectId, mediaPath, proxyPath, localRootPath) {
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
  var items = __atrFindProjectItemsByMediaPath(mediaPath);
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

function __atrAttachGeneratedProxyForJob(jobID, outputPath) {
  var job = __atrProxyJobMap[jobID] || null;
  var response = {
    proxy_attached: false,
    proxy_attach_pending: false,
    proxy_attach_error: "",
    media_path: "",
    output_path: __atrSafeString(outputPath),
    attached_count: 0,
    eligible_count: 0,
    already_compliant_count: 0,
    attach_pending_count: 0,
    unverified_attach_count: 0,
  };
  if (!job) {
    return response;
  }

  response.media_path = __atrSafeString(job.media_path);
  var proxyPath = __atrNormalizePath(outputPath || job.proxy_path);
  var proxyFile = new File(proxyPath);
  var proxyFsPath = __atrSafeString(proxyFile.fsName || proxyPath);

  try {
    var attachAttempt = __atrAttachProxyToMatchingItems(
      job.project_id,
      job.media_path,
      proxyFsPath,
      job.local_root_path,
    );
    response.proxy_attached = !!attachAttempt.proxy_attached;
    response.proxy_attach_pending = !!attachAttempt.proxy_attach_pending;
    response.proxy_attach_error = __atrSafeString(
      attachAttempt.proxy_attach_error,
    );
    response.attached_count = Number(attachAttempt.attached_count || 0);
    response.eligible_count = Number(attachAttempt.eligible_count || 0);
    response.ignored_count = Number(attachAttempt.ignored_count || 0);
    response.already_compliant_count = Number(
      attachAttempt.already_compliant_count || 0,
    );
    response.attach_pending_count = Number(attachAttempt.attach_pending_count || 0);
    response.unverified_attach_count = Number(
      attachAttempt.unverified_attach_count || 0,
    );
  } catch (eAttach) {
    response.proxy_attached = false;
    response.proxy_attach_pending = true;
    response.proxy_attach_error = __atrSafeString(eAttach.message || eAttach);
  }

  __atrPushHostTrace(
    job.project_id,
    response.proxy_attached
      ? "Proxy attached for " + __atrGetBasename(proxyFsPath)
      : "Proxy attach failed for " +
          __atrGetBasename(proxyFsPath) +
          (response.proxy_attach_error
            ? ": " + response.proxy_attach_error
            : ""),
    response.proxy_attached
      ? "info"
      : response.proxy_attach_pending
        ? "info"
        : "warn",
  );

  if (
    response.proxy_attached &&
    job.auto_enable_proxy_view &&
    app &&
    app.setEnableProxies
  ) {
    try {
      app.setEnableProxies(1);
    } catch (eEnableProxyView) {}
  }

  if (!response.proxy_attached && !response.proxy_attach_error) {
    response.proxy_attach_error = "attachProxy returned false";
  }
  return response;
}

function __atrBuildProxyRepairKey(projectId, mediaPath) {
  return (
    __atrSafeString(projectId) +
    "::" +
    __atrNormalizeComparePath(mediaPath)
  );
}

function __atrFindProxyCapableProjectItemByMediaPath(mediaPath) {
  var items = __atrFindProjectItemsByMediaPath(mediaPath);
  for (var i = 0; i < items.length; i += 1) {
    var item = items[i];
    if (
      item &&
      !__atrIsAtrRawAudioProjectItem(item) &&
      __atrProjectItemCanAcceptProxy(item)
    ) {
      return item;
    }
  }
  return null;
}

function __atrResolveRepairOutputPath(baseRepairPath) {
  var normalizedRepairPath = __atrNormalizePath(baseRepairPath);
  var repairFile = new File(normalizedRepairPath);
  if (!repairFile.exists) {
    return normalizedRepairPath;
  }
  try {
    if (repairFile.remove()) {
      return normalizedRepairPath;
    }
  } catch (eRemoveRepair) {}

  var parentPath = __atrGetParentPath(normalizedRepairPath);
  var baseName = __atrStripExtension(__atrGetBasename(normalizedRepairPath));
  return __atrJoinPath(
    parentPath,
    baseName + "_" + new Date().getTime() + ".mp4",
  );
}

function __atrQueueProjectItemProxyRepair(
  projectId,
  target,
  normalizedRootPath,
  proxyPresetTemplatePath,
  autoEnableProxyView,
) {
  var response = {
    ok: false,
    queued: false,
    job_id: "",
    output_path: "",
    error: "",
  };
  var repairKey = __atrBuildProxyRepairKey(projectId, target && target.media_path);
  if (__atrProxyRepairQueuedMap[repairKey]) {
    response.ok = true;
    return response;
  }

  if (!app || !app.encoder) {
    response.error = "app.encoder is unavailable for proxy repair";
    return response;
  }

  var projectItem = __atrFindProxyCapableProjectItemByMediaPath(target.media_path);
  if (!projectItem) {
    response.error =
      "No proxy-capable imported project item found for " + target.media_path;
    return response;
  }

  var presetResult = __atrBuildProxyPresetPath(
    normalizedRootPath,
    target,
    proxyPresetTemplatePath,
  );
  var presetPath = presetResult && presetResult.path ? presetResult.path : "";
  if (!presetPath) {
    response.error =
      "Failed to build proxy repair preset for " +
      target.media_path +
      (presetResult && presetResult.error ? " (" + presetResult.error + ")" : "");
    return response;
  }

  var repairPath = __atrResolveRepairOutputPath(
    __atrComputeManagedProxyRepairOutputPath(normalizedRootPath, target),
  );
  var repairFolderPath = __atrGetParentPath(repairPath);
  if (repairFolderPath && !__atrEnsureFolder(repairFolderPath)) {
    response.error = "Failed to create proxy repair folder for " + repairPath;
    return response;
  }

  __atrPushHostTrace(
    projectId,
    "Queueing ProjectItem proxy repair for " + __atrGetBasename(target.media_path),
    "info",
  );

  try {
    app.encoder.launchEncoder();
    if (!__atrBindEncoderCallbacks()) {
      response.error = "Unable to bind AME encoder callbacks for proxy repair";
      return response;
    }

    var repairFile = new File(repairPath);
    var repairFsPath = __atrSafeString(repairFile.fsName || repairPath);
    var presetFile = new File(presetPath);
    var presetFsPath = __atrSafeString(presetFile.fsName || presetPath);
    var jobID = __atrEncodeProjectItem(projectItem, repairFsPath, presetFsPath);
    __atrRememberEncoderJob(
      jobID,
      projectId || __atrSafeString(app.project ? app.project.name || "" : ""),
      "proxy",
      repairFsPath,
      presetFsPath,
    );
    __atrRememberProxyJob(
      jobID,
      projectId || __atrSafeString(app.project ? app.project.name || "" : ""),
      target.media_path,
      repairPath,
      normalizedRootPath,
      autoEnableProxyView,
    );
    __atrProxyJobMap[jobID].proxy_repair = true;
    __atrProxyJobMap[jobID].repair_key = repairKey;
    __atrProxyRepairQueuedMap[repairKey] = true;
    __atrProxyRepairJobKeyMap[jobID] = repairKey;

    if (app.encoder.startBatch) {
      app.encoder.startBatch();
    }

    response.ok = true;
    response.queued = true;
    response.job_id = __atrSafeString(jobID);
    response.output_path = repairFsPath;
    __atrPushHostTrace(
      projectId,
      "Queued ProjectItem proxy repair job " + response.job_id,
      "info",
    );
  } catch (eRepair) {
    response.error = __atrSafeString(eRepair.message || eRepair);
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
  try {
    delete __atrProxyJobMap[jobID];
  } catch (e3) {}
  try {
    delete __atrProxyRepairJobKeyMap[jobID];
  } catch (e4) {}
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
  var meta = __atrEncoderJobMetaMap[jobID] || null;
  if (meta && meta.render_kind === "proxy") {
    var proxyAttach = __atrAttachGeneratedProxyForJob(jobID, outputPath);
    detail.proxy_attached = !!proxyAttach.proxy_attached;
    detail.proxy_attach_pending = !!proxyAttach.proxy_attach_pending;
    detail.proxy_attach_error = __atrSafeString(proxyAttach.proxy_attach_error);
    detail.media_path = __atrSafeString(proxyAttach.media_path);
    detail.attached_count = Number(proxyAttach.attached_count || 0);
    detail.eligible_count = Number(proxyAttach.eligible_count || 0);
    detail.already_compliant_count = Number(
      proxyAttach.already_compliant_count || 0,
    );
    detail.ignored_count = Number(proxyAttach.ignored_count || 0);
    detail.attach_pending_count = Number(proxyAttach.attach_pending_count || 0);
    detail.unverified_attach_count = Number(
      proxyAttach.unverified_attach_count || 0,
    );
  }
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

function __atrEncodeFile(filePath, outputFsPath, presetFsPath) {
  var normalizedFilePath = __atrNormalizePath(filePath);
  var sourceFile = new File(normalizedFilePath);
  var sourceFsPath = __atrSafeString(sourceFile.fsName || normalizedFilePath);
  var jobID = null;
  try {
    jobID = app.encoder.encodeFile(sourceFsPath, outputFsPath, presetFsPath, 0, 0);
  } catch (eFive) {
    try {
      jobID = app.encoder.encodeFile(sourceFsPath, outputFsPath, presetFsPath, 0);
    } catch (eFour) {
      jobID = app.encoder.encodeFile(sourceFsPath, outputFsPath, presetFsPath);
    }
  }

  if (!jobID && jobID !== 0) {
    throw new Error("encodeFile returned an empty job ID");
  }
  return jobID;
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

function __atrRunScheduledProxyKickoff(kickoffId) {
  var normalizedKickoffId = __atrSafeString(kickoffId);
  var payload = __atrPendingProxyKickoffs[normalizedKickoffId] || null;
  if (!payload) {
    return;
  }

  try {
    delete __atrPendingProxyKickoffs[normalizedKickoffId];
  } catch (eDeleteKickoff) {}

  var plan = __atrParseProxyPlan(payload.proxyPlanJson);
  var projectId =
    plan.project_id || __atrSafeString(app && app.project ? app.project.name : "");
  __atrPushHostTrace(
    projectId,
    "Proxy kickoff started (" + normalizedKickoffId + ")",
    "info",
  );

  var result = ensureAtrProjectProxies(
    payload.localRootPath,
    payload.proxyPlanJson,
    payload.proxyPresetTemplatePath,
  );

  if (result && String(result).indexOf("ERROR:") === 0) {
    __atrPushHostTrace(projectId, "Proxy kickoff failed: " + result, "error");
    __atrPushProxySummary(projectId, {
      ok: false,
      queued: 0,
      attached: 0,
      skipped_h264: 0,
      already_compliant: 0,
      replaced_noncompliant: 0,
      errors: [String(result)],
      job_ids: [],
    });
    return;
  }

  var parsed = {};
  try {
    parsed = JSON.parse(result || "{}");
  } catch (eParse) {
    parsed = {
      ok: false,
      queued: 0,
      attached: 0,
      skipped_h264: 0,
      already_compliant: 0,
      replaced_noncompliant: 0,
      errors: ["Invalid proxy kickoff result: " + __atrSafeString(result)],
      job_ids: [],
    };
  }

  __atrPushProxySummary(projectId, parsed);
  __atrPushHostTrace(
    projectId,
    "Proxy kickoff finished: queued=" +
      Number(parsed.queued || 0) +
      ", attached=" +
      Number(parsed.attached || 0) +
      ", errors=" +
      (parsed.errors && parsed.errors.length ? parsed.errors.length : 0),
    parsed.ok === false ? "warn" : "info",
  );
}

function __atrRunScheduledProxyEncoding(kickoffId) {
  var normalizedKickoffId = __atrSafeString(kickoffId);
  var payload = __atrPendingProxyKickoffs[normalizedKickoffId] || null;
  if (!payload) {
    return;
  }

  try {
    delete __atrPendingProxyKickoffs[normalizedKickoffId];
  } catch (eDeleteKickoff) {}

  var plan = __atrParseProxyPlan(payload.proxyPlanJson);
  var projectId =
    plan.project_id || __atrSafeString(app && app.project ? app.project.name : "");
  __atrPushHostTrace(
    projectId,
    "Pre-import proxy encoding kickoff started (" + normalizedKickoffId + ")",
    "info",
  );

  var result = queueAtrProjectProxyEncoding(
    payload.localRootPath,
    payload.proxyPlanJson,
    payload.proxyPresetTemplatePath,
  );

  if (result && String(result).indexOf("ERROR:") === 0) {
    __atrPushHostTrace(projectId, "Proxy encoding kickoff failed: " + result, "error");
    __atrPushProxySummary(projectId, {
      ok: false,
      queued: 0,
      skipped_h264: 0,
      existing_outputs: 0,
      errors: [String(result)],
      job_ids: [],
    });
    return;
  }

  var parsed = {};
  try {
    parsed = JSON.parse(result || "{}");
  } catch (eParse) {
    parsed = {
      ok: false,
      queued: 0,
      skipped_h264: 0,
      existing_outputs: 0,
      errors: ["Invalid proxy encoding kickoff result: " + __atrSafeString(result)],
      job_ids: [],
    };
  }

  __atrPushProxySummary(projectId, parsed);
  __atrPushHostTrace(
    projectId,
    "Pre-import proxy encoding kickoff finished: queued=" +
      Number(parsed.queued || 0) +
      ", existing=" +
      Number(parsed.existing_outputs || 0) +
      ", errors=" +
      (parsed.errors && parsed.errors.length ? parsed.errors.length : 0),
    parsed.ok === false ? "warn" : "info",
  );
}

function scheduleAtrProjectProxyEncoding(localRootPath, proxyPlanJson, proxyPresetTemplatePath) {
  try {
    var plan = __atrParseProxyPlan(proxyPlanJson);
    var projectId =
      plan.project_id || __atrSafeString(app && app.project ? app.project.name : "");
    var targetCount = plan.targets ? plan.targets.length : 0;

    if (!plan.enabled || !targetCount) {
      return JSON.stringify({
        ok: true,
        scheduled: false,
        project_id: projectId,
        target_count: targetCount,
      });
    }

    __atrPushHostTrace(
      projectId,
      "Scheduling pre-import proxy encoding for " + targetCount + " target(s)",
      "info",
    );

    if (app && app.scheduleTask) {
      __atrProxyKickoffSeq += 1;
      var kickoffId =
        "atr_proxy_encode_" + new Date().getTime() + "_" + __atrProxyKickoffSeq;
      __atrPendingProxyKickoffs[kickoffId] = {
        localRootPath: __atrSafeString(localRootPath),
        proxyPlanJson: proxyPlanJson,
        proxyPresetTemplatePath: __atrSafeString(proxyPresetTemplatePath),
      };
      app.scheduleTask(
        '__atrRunScheduledProxyEncoding("' +
          __atrEscapeForCodeString(kickoffId) +
          '")',
        50,
        false,
      );
      __atrPushHostTrace(
        projectId,
        "Pre-import proxy encoding scheduled (" + kickoffId + ")",
        "info",
      );
      return JSON.stringify({
        ok: true,
        scheduled: true,
        project_id: projectId,
        kickoff_id: kickoffId,
        target_count: targetCount,
      });
    }

    __atrPushHostTrace(
      projectId,
      "app.scheduleTask unavailable; running pre-import proxy encoding inline",
      "warn",
    );
    var inlineResult = queueAtrProjectProxyEncoding(
      localRootPath,
      proxyPlanJson,
      proxyPresetTemplatePath,
    );
    if (inlineResult && String(inlineResult).indexOf("ERROR:") === 0) {
      return inlineResult;
    }
    try {
      __atrPushProxySummary(projectId, JSON.parse(inlineResult || "{}"));
    } catch (eInlineParse) {}
    return inlineResult;
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function queueAtrProjectProxyEncoding(localRootPath, proxyPlanJson, proxyPresetTemplatePath) {
  try {
    var normalizedRootPath = __atrNormalizePath(localRootPath);
    if (!normalizedRootPath) {
      return "ERROR: Missing local project root";
    }

    var plan = __atrParseProxyPlan(proxyPlanJson);
    var result = {
      ok: true,
      queued: 0,
      skipped_h264: 0,
      existing_outputs: 0,
      errors: [],
      job_ids: [],
    };
    if (plan.ffprobe_warning) {
      result.ffprobe_warning = plan.ffprobe_warning;
    }
    if (!plan.enabled || !plan.targets.length) {
      return JSON.stringify(result);
    }

    __atrPushHostTrace(
      plan.project_id,
      "Proxy pre-import queue entered for " + plan.targets.length + " target(s)",
      "info",
    );

    if (!app || !app.encoder) {
      return "ERROR: app.encoder is unavailable";
    }

    __atrPushHostTrace(plan.project_id, "Launching Adobe Media Encoder", "info");
    app.encoder.launchEncoder();
    __atrPushHostTrace(plan.project_id, "Binding AME encoder callbacks", "info");
    if (!__atrBindEncoderCallbacks()) {
      return "ERROR: Unable to bind AME encoder callbacks";
    }

    for (var i = 0; i < plan.targets.length; i += 1) {
      var target = plan.targets[i];
      if (!target) {
        continue;
      }

      if (!target.needs_proxy) {
        result.skipped_h264 += 1;
        continue;
      }

      __atrPushHostTrace(
        plan.project_id,
        "Proxy queue target " +
          (i + 1) +
          "/" +
          plan.targets.length +
          ": " +
          __atrGetBasename(target.media_path),
        "info",
      );

      var desiredProxyPath = __atrComputeManagedProxyOutputPath(
        normalizedRootPath,
        target,
      );
      var desiredProxyFile = new File(desiredProxyPath);
      if (desiredProxyFile.exists) {
        result.existing_outputs += 1;
        continue;
      }

      var presetResult = __atrBuildProxyPresetPath(
        normalizedRootPath,
        target,
        proxyPresetTemplatePath,
      );
      var presetPath = presetResult && presetResult.path ? presetResult.path : "";
      if (!presetPath) {
        result.errors.push(
          "Failed to build proxy preset for " +
            target.media_path +
            (presetResult && presetResult.error
              ? " (" + presetResult.error + ")"
              : ""),
        );
        result.ok = false;
        continue;
      }

      var proxyFolderPath = __atrGetParentPath(desiredProxyPath);
      if (proxyFolderPath && !__atrEnsureFolder(proxyFolderPath)) {
        result.errors.push(
          "Failed to create proxy folder for " + desiredProxyPath,
        );
        result.ok = false;
        continue;
      }

      __atrPushHostTrace(
        plan.project_id,
        "Queueing proxy encode for " + __atrGetBasename(target.media_path),
        "info",
      );
      var desiredProxyOutputFile = new File(desiredProxyPath);
      var desiredProxyOutputFsPath = __atrSafeString(
        desiredProxyOutputFile.fsName || desiredProxyPath,
      );
      var presetFile = new File(presetPath);
      var presetFsPath = __atrSafeString(presetFile.fsName || presetPath);
      var jobID = __atrEncodeFile(
        target.media_path,
        desiredProxyOutputFsPath,
        presetFsPath,
      );
      __atrRememberEncoderJob(
        jobID,
        plan.project_id || __atrSafeString(app.project ? app.project.name : ""),
        "proxy",
        desiredProxyOutputFsPath,
        presetFsPath,
      );
      __atrRememberProxyJob(
        jobID,
        plan.project_id || __atrSafeString(app.project ? app.project.name : ""),
        target.media_path,
        desiredProxyPath,
        normalizedRootPath,
        plan.auto_enable_proxy_view,
      );
      __atrPushHostTrace(
        plan.project_id,
        "Queued proxy encode job " + __atrSafeString(jobID),
        "info",
      );
      result.queued += 1;
      result.job_ids.push(__atrSafeString(jobID));
    }

    if (result.queued > 0 && app.encoder && app.encoder.startBatch) {
      __atrPushHostTrace(
        plan.project_id,
        "Starting AME batch for " + result.queued + " proxy job(s)",
        "info",
      );
      try {
        app.encoder.startBatch();
      } catch (eStartBatchQueued) {
        result.ok = false;
        result.errors.push(
          "Failed to start AME batch: " +
            __atrSafeString(eStartBatchQueued.message || eStartBatchQueued),
        );
        __atrPushHostTrace(
          plan.project_id,
          "Failed to start AME batch: " +
            __atrSafeString(eStartBatchQueued.message || eStartBatchQueued),
          "error",
        );
      }
    }

    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function scheduleAtrProjectProxies(localRootPath, proxyPlanJson, proxyPresetTemplatePath) {
  try {
    var plan = __atrParseProxyPlan(proxyPlanJson);
    var projectId =
      plan.project_id || __atrSafeString(app && app.project ? app.project.name : "");
    var targetCount = plan.targets ? plan.targets.length : 0;

    if (!plan.enabled || !targetCount) {
      return JSON.stringify({
        ok: true,
        scheduled: false,
        project_id: projectId,
        target_count: targetCount,
      });
    }

    __atrPushHostTrace(
      projectId,
      "Scheduling proxy kickoff for " + targetCount + " target(s)",
      "info",
    );

    if (app && app.scheduleTask) {
      __atrProxyKickoffSeq += 1;
      var kickoffId =
        "atr_proxy_" + new Date().getTime() + "_" + __atrProxyKickoffSeq;
      __atrPendingProxyKickoffs[kickoffId] = {
        localRootPath: __atrSafeString(localRootPath),
        proxyPlanJson: proxyPlanJson,
        proxyPresetTemplatePath: __atrSafeString(proxyPresetTemplatePath),
      };
      app.scheduleTask(
        '__atrRunScheduledProxyKickoff("' +
          __atrEscapeForCodeString(kickoffId) +
          '")',
        50,
        false,
      );
      __atrPushHostTrace(
        projectId,
        "Proxy kickoff scheduled (" + kickoffId + ")",
        "info",
      );
      return JSON.stringify({
        ok: true,
        scheduled: true,
        project_id: projectId,
        kickoff_id: kickoffId,
        target_count: targetCount,
      });
    }

    __atrPushHostTrace(
      projectId,
      "app.scheduleTask unavailable; running proxy kickoff inline",
      "warn",
    );
    return ensureAtrProjectProxies(
      localRootPath,
      proxyPlanJson,
      proxyPresetTemplatePath,
    );
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}

function ensureAtrProjectProxies(localRootPath, proxyPlanJson, proxyPresetTemplatePath) {
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
      queued: 0,
      attached: 0,
      skipped_h264: 0,
      already_compliant: 0,
      replaced_noncompliant: 0,
      errors: [],
      job_ids: [],
    };
    if (plan.ffprobe_warning) {
      result.ffprobe_warning = plan.ffprobe_warning;
    }
    if (!plan.enabled || !plan.targets.length) {
      return JSON.stringify(result);
    }

    __atrPushHostTrace(
      plan.project_id,
      "Proxy audit entered for " + plan.targets.length + " target(s)",
      "info",
    );

    if (!app.encoder) {
      return "ERROR: app.encoder is unavailable";
    }

    __atrPushHostTrace(plan.project_id, "Launching Adobe Media Encoder", "info");
    app.encoder.launchEncoder();
    __atrPushHostTrace(plan.project_id, "Binding AME encoder callbacks", "info");
    if (!__atrBindEncoderCallbacks()) {
      return "ERROR: Unable to bind AME encoder callbacks";
    }

    for (var i = 0; i < plan.targets.length; i += 1) {
      var target = plan.targets[i];
      if (!target) {
        continue;
      }

      if (!target.needs_proxy) {
        result.skipped_h264 += 1;
        continue;
      }

      __atrPushHostTrace(
        plan.project_id,
        "Proxy audit target " +
          (i + 1) +
          "/" +
          plan.targets.length +
          ": " +
          __atrGetBasename(target.media_path),
        "info",
      );

      var projectItem = __atrFindProjectItemByMediaPath(target.media_path);
      if (!projectItem) {
        result.errors.push(
          "Imported project item not found for " + target.media_path,
        );
        result.ok = false;
        continue;
      }

      if (!projectItem.canProxy || !projectItem.canProxy()) {
        result.errors.push(
          "Project item cannot accept a proxy: " +
            __atrSafeString(projectItem.name),
        );
        result.ok = false;
        continue;
      }

      var desiredProxyPath = __atrComputeManagedProxyOutputPath(
        normalizedRootPath,
        target,
      );
      var currentProxyPath = "";
      try {
        if (projectItem.hasProxy && projectItem.hasProxy()) {
          currentProxyPath = __atrNormalizePath(projectItem.getProxyPath());
        }
      } catch (eCurrentProxy) {
        currentProxyPath = "";
      }

      var desiredProxyFile = new File(desiredProxyPath);
      var desiredExists = !!desiredProxyFile.exists;
      var desiredMatchesCurrent =
        __atrNormalizeComparePath(currentProxyPath) ===
        __atrNormalizeComparePath(desiredProxyPath);

      if (desiredMatchesCurrent && desiredExists) {
        result.already_compliant += 1;
        continue;
      }

      if (desiredExists) {
        try {
          var desiredProxyFsPath = __atrSafeString(
            desiredProxyFile.fsName || desiredProxyPath,
          );
          var attachExisting = __atrTryAttachProxy(
            projectItem,
            desiredProxyFsPath,
          );
          if (!attachExisting.ok) {
            throw new Error(attachExisting.error || "attachProxy returned false");
          }
          result.attached += 1;
          if (currentProxyPath) {
            result.replaced_noncompliant += 1;
          }
          continue;
        } catch (eAttachExisting) {
          result.errors.push(
            "Failed to attach existing proxy for " +
              target.media_path +
              ": " +
              __atrSafeString(eAttachExisting.message || eAttachExisting),
          );
          result.ok = false;
          continue;
        }
      }

      var presetResult = __atrBuildProxyPresetPath(
        normalizedRootPath,
        target,
        proxyPresetTemplatePath,
      );
      var presetPath = presetResult && presetResult.path ? presetResult.path : "";
      if (!presetPath) {
        result.errors.push(
          "Failed to build proxy preset for " +
            target.media_path +
            (presetResult && presetResult.error
              ? " (" + presetResult.error + ")"
              : ""),
        );
        result.ok = false;
        continue;
      }

      var proxyFolderPath = __atrGetParentPath(desiredProxyPath);
      if (proxyFolderPath && !__atrEnsureFolder(proxyFolderPath)) {
        result.errors.push(
          "Failed to create proxy folder for " + desiredProxyPath,
        );
        result.ok = false;
        continue;
      }

      __atrPushHostTrace(
        plan.project_id,
        "Queueing proxy encode for " + __atrGetBasename(target.media_path),
        "info",
      );
      var desiredProxyOutputFile = new File(desiredProxyPath);
      var desiredProxyOutputFsPath = __atrSafeString(
        desiredProxyOutputFile.fsName || desiredProxyPath,
      );
      var presetFile = new File(presetPath);
      var presetFsPath = __atrSafeString(presetFile.fsName || presetPath);
      var jobID = __atrEncodeProjectItem(
        projectItem,
        desiredProxyOutputFsPath,
        presetFsPath,
      );
      __atrRememberEncoderJob(
        jobID,
        plan.project_id || __atrSafeString(app.project.name || ""),
        "proxy",
        desiredProxyOutputFsPath,
        presetFsPath,
      );
      __atrRememberProxyJob(
        jobID,
        plan.project_id || __atrSafeString(app.project.name || ""),
        target.media_path,
        desiredProxyPath,
        normalizedRootPath,
        plan.auto_enable_proxy_view,
      );
      __atrPushHostTrace(
        plan.project_id,
        "Queued proxy encode job " + __atrSafeString(jobID),
        "info",
      );
      result.queued += 1;
      result.job_ids.push(__atrSafeString(jobID));
      if (currentProxyPath && !__atrLooksLikeManagedProxyPath(currentProxyPath, normalizedRootPath)) {
        result.replaced_noncompliant += 1;
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

    if (result.queued > 0 && app.encoder && app.encoder.startBatch) {
      __atrPushHostTrace(
        plan.project_id,
        "Starting AME batch for " + result.queued + " proxy job(s)",
        "info",
      );
      try {
        app.encoder.startBatch();
      } catch (eStartBatchQueued) {
        result.ok = false;
        result.errors.push(
          "Failed to start AME batch: " +
            __atrSafeString(eStartBatchQueued.message || eStartBatchQueued),
        );
        __atrPushHostTrace(
          plan.project_id,
          "Failed to start AME batch: " +
            __atrSafeString(eStartBatchQueued.message || eStartBatchQueued),
          "error",
        );
      }
    }

    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
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
      var repairKey = __atrBuildProxyRepairKey(plan.project_id, target.media_path);
      if (__atrProxyRepairQueuedMap[repairKey] && !repairProxyFile.exists) {
        result.pending += 1;
        result.attach_pending += 1;
        result.attach_pending_errors.push(
          target.media_path + ": ProjectItem proxy repair is still rendering",
        );
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

function __atrCountTrackedProxyJobs() {
  var count = 0;
  for (var jobID in __atrProxyJobMap) {
    if (__atrProxyJobMap.hasOwnProperty(jobID)) {
      count += 1;
    }
  }
  return count;
}

function __atrClearTrackedProxyState() {
  var canceledJobs = 0;
  for (var proxyJobID in __atrProxyJobMap) {
    if (__atrProxyJobMap.hasOwnProperty(proxyJobID)) {
      canceledJobs += 1;
      try {
        delete __atrProxyJobMap[proxyJobID];
      } catch (eDeleteProxy) {}
      try {
        delete __atrEncoderJobProjectMap[proxyJobID];
      } catch (eDeleteProject) {}
      try {
        delete __atrEncoderJobMetaMap[proxyJobID];
      } catch (eDeleteMeta) {}
    }
  }

  for (var metaJobID in __atrEncoderJobMetaMap) {
    if (
      __atrEncoderJobMetaMap.hasOwnProperty(metaJobID) &&
      __atrEncoderJobMetaMap[metaJobID] &&
      __atrEncoderJobMetaMap[metaJobID].render_kind === "proxy"
    ) {
      try {
        delete __atrEncoderJobProjectMap[metaJobID];
      } catch (eDeleteProject2) {}
      try {
        delete __atrEncoderJobMetaMap[metaJobID];
      } catch (eDeleteMeta2) {}
    }
  }

  __atrProxyRepairQueuedMap = {};
  __atrProxyRepairJobKeyMap = {};
  __atrProxyAttachAttemptMap = {};

  var canceledKickoffs = 0;
  for (var kickoffId in __atrPendingProxyKickoffs) {
    if (__atrPendingProxyKickoffs.hasOwnProperty(kickoffId)) {
      canceledKickoffs += 1;
      try {
        delete __atrPendingProxyKickoffs[kickoffId];
      } catch (eDeleteKickoff) {}
    }
  }

  return {
    canceled_proxy_jobs: canceledJobs,
    canceled_scheduled_kickoffs: canceledKickoffs,
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

  var deadline = new Date().getTime() + 90000;
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

  result.errors.push("Adobe Media Encoder did not start within 90 seconds");
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
    var sendDeadline = new Date().getTime() + 30000;
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
    canceled_proxy_jobs: __atrCountTrackedProxyJobs(),
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
      project_items_remaining: Number(
        projectItems.project_items_remaining || 0,
      ),
      project_items_considered: Number(
        projectItems.project_items_considered || 0,
      ),
      cleanup_passes_executed: Number(projectItems.passes_executed || 0),
      timeline_removed: Number(timeline.removed || 0),
      timeline_failed: Number(timeline.failed || 0),
    };

    if (!result.ok) {
      result.error = "Imported Premiere project items remain after cleanup";
    }

    return JSON.stringify(result);
  } catch (e) {
    return "ERROR: " + e.message + " (line " + e.line + ")";
  }
}
