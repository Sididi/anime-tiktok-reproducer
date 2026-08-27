"use strict";

function hasCompletedHostCleanup(state) {
  var current = state || {};
  if (current.host_cleanup_complete === true) {
    return true;
  }

  // State created before host_cleanup_complete existed can still safely skip
  // destructive Premiere work when its persisted release verification proves
  // that every managed sequence, item, media link, and proxy link was gone.
  var legacyResult = current.host_cleanup_result || null;
  return !!(
    legacyResult &&
    legacyResult.ok === true &&
    legacyResult.release_verification &&
    legacyResult.release_verification.ok === true
  );
}

function canRetryCleanup(state, triggerSource) {
  var current = state || {};
  var status = String(current.status || "");
  var source = String(triggerSource || "retry");
  return (
    status === "cleanup_pending" ||
    (status === "cleanup_failed" &&
      (source === "manual" || !!current.cleanup_retryable))
  );
}

function selectCleanupPhase(state) {
  return hasCompletedHostCleanup(state) ? "disk_only" : "host_then_disk";
}

module.exports = {
  hasCompletedHostCleanup: hasCompletedHostCleanup,
  canRetryCleanup: canRetryCleanup,
  selectCleanupPhase: selectCleanupPhase,
};
