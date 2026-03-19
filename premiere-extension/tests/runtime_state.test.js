"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const runtimeState = require("../tiktok-reproducer/client/runtime_state");

test("createEmptyJobStore returns an in-memory empty queue", () => {
  assert.deepEqual(runtimeState.createEmptyJobStore(), {
    queue: [],
    active: null,
  });
});

test("normalizeLoadedProjectStates converts transient states to manual recovery errors", () => {
  const loadedStates = {
    downloadingProject: {
      project_id: "downloadingProject",
      status: "downloading",
      last_error: null,
      updated_at: "2026-03-18T10:00:00.000Z",
    },
    cleanupProject: {
      project_id: "cleanupProject",
      status: "cleanup_pending",
      cleanup_retryable: true,
      cleanup_next_retry_at: "2026-03-18T10:05:00.000Z",
    },
    exportProject: {
      project_id: "exportProject",
      status: "ready_for_export",
      output_path: "C:\\temp\\output.mp4",
    },
  };

  const result = runtimeState.normalizeLoadedProjectStates(
    loadedStates,
    "2026-03-19T08:00:00.000Z",
  );

  assert.equal(result.changed_count, 2);
  assert.equal(result.states.downloadingProject.status, "error");
  assert.match(
    result.states.downloadingProject.last_error,
    /Automatic recovery is disabled/i,
  );
  assert.equal(result.states.downloadingProject.updated_at, "2026-03-19T08:00:00.000Z");

  assert.equal(result.states.cleanupProject.status, "cleanup_failed");
  assert.equal(result.states.cleanupProject.cleanup_retryable, false);
  assert.equal(result.states.cleanupProject.cleanup_next_retry_at, null);
  assert.match(
    result.states.cleanupProject.cleanup_error,
    /Automatic recovery is disabled/i,
  );

  assert.equal(result.states.exportProject.status, "ready_for_export");
  assert.equal(result.states.exportProject.output_path, "C:\\temp\\output.mp4");
});
