"use strict";

var PHASES = {
  intake: "intake",
  exporting: "exporting",
  cleaning: "cleaning",
  awaiting_final_ack: "awaiting_final_ack",
  blocked_error: "blocked_error",
};

function normalizeProjectId(projectId) {
  return String(projectId || "").trim();
}

function cloneArray(values) {
  return Array.isArray(values) ? values.slice(0) : [];
}

function ensureProjectListed(targetList, projectId) {
  if (targetList.indexOf(projectId) >= 0) {
    return false;
  }
  targetList.push(projectId);
  return true;
}

function createBatchRuntime() {
  return {
    phase: PHASES.intake,
    current_batch_ids: [],
    export_batch_ids: [],
    sleeping_queue: [],
    session_excluded_ids: {},
  };
}

function buildSequenceName(projectId) {
  return "ATR_BATCH__" + normalizeProjectId(projectId);
}

function hasProjectBeenSeen(runtime, projectId) {
  var id = normalizeProjectId(projectId);
  if (!runtime || !id) {
    return false;
  }
  return !!runtime.session_excluded_ids[id];
}

function isProjectSleeping(runtime, projectId) {
  var id = normalizeProjectId(projectId);
  if (!runtime || !id) {
    return false;
  }
  return runtime.sleeping_queue.indexOf(id) >= 0;
}

function isProjectInCurrentBatch(runtime, projectId) {
  var id = normalizeProjectId(projectId);
  if (!runtime || !id) {
    return false;
  }
  return runtime.current_batch_ids.indexOf(id) >= 0;
}

function isProjectInExportBatch(runtime, projectId) {
  var id = normalizeProjectId(projectId);
  if (!runtime || !id) {
    return false;
  }
  return runtime.export_batch_ids.indexOf(id) >= 0;
}

function acceptProject(runtime, projectId) {
  var id = normalizeProjectId(projectId);
  if (!runtime || !id) {
    throw new Error("Missing project id");
  }

  if (hasProjectBeenSeen(runtime, id)) {
    return {
      accepted: false,
      duplicate: true,
      is_sleeping: isProjectSleeping(runtime, id),
      phase: String(runtime.phase || ""),
    };
  }

  runtime.session_excluded_ids[id] = true;
  if (String(runtime.phase || "") === PHASES.intake) {
    ensureProjectListed(runtime.current_batch_ids, id);
    return {
      accepted: true,
      duplicate: false,
      is_sleeping: false,
      phase: PHASES.intake,
    };
  }

  ensureProjectListed(runtime.sleeping_queue, id);
  return {
    accepted: true,
    duplicate: false,
    is_sleeping: true,
    phase: String(runtime.phase || ""),
  };
}

function collectExportBlockers(runtime, projectStates, jobStore) {
  var blockers = [];
  var states = projectStates || {};
  var jobs = jobStore || { queue: [], active: null };

  if (jobs.active) {
    blockers.push({
      type: "active_job",
      job_type: String(jobs.active.type || ""),
      project_id: normalizeProjectId(
        jobs.active.payload && jobs.active.payload.project_id,
      ),
    });
  }

  cloneArray(jobs.queue).forEach(function (job) {
    if (!job) {
      return;
    }
    blockers.push({
      type: "queued_job",
      job_type: String(job.type || ""),
      project_id: normalizeProjectId(job.payload && job.payload.project_id),
    });
  });

  cloneArray(runtime && runtime.current_batch_ids).forEach(function (projectId) {
    var state = states[projectId] || null;
    var status = String((state && state.status) || "").trim();
    if (status !== "ready_for_export" && status !== "uploaded") {
      blockers.push({
        type: "project_status",
        project_id: projectId,
        status: status || "unknown",
      });
    }
  });

  return blockers;
}

