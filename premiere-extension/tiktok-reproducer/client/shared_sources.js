"use strict";

/**
 * Shared-source reuse for project downloads.
 *
 * New-layout Drive exports externalize large sources/ files (episodes, music)
 * into a shared Drive folder and describe them in atr_remote_sources.json at
 * the project folder root. This module parses that manifest and pre-seeds the
 * download destination by hardlinking matching files (and their already
 * transcoded proxies) from sibling project folders still on disk, so the
 * download loop's existing dest-exists reuse branch short-circuits them.
 *
 * Pure Node (no CEP/DOM dependencies): required from the Drive task runner, the
 * panel fallback path, and plain `node` for smoke tests.
 */

var fs = require("fs");
var path = require("path");
var constants = require("./constants");

var REMOTE_SOURCES_MANIFEST_FILENAME =
  constants.REMOTE_SOURCES_MANIFEST_FILENAME;
var SHARED_SOURCE_MIN_BYTES = constants.SHARED_SOURCE_MIN_BYTES;
var PROXY_OUTPUT_SUFFIX = constants.PROXY_OUTPUT_SUFFIX;
var PROXY_MARKER_SUFFIX = constants.PROXY_MARKER_SUFFIX;
var PROXY_MARKER_VERSION = constants.PROXY_MARKER_VERSION;
var SUPPORTED_MANIFEST_SCHEMA_VERSION = 1;

