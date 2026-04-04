"use strict";

var DEFAULT_MAX_ENTRIES = 200;

function normalizeMaxEntries(value) {
  var parsed = Number(value);
  if (!parsed || !isFinite(parsed)) {
    return DEFAULT_MAX_ENTRIES;
  }
  return Math.max(1, Math.floor(parsed));
}

function createLogState(maxEntries) {
  return {
    max_entries: normalizeMaxEntries(maxEntries),
    entries: [],
  };
}

function appendLogEntry(state, entry) {
  var runtime = state || createLogState(DEFAULT_MAX_ENTRIES);
  var nextEntry = {
    level: String((entry && entry.level) || "info"),
    message: String((entry && entry.message) || ""),
    timestamp: String((entry && entry.timestamp) || ""),
  };

  runtime.entries.push(nextEntry);

  var trimmedCount = 0;
  if (runtime.entries.length > runtime.max_entries) {
    trimmedCount = runtime.entries.length - runtime.max_entries;
    runtime.entries.splice(0, trimmedCount);
  }

  return {
    entry: nextEntry,
    trimmed_count: trimmedCount,
  };
}

module.exports = {
  DEFAULT_MAX_ENTRIES: DEFAULT_MAX_ENTRIES,
  appendLogEntry: appendLogEntry,
  createLogState: createLogState,
};
