"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const downloadProgress = require("../tiktok-reproducer/client/download_progress");

test("buildSummaryEvent emits on the first update, each new 10 percent bucket, and completion", () => {
  const state = downloadProgress.createProgressState();

  const first = downloadProgress.buildSummaryEvent(state, {
    project_id: "projectA",
    file_count: 10,
    downloaded_bytes: 5,
    total_bytes: 100,
  });
  const repeatedLowBucket = downloadProgress.buildSummaryEvent(state, {
    project_id: "projectA",
    file_count: 10,
    downloaded_bytes: 9,
    total_bytes: 100,
  });
  const tenPercent = downloadProgress.buildSummaryEvent(state, {
    project_id: "projectA",
    file_count: 10,
    downloaded_bytes: 10,
    total_bytes: 100,
  });
  const repeatedTenBucket = downloadProgress.buildSummaryEvent(state, {
    project_id: "projectA",
    file_count: 10,
    downloaded_bytes: 19,
    total_bytes: 100,
  });
  const twentyPercent = downloadProgress.buildSummaryEvent(state, {
    project_id: "projectA",
    file_count: 10,
    downloaded_bytes: 20,
    total_bytes: 100,
  });
  const complete = downloadProgress.buildSummaryEvent(state, {
    project_id: "projectA",
    file_count: 10,
    downloaded_bytes: 100,
    total_bytes: 100,
  });

  assert.equal(first.progress_pct, 5);
  assert.equal(first.progress_bucket, 0);
  assert.equal(repeatedLowBucket, null);
  assert.equal(tenPercent.progress_pct, 10);
  assert.equal(tenPercent.progress_bucket, 10);
  assert.equal(repeatedTenBucket, null);
  assert.equal(twentyPercent.progress_bucket, 20);
  assert.equal(complete.progress_pct, 100);
  assert.equal(complete.progress_bucket, 100);
  assert.match(complete.message, /100%/);
});