function readJsonQuiet(filePath) {
  try {
    if (!fs.existsSync(filePath)) {
      return null;
    }
    var raw = fs.readFileSync(filePath, "utf8");
    if (!raw.trim()) {
      return null;
    }
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function normalizeRootForCompare(rootPath) {
  var normalized = path.resolve(String(rootPath || ""));
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

/**
 * Manifest paths use POSIX separators and original (Drive-side) names, while
 * files on disk went through sanitizeWindowsSegment at download time. Matching
 * must therefore happen in sanitized, platform-separator space.
 */
function sanitizeRelativePosixPath(posixPath, sanitizeSegment) {
  var segments = String(posixPath || "")
    .split("/")
    .filter(function (segment) {
      return segment.length > 0;
    })
    .map(function (segment) {
      return sanitizeSegment(segment);
    });
  return segments.join(path.sep);
}

function isSafeRelativePosixPath(posixPath) {
  var value = String(posixPath || "");
  if (!value || value.indexOf("\\") !== -1 || value.charAt(0) === "/") {
    return false;
  }
  var segments = value.split("/");
  for (var i = 0; i < segments.length; i += 1) {
    var segment = segments[i];
    if (!segment || segment === "." || segment === "..") {
      return false;
    }
  }
  return true;
}

/**
 * Parses atr_remote_sources.json content. Returns null for a missing/blank
 * document; throws on a malformed one or a schema from the future (the
 * backend moved on and this extension must be updated before downloading).
 */
function parseRemoteSourcesManifest(jsonText) {
  var raw = String(jsonText || "").trim();
  if (!raw) {
    return null;
  }
  var parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new Error(
      REMOTE_SOURCES_MANIFEST_FILENAME + " is not valid JSON: " + e.message,
    );
  }
  var schemaVersion = Number(parsed && parsed.schema_version);
  if (!isFinite(schemaVersion) || schemaVersion < 1) {
    throw new Error(
      REMOTE_SOURCES_MANIFEST_FILENAME + " has an invalid schema_version",
    );
  }
  if (schemaVersion > SUPPORTED_MANIFEST_SCHEMA_VERSION) {
    throw new Error(
      REMOTE_SOURCES_MANIFEST_FILENAME +
        " schema_version " +
        schemaVersion +
        " is newer than this extension supports; update the TiktokReproducer extension.",
    );
  }

  var entries = Array.isArray(parsed.files) ? parsed.files : [];
  var files = [];
  entries.forEach(function (entry) {
    var relPath = String((entry && entry.path) || "");
    var size = Number((entry && entry.size) || 0);
    var driveFileId = String((entry && entry.drive_file_id) || "");
    if (!isSafeRelativePosixPath(relPath)) {
      throw new Error(
        REMOTE_SOURCES_MANIFEST_FILENAME +
          " has an unsafe file path: " +
          relPath,
      );
    }
    if (!driveFileId || !(size > 0)) {
      throw new Error(
        REMOTE_SOURCES_MANIFEST_FILENAME +
          " entry for " +
          relPath +
          " is missing drive_file_id or size",
      );
    }
    files.push({
      path: relPath,
      size: size,
      sha256: entry.sha256 ? String(entry.sha256).toLowerCase() : null,
      md5: entry.md5 ? String(entry.md5).toLowerCase() : null,
      drive_file_id: driveFileId,
      shared_name: entry.shared_name ? String(entry.shared_name) : null,
    });
  });

  return {
    schema_version: schemaVersion,
    shared_folder_id: String(parsed.shared_folder_id || ""),
    files: files,
  };
}

/**
 * Roots of other downloaded projects that may still hold reusable files.
 * Sourced from the panel's per-project state files; only existing, finalized
 * (non-.partial) directories are returned, most recently touched first.
 */
function buildSiblingRootIndex(appDataPath, excludeRootPath) {
  var stateProjectsDir = path.join(
    String(appDataPath || ""),
    "Adobe",
    "TiktokReproducer",
    "state",
    "projects",
  );
  var excluded = excludeRootPath
    ? normalizeRootForCompare(excludeRootPath)
    : null;

  var entries;
  try {
    entries = fs.readdirSync(stateProjectsDir);
  } catch (e) {
    return [];
  }

  var seen = {};
  var roots = [];
  entries.forEach(function (fileName) {
    if (!/\.json$/i.test(fileName)) {
      return;
    }
    var statePath = path.join(stateProjectsDir, fileName);
    var state = readJsonQuiet(statePath);
    var localRoot = String((state && state.local_root) || "").trim();
    if (!localRoot || /\.partial$/i.test(localRoot)) {
      return;
    }
    var compareKey = normalizeRootForCompare(localRoot);
    if (seen[compareKey] || (excluded && compareKey === excluded)) {
      return;
    }
    seen[compareKey] = true;
    try {
      if (!fs.statSync(localRoot).isDirectory()) {
        return;
      }
      roots.push({
        root: localRoot,
        state_mtime_ms: fs.statSync(statePath).mtimeMs || 0,
      });
    } catch (e) {
      // Root disappeared (batch cleanup) - skip.
    }
  });

  roots.sort(function (a, b) {
    return b.state_mtime_ms - a.state_mtime_ms;
  });
  return roots.map(function (item) {
    return item.root;
  });
}

/**
 * Mirrors main.js computeManagedProxyOutputPath, but relative:
 * sources/Ep01.mkv -> proxies/sources/Ep01__atr_proxy.mp4
 */
function computeProxyRelativePath(sourceRelativePath) {
  var posix = String(sourceRelativePath || "").split(path.sep).join("/");
  var segments = posix.split("/").filter(function (segment) {
    return segment.length > 0;
  });
  if (segments.length === 0) {
    return null;
  }
  var baseName = segments[segments.length - 1];
  var parsed = path.parse(baseName);
  var stem = parsed.name || baseName;
  var dirSegments = ["proxies"].concat(segments.slice(0, -1));
  return path.join.apply(
    path,
    dirSegments.concat([stem + PROXY_OUTPUT_SUFFIX]),
  );
}

/**
 * Candidates worth checking against sibling roots before downloading:
 * manifest-listed shared files, plus (old-layout bonus) any walked sources/
 * file large enough to be worth a hardlink.
 */
function selectPreSeedCandidates(walkedFiles, manifestFiles, sanitizeSegment) {
  var candidates = [];
  var seen = {};

  (manifestFiles || []).forEach(function (entry) {
    var relativePath = sanitizeRelativePosixPath(entry.path, sanitizeSegment);
    if (!relativePath || seen[relativePath]) {
      return;
    }
    seen[relativePath] = true;
    candidates.push({
      relativePath: relativePath,
      size: Number(entry.size || 0),
      sha256: entry.sha256 || null,
      from_manifest: true,
    });
  });

  var sourcesPrefix = "sources" + path.sep;
  (walkedFiles || []).forEach(function (file) {
    var relativePath = String((file && file.relativePath) || "");
    var size = Number((file && file.size) || 0);
    if (
      relativePath.indexOf(sourcesPrefix) !== 0 ||
      size < SHARED_SOURCE_MIN_BYTES ||
      seen[relativePath]
    ) {
      return;
    }
    seen[relativePath] = true;
    candidates.push({
      relativePath: relativePath,
      size: size,
      sha256: null,
      from_manifest: false,
    });
  });

  return candidates;
}

function loadSiblingShaIndex(siblingRoot, sanitizeSegment, cache) {
  if (Object.prototype.hasOwnProperty.call(cache, siblingRoot)) {
    return cache[siblingRoot];
  }
  var manifestPath = path.join(siblingRoot, REMOTE_SOURCES_MANIFEST_FILENAME);
  var index = null;
  var parsed = readJsonQuiet(manifestPath);
  if (parsed && Array.isArray(parsed.files)) {
    index = {};
    parsed.files.forEach(function (entry) {
      var relPath = String((entry && entry.path) || "");
      if (!relPath || !entry.sha256) {
        return;
      }
      index[sanitizeRelativePosixPath(relPath, sanitizeSegment)] = String(
        entry.sha256,
      ).toLowerCase();
    });
  }
  cache[siblingRoot] = index;
  return index;
}

function linkOrCopyFile(sourcePath, destinationPath) {
  ensureDir(path.dirname(destinationPath));
  try {
    fs.linkSync(sourcePath, destinationPath);
    return "hardlink";
  } catch (linkErr) {
    fs.copyFileSync(sourcePath, destinationPath);
    return "copy";
  }
}

function isReusableProxyMarker(marker) {
  return !!(
    marker &&
    Number(marker.marker_version || 0) === PROXY_MARKER_VERSION &&
    marker.panel_build_id
  );
}

function reuseSiblingProxy(siblingRoot, partialRoot, candidate) {
  var proxyRelativePath = computeProxyRelativePath(candidate.relativePath);
  if (!proxyRelativePath) {
    return false;
  }
  var sourceProxyPath = path.join(siblingRoot, proxyRelativePath);
  var sourceMarker = readJsonQuiet(sourceProxyPath + PROXY_MARKER_SUFFIX);
  if (!fs.existsSync(sourceProxyPath) || !isReusableProxyMarker(sourceMarker)) {
    return false;
  }

  var destinationProxyPath = path.join(partialRoot, proxyRelativePath);
  try {
    linkOrCopyFile(sourceProxyPath, destinationProxyPath);
  } catch (e) {
    return false;
  }

  // Never hardlink the marker itself: markers are rewritten in place and a
  // shared inode would leak writes into the sibling project. Rewrite the
  // paths so isCleanProxyOutput/attach logic sees this project's layout.
  var freshMarker = {};
  Object.keys(sourceMarker).forEach(function (key) {
    freshMarker[key] = sourceMarker[key];
  });
  freshMarker.output_path = destinationProxyPath;
  freshMarker.media_path = path.join(partialRoot, candidate.relativePath);
  freshMarker.reused_from_root = siblingRoot;
  var markerPath = destinationProxyPath + PROXY_MARKER_SUFFIX;
  var tmpMarkerPath = markerPath + ".tmp";
  try {
    fs.writeFileSync(tmpMarkerPath, JSON.stringify(freshMarker, null, 2));
    fs.renameSync(tmpMarkerPath, markerPath);
  } catch (e) {
    try {
      fs.rmSync(destinationProxyPath, { force: true });
      fs.rmSync(tmpMarkerPath, { force: true });
    } catch (cleanupErr) {
      // best effort
    }
    return false;
  }
  return true;
}

/**
 * Hardlinks (or copies) matching candidates from sibling roots into
 * partialRoot so the download loop's dest-exists reuse branch skips them.
 * Every failure degrades to a normal download - this never throws.
 */
function preSeedFromSiblings(options) {
  var partialRoot = options.partialRoot;
  var candidates = options.candidates || [];
  var siblingRoots = options.siblingRoots || [];
  var sanitizeSegment = options.sanitizeSegment;

  var stats = {
    candidate_count: candidates.length,
    sibling_root_count: siblingRoots.length,
    reused_count: 0,
    reused_bytes: 0,
    proxies_reused_count: 0,
    hardlink_fallback_copy_count: 0,
    reused_files: [],
  };
  if (candidates.length === 0 || siblingRoots.length === 0) {
    return stats;
  }

  var shaIndexCache = {};
  candidates.forEach(function (candidate) {
    var destination = path.join(partialRoot, candidate.relativePath);
    if (fs.existsSync(destination)) {
      return;
    }

    for (var i = 0; i < siblingRoots.length; i += 1) {
      var siblingRoot = siblingRoots[i];
      var siblingPath = path.join(siblingRoot, candidate.relativePath);
      var siblingStat;
      try {
        siblingStat = fs.statSync(siblingPath);
      } catch (e) {
        continue;
      }
      if (!siblingStat.isFile() || siblingStat.size !== candidate.size) {
        continue;
      }

      // Same basename + size can still be different content (e.g. pure-mode
      // tiktok_clean.mp4). When both sides carry a sha256, require equality.
      if (candidate.sha256) {
        var siblingShaIndex = loadSiblingShaIndex(
          siblingRoot,
          sanitizeSegment,
          shaIndexCache,
        );
        if (siblingShaIndex) {
          var siblingSha = siblingShaIndex[candidate.relativePath];
          if (siblingSha && siblingSha !== candidate.sha256) {
            continue;
          }
        }
      }

      var method;
      try {
        method = linkOrCopyFile(siblingPath, destination);
      } catch (e) {
        continue;
      }

      stats.reused_count += 1;
      stats.reused_bytes += candidate.size;
      if (method === "copy") {
        stats.hardlink_fallback_copy_count += 1;
      }
      var proxyReused = reuseSiblingProxy(siblingRoot, partialRoot, candidate);
      if (proxyReused) {
        stats.proxies_reused_count += 1;
      }
      stats.reused_files.push({
        relative_path: candidate.relativePath,
        size: candidate.size,
        method: method,
        proxy_reused: proxyReused,
        from_root: siblingRoot,
      });
      return;
    }
  });

  return stats;
}

module.exports = {
  REMOTE_SOURCES_MANIFEST_FILENAME: REMOTE_SOURCES_MANIFEST_FILENAME,
  SUPPORTED_MANIFEST_SCHEMA_VERSION: SUPPORTED_MANIFEST_SCHEMA_VERSION,
  buildSiblingRootIndex: buildSiblingRootIndex,
  computeProxyRelativePath: computeProxyRelativePath,
  parseRemoteSourcesManifest: parseRemoteSourcesManifest,
  preSeedFromSiblings: preSeedFromSiblings,
  sanitizeRelativePosixPath: sanitizeRelativePosixPath,
  selectPreSeedCandidates: selectPreSeedCandidates,
};
