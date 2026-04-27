"use strict";

var METRIC_KEYS = [
  "http_received_at",
  "ready_elapsed_ms",
  "orchestration_elapsed_ms",
  "host_import_elapsed_ms",
  "resolve_folder_elapsed_ms",
  "list_tree_elapsed_ms",
  "download_elapsed_ms",
  "subtitle_extract_elapsed_ms",
];

function normalizeNumber(value) {
  var parsed = Number(value);
  if (!isFinite(parsed) || parsed < 0) {
    return null;
  }
  return Math.round(parsed);
}

function createInitialMetrics(httpReceivedAt) {
  return {
    http_received_at: httpReceivedAt ? String(httpReceivedAt) : null,
    ready_elapsed_ms: null,
    orchestration_elapsed_ms: null,
    host_import_elapsed_ms: null,
    resolve_folder_elapsed_ms: null,
    list_tree_elapsed_ms: null,
    download_elapsed_ms: null,
    subtitle_extract_elapsed_ms: null,
  };
}

function normalizeMetrics(metrics) {
  var source = metrics || {};
  var normalized = createInitialMetrics(
    Object.prototype.hasOwnProperty.call(source, "http_received_at")
      ? source.http_received_at
      : null,
  );

  METRIC_KEYS.forEach(function (key) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      return;
    }
    if (key === "http_received_at") {
      normalized.http_received_at = source.http_received_at
        ? String(source.http_received_at)
        : null;
      return;
    }
    normalized[key] = normalizeNumber(source[key]);
  });

  return normalized;
}

function mergeMetrics(existingMetrics, patchMetrics) {
  var merged = normalizeMetrics(existingMetrics);
  var patch = patchMetrics || {};

  METRIC_KEYS.forEach(function (key) {
    if (!Object.prototype.hasOwnProperty.call(patch, key)) {
      return;
    }
    if (key === "http_received_at") {
      merged.http_received_at = patch.http_received_at
        ? String(patch.http_received_at)
        : null;
      return;
    }
    merged[key] = normalizeNumber(patch[key]);
  });

  return merged;
}

function computeElapsedMs(startIso, endIso) {
  var startMs = Date.parse(String(startIso || ""));
  var endMs = Date.parse(String(endIso || ""));
  if (!isFinite(startMs) || !isFinite(endMs)) {
    return null;
  }
  return Math.max(0, Math.round(endMs - startMs));
}

function finalizeReadyMetrics(existingMetrics, readyAtIso, hostImportElapsedMs) {
  var merged = mergeMetrics(existingMetrics, {
    host_import_elapsed_ms: hostImportElapsedMs,
  });
  var readyElapsedMs = computeElapsedMs(merged.http_received_at, readyAtIso);

  if (readyElapsedMs === null) {
    merged.ready_elapsed_ms = null;
    merged.orchestration_elapsed_ms = null;
    return merged;
  }

  merged.ready_elapsed_ms = readyElapsedMs;
  merged.orchestration_elapsed_ms = Math.max(
    0,
    readyElapsedMs - (normalizeNumber(merged.host_import_elapsed_ms) || 0),
  );
  return merged;
}

module.exports = {
  METRIC_KEYS: METRIC_KEYS,
  computeElapsedMs: computeElapsedMs,
  createInitialMetrics: createInitialMetrics,
  finalizeReadyMetrics: finalizeReadyMetrics,
  mergeMetrics: mergeMetrics,
  normalizeMetrics: normalizeMetrics,
};
