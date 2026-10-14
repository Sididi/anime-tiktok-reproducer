"use strict";

// Persist this receipt alongside the successful Drive upload. Notification
// retries never need the local video, Premiere, or another Drive upload.
function createReceipt(state, outputPath, result, timestamp) {
  if (!/\.mp4$/i.test(String(outputPath || "")) || !result.drive_file_id) {
    return null;
  }
  return {
    notification_id: require("crypto").randomBytes(16).toString("hex"),
    launch_id: state.link_launch_id || null,
    started_at: state.run_started_at || state.created_at || timestamp,
    uploaded_at: timestamp,
    drive_file_id: result.drive_file_id,
    status: "pending",
    last_error: null,
  };
}

function pendingFrames(states) {
  return Object.keys(states || {}).reduce(function (frames, projectId) {
    var receipt = states[projectId].video_upload_notification;
    if (receipt && receipt.status === "pending") {
      frames.push({
        type: "video_uploaded",
        project_id: projectId,
        notification_id: receipt.notification_id,
        launch_id: receipt.launch_id,
        started_at: receipt.started_at,
        uploaded_at: receipt.uploaded_at,
        drive_file_id: receipt.drive_file_id,
      });
    }
    return frames;
  }, []);
}

function applyAcknowledgement(receipt, frame, timestamp) {
  if (!receipt || receipt.status !== "pending" ||
      frame.notification_id !== receipt.notification_id) {
    return null;
  }
  var next = {};
  Object.keys(receipt).forEach(function (key) { next[key] = receipt[key]; });
  if (frame.ok === true) {
    next.status = "confirmed";
    next.confirmed_at = timestamp;
    next.last_error = null;
  } else {
    next.status = frame.retryable === false ? "rejected" : "pending";
    next.last_error = String(frame.error || "Discord reaction failed");
  }
  return next;
}

module.exports = {
  createReceipt: createReceipt,
  pendingFrames: pendingFrames,
  applyAcknowledgement: applyAcknowledgement,
};
