"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const automationControl = require("../tiktok-reproducer/client/automation_control");

test("activateProjectOwnership switches owner and invalidates previous lease", () => {
  const runtime = automationControl.createAutomationRuntime();

  const first = automationControl.activateProjectOwnership(runtime, "projectA");
  const leaseA = automationControl.captureLease(runtime, "projectA");

  assert.equal(first.changed, true);
  assert.equal(first.previous_project_id, null);
  assert.equal(first.lease.project_id, "projectA");
  assert.equal(first.lease.generation, 1);
  assert.equal(automationControl.isLeaseActive(runtime, leaseA), true);

  const second = automationControl.activateProjectOwnership(runtime, "projectB");

  assert.equal(second.changed, true);
  assert.equal(second.previous_project_id, "projectA");
  assert.equal(second.lease.project_id, "projectB");
  assert.equal(second.lease.generation, 2);
  assert.equal(automationControl.isLeaseActive(runtime, leaseA), false);
  assert.equal(automationControl.isProjectActive(runtime, "projectA"), false);
  assert.equal(automationControl.isProjectActive(runtime, "projectB"), true);
});

test("activateProjectOwnership is stable when same project stays active", () => {
  const runtime = automationControl.createAutomationRuntime();

  const first = automationControl.activateProjectOwnership(runtime, "projectA");
  const second = automationControl.activateProjectOwnership(runtime, "projectA");

  assert.equal(first.lease.generation, 1);
  assert.equal(second.changed, false);
  assert.equal(second.previous_project_id, null);
  assert.equal(second.lease.project_id, "projectA");
  assert.equal(second.lease.generation, 1);
});