function canStartExport(runtime, projectStates, jobStore) {
  var phase = String((runtime && runtime.phase) || "");
  var blockers = collectExportBlockers(runtime, projectStates, jobStore);
  var batchIds = cloneArray(runtime && runtime.current_batch_ids);
  var allowedPhase =
    phase === PHASES.intake || phase === PHASES.blocked_error;
  return {
    ok: allowedPhase && batchIds.length > 0 && blockers.length === 0,
    blockers: blockers,
    current_batch_ids: batchIds,
    phase: phase,
  };
}

function beginExportPhase(runtime) {
  if (!runtime) {
    throw new Error("Missing runtime");
  }
  runtime.export_batch_ids = cloneArray(runtime.current_batch_ids);
  runtime.phase = PHASES.exporting;
  return cloneArray(runtime.export_batch_ids);
}

function markBatchBlocked(runtime) {
  if (!runtime) {
    throw new Error("Missing runtime");
  }
  runtime.phase = PHASES.blocked_error;
}

function getTrackedBatchProjectIds(runtime) {
  if (!runtime) {
    return [];
  }
  if (runtime.export_batch_ids.length > 0) {
    return cloneArray(runtime.export_batch_ids);
  }
  return cloneArray(runtime.current_batch_ids);
}

function beginCleaningPhase(runtime) {
  if (!runtime) {
    throw new Error("Missing runtime");
  }
  runtime.phase = PHASES.cleaning;
}

function partitionCleanupResults(results) {
  var summary = {
    completed: [],
    retryable: [],
    terminal: [],
  };

  cloneArray(results).forEach(function (entry) {
    var cleanupResult = entry && entry.cleanup_result ? entry.cleanup_result : null;
    if (cleanupResult && cleanupResult.ok) {
      summary.completed.push(entry);
      return;
    }
    if (cleanupResult && cleanupResult.retryable_lock) {
      summary.retryable.push(entry);
      return;
    }
    summary.terminal.push(entry);
  });

  return summary;
}

function isBatchCleanupComplete(runtime, projectStates) {
  if (!runtime) {
    return false;
  }

  var phase = String(runtime.phase || "");
  if (phase !== PHASES.cleaning && phase !== PHASES.blocked_error) {
    return false;
  }

  var batchIds = cloneArray(runtime.export_batch_ids);
  if (batchIds.length === 0) {
    return false;
  }

  var states = projectStates || {};
  return batchIds.every(function (projectId) {
    var state = states[projectId] || null;
    return String((state && state.status) || "") === "uploaded_cleaned";
  });
}

function markAwaitingFinalAck(runtime) {
  if (!runtime) {
    throw new Error("Missing runtime");
  }
  runtime.phase = PHASES.awaiting_final_ack;
}

function acknowledgeFinalPopup(runtime) {
  if (!runtime) {
    throw new Error("Missing runtime");
  }
  var promoted = cloneArray(runtime.sleeping_queue);
  runtime.phase = PHASES.intake;
  runtime.current_batch_ids = promoted.slice(0);
  runtime.export_batch_ids = [];
  runtime.sleeping_queue = [];
  return promoted;
}

module.exports = {
  PHASES: PHASES,
  acceptProject: acceptProject,
  acknowledgeFinalPopup: acknowledgeFinalPopup,
  beginCleaningPhase: beginCleaningPhase,
  beginExportPhase: beginExportPhase,
  buildSequenceName: buildSequenceName,
  canStartExport: canStartExport,
  collectExportBlockers: collectExportBlockers,
  createBatchRuntime: createBatchRuntime,
  getTrackedBatchProjectIds: getTrackedBatchProjectIds,
  hasProjectBeenSeen: hasProjectBeenSeen,
  isBatchCleanupComplete: isBatchCleanupComplete,
  isProjectInCurrentBatch: isProjectInCurrentBatch,
  isProjectInExportBatch: isProjectInExportBatch,
  isProjectSleeping: isProjectSleeping,
  markAwaitingFinalAck: markAwaitingFinalAck,
  markBatchBlocked: markBatchBlocked,
  partitionCleanupResults: partitionCleanupResults,
};
