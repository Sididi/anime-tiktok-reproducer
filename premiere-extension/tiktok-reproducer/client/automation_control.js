"use strict";

function createAutomationRuntime() {
  return {
    active_project_id: "",
    generation: 0,
  };
}

function captureLease(runtime, projectId) {
  return {
    project_id: String(projectId || ""),
    generation: Number((runtime && runtime.generation) || 0),
  };
}

function isLeaseActive(runtime, lease) {
  if (!runtime || !lease) {
    return false;
  }
  return (
    String(runtime.active_project_id || "") === String(lease.project_id || "") &&
    Number(runtime.generation || 0) === Number(lease.generation || 0)
  );
}

function isProjectActive(runtime, projectId) {
  return (
    String((runtime && runtime.active_project_id) || "") ===
    String(projectId || "")
  );
}

function activateProjectOwnership(runtime, projectId) {
  if (!runtime) {
    throw new Error("Missing automation runtime");
  }

  var nextProjectId = String(projectId || "").trim();
  if (!nextProjectId) {
    throw new Error("Missing project id");
  }

  var previousProjectId = String(runtime.active_project_id || "").trim() || null;
  if (previousProjectId === nextProjectId) {
    return {
      changed: false,
      previous_project_id: null,
      lease: captureLease(runtime, nextProjectId),
    };
  }

  runtime.active_project_id = nextProjectId;
  runtime.generation = Number(runtime.generation || 0) + 1;

  return {
    changed: true,
    previous_project_id: previousProjectId,
    lease: captureLease(runtime, nextProjectId),
  };
}

module.exports = {
  activateProjectOwnership: activateProjectOwnership,
  captureLease: captureLease,
  createAutomationRuntime: createAutomationRuntime,
  isLeaseActive: isLeaseActive,
  isProjectActive: isProjectActive,
};
