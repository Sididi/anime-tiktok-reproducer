"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const batchRuntime = require("../tiktok-reproducer/client/batch_runtime");

test("acceptProject queues intake projects and blocks duplicates for the session", () => {
  const runtime = batchRuntime.createBatchRuntime();

  const first = batchRuntime.acceptProject(runtime, "projectA");
  const duplicate = batchRuntime.acceptProject(runtime, "projectA");

  assert.equal(first.accepted, true);
  assert.equal(first.is_sleeping, false);
  assert.deepEqual(runtime.current_batch_ids, ["projectA"]);
  assert.equal(duplicate.accepted, false);
  assert.equal(duplicate.duplicate, true);
  assert.equal(batchRuntime.hasProjectBeenSeen(runtime, "projectA"), true);
});

test("acceptProject routes new projects to sleeping queue outside intake", () => {
  const runtime = batchRuntime.createBatchRuntime();
  runtime.phase = batchRuntime.PHASES.exporting;

  const result = batchRuntime.acceptProject(runtime, "projectB");

  assert.equal(result.accepted, true);
  assert.equal(result.is_sleeping, true);
  assert.deepEqual(runtime.sleeping_queue, ["projectB"]);
});

test("canStartExport requires ready_for_export states and no active download jobs", () => {
  const runtime = batchRuntime.createBatchRuntime();
  runtime.current_batch_ids = ["projectA", "projectB"];

  const projectStates = {
    projectA: { project_id: "projectA", status: "ready_for_export" },
    projectB: { project_id: "projectB", status: "importing" },
  };

  const withStatusBlocker = batchRuntime.canStartExport(runtime, projectStates, {
    queue: [],
    active: null,
  });
  assert.equal(withStatusBlocker.ok, false);
  assert.deepEqual(withStatusBlocker.blockers, [
    {
      type: "project_status",
      project_id: "projectB",
      status: "importing",
    },
  ]);

  projectStates.projectB.status = "ready_for_export";
  const withJobBlocker = batchRuntime.canStartExport(runtime, projectStates, {
    queue: [],
    active: {
      type: "download_import",
      payload: { project_id: "projectA" },
    },
  });
  assert.equal(withJobBlocker.ok, false);
  assert.deepEqual(withJobBlocker.blockers, [
    {
      type: "active_job",
      job_type: "download_import",
      project_id: "projectA",
    },
  ]);

  const withQueuedJobBlocker = batchRuntime.canStartExport(runtime, projectStates, {
    queue: [
      {
        type: "upload_output",
        payload: { project_id: "projectA" },
      },
    ],
    active: null,
  });
  assert.equal(withQueuedJobBlocker.ok, false);
  assert.deepEqual(withQueuedJobBlocker.blockers, [
    {
      type: "queued_job",
      job_type: "upload_output",
      project_id: "projectA",
    },
  ]);

  const ready = batchRuntime.canStartExport(runtime, projectStates, {
    queue: [],
    active: null,
  });
  assert.equal(ready.ok, true);
});

test("acknowledgeFinalPopup promotes sleeping queue into the next intake batch", () => {
  const runtime = batchRuntime.createBatchRuntime();
  runtime.phase = batchRuntime.PHASES.awaiting_final_ack;
  runtime.current_batch_ids = ["finishedProject"];
  runtime.export_batch_ids = ["finishedProject"];
  runtime.sleeping_queue = ["projectC", "projectD"];

  const promoted = batchRuntime.acknowledgeFinalPopup(runtime);

  assert.deepEqual(promoted, ["projectC", "projectD"]);
  assert.equal(runtime.phase, batchRuntime.PHASES.intake);
  assert.deepEqual(runtime.current_batch_ids, ["projectC", "projectD"]);
  assert.deepEqual(runtime.export_batch_ids, []);
  assert.deepEqual(runtime.sleeping_queue, []);
});
