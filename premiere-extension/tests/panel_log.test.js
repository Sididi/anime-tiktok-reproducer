"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const panelLog = require("../tiktok-reproducer/client/panel_log");

test("appendLogEntry trims the oldest entries when the cap is reached", () => {
  const state = panelLog.createLogState(3);

  panelLog.appendLogEntry(state, { message: "one", level: "info" });
  panelLog.appendLogEntry(state, { message: "two", level: "info" });
  panelLog.appendLogEntry(state, { message: "three", level: "warn" });
  const finalAppend = panelLog.appendLogEntry(state, {
    message: "four",
    level: "error",
  });

  assert.equal(finalAppend.trimmed_count, 1);
  assert.deepEqual(
    state.entries.map((entry) => entry.message),
    ["two", "three", "four"],
  );
  assert.deepEqual(
    state.entries.map((entry) => entry.level),
    ["info", "warn", "error"],
  );
});
