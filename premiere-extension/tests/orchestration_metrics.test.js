"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const orchestrationMetrics = require("../tiktok-reproducer/client/orchestration_metrics");

test("finalizeReadyMetrics computes total ready time and excludes host import time", () => {
  const seeded = orchestrationMetrics.mergeMetrics(
    orchestrationMetrics.createInitialMetrics("2026-04-03T10:00:00.000Z"),
    {
      resolve_folder_elapsed_ms: 250,
      list_tree_elapsed_ms: 1250,
      download_elapsed_ms: 9000,
      subtitle_extract_elapsed_ms: 500,
    },
  );

  const finalized = orchestrationMetrics.finalizeReadyMetrics(
    seeded,
    "2026-04-03T10:00:15.000Z",
    4000,
  );

  assert.equal(finalized.http_received_at, "2026-04-03T10:00:00.000Z");
  assert.equal(finalized.ready_elapsed_ms, 15000);
  assert.equal(finalized.host_import_elapsed_ms, 4000);
  assert.equal(finalized.orchestration_elapsed_ms, 11000);
  assert.equal(finalized.resolve_folder_elapsed_ms, 250);
  assert.equal(finalized.list_tree_elapsed_ms, 1250);
  assert.equal(finalized.download_elapsed_ms, 9000);
  assert.equal(finalized.subtitle_extract_elapsed_ms, 500);
});
