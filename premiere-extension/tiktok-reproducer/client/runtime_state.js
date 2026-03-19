"use strict";

var TRANSIENT_ERROR_STATUSES = {
  queued_download: true,
  downloading: true,
  importing: true,
  exporting: true,
  uploading: true,
};

function cloneRecord(value) {
  var out = {};
  Object.keys(value || {}).forEach(function (key) {
    out[key] = value[key];
  });
  return out;
}

function createEmptyJobStore() {
  return {
    queue: [],
    active: null,
  };
}

function buildRecoveryDisabledMessage(previousStatus) {
  return (
    'Project was left in status "' +
    String(previousStatus || "unknown") +
    '" before Premiere restarted. Automatic recovery is disabled; retry manually.'
  );
}

function normalizeLoadedProjectStates(loadedStates, nowIsoValue) {
  var input = loadedStates || {};
  var normalized = {};
  var changedCount = 0;
  var changedProjectIds = [];
  var timestamp = String(nowIsoValue || new Date().toISOString());

  Object.keys(input).forEach(function (projectId) {
    var state = cloneRecord(input[projectId] || {});
    var status = String(state.status || "");

    if (TRANSIENT_ERROR_STATUSES[status]) {
      state.status = "error";
      state.last_error = buildRecoveryDisabledMessage(status);
      state.upload_pending = false;
      state.cleanup_retryable = false;
      state.cleanup_next_retry_at = null;
      state.export_job_id = null;
      state.video_export_job_id = null;
      state.audio_export_job_id = null;
      state.updated_at = timestamp;
      changedCount += 1;
      changedProjectIds.push(projectId);
    } else if (status === "cleanup_pending") {
      state.status = "cleanup_failed";
      state.cleanup_retryable = false;
      state.cleanup_next_retry_at = null;
      state.cleanup_error = buildRecoveryDisabledMessage(status);
      state.updated_at = timestamp;
      changedCount += 1;
      changedProjectIds.push(projectId);
    }

    normalized[projectId] = state;
  });

  return {
    states: normalized,
    changed_count: changedCount,
    changed_project_ids: changedProjectIds,
  };
}

module.exports = {
  buildRecoveryDisabledMessage: buildRecoveryDisabledMessage,
  createEmptyJobStore: createEmptyJobStore,
  normalizeLoadedProjectStates: normalizeLoadedProjectStates,
};
