"use strict";

function createProgressState() {
  return {
    has_emitted_initial: false,
    last_bucket: -1,
  };
}

function clampPercent(value) {
  var parsed = Number(value);
  if (!isFinite(parsed) || parsed <= 0) {
    return 0;
  }
  if (parsed >= 100) {
    return 100;
  }
  return Math.max(0, Math.min(100, Math.round(parsed)));
}

function computePercent(downloadedBytes, totalBytes) {
  var total = Number(totalBytes || 0);
  var downloaded = Number(downloadedBytes || 0);
  if (!isFinite(total) || total <= 0) {
    return 0;
  }
  return clampPercent((downloaded / total) * 100);
}

function computeBucket(percent) {
  var safePercent = clampPercent(percent);
  if (safePercent >= 100) {
    return 100;
  }
  return Math.floor(safePercent / 10) * 10;
}

function formatMegabytes(bytes) {
  return (Math.max(0, Number(bytes || 0)) / (1024 * 1024)).toFixed(1);
}

function buildSummaryEvent(state, details) {
  var runtime = state || createProgressState();
  var totalBytes = Math.max(0, Number((details && details.total_bytes) || 0));
  var downloadedBytes = Math.max(
    0,
    Number((details && details.downloaded_bytes) || 0),
  );
  var percent = computePercent(downloadedBytes, totalBytes);
  var bucket = computeBucket(percent);
  var shouldEmit = false;

  if (!runtime.has_emitted_initial) {
    shouldEmit = true;
  } else if (percent >= 100 && runtime.last_bucket !== 100) {
    shouldEmit = true;
  } else if (bucket >= 10 && bucket !== runtime.last_bucket) {
    shouldEmit = true;
  }

  if (!shouldEmit) {
    return null;
  }

  runtime.has_emitted_initial = true;
  runtime.last_bucket = bucket;

  return {
    stage: "download_progress_summary",
    project_id: String((details && details.project_id) || ""),
    file_count: Math.max(0, Number((details && details.file_count) || 0)),
    downloaded_bytes: downloadedBytes,
    total_bytes: totalBytes,
    progress_pct: percent,
    progress_bucket: bucket,
    message:
      "Download progress for " +
      String((details && details.project_id) || "") +
      ": " +
      percent +
      "% (" +
      formatMegabytes(downloadedBytes) +
      "/" +
      formatMegabytes(totalBytes) +
      " MB)",
  };
}

module.exports = {
  buildSummaryEvent: buildSummaryEvent,
  computeBucket: computeBucket,
  computePercent: computePercent,
  createProgressState: createProgressState,
};
