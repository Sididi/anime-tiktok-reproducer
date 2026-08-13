"use strict";

/**
 * Serialize all CEP -> ExtendScript calls through the host's single script
 * engine. Long-running imports can be prioritized over queued background
 * polls, and repeated background calls can share one in-flight request.
 */

function createTransportError(label, detail) {
  var prefix = label ? String(label) + ": " : "";
  return new Error(prefix + detail);
}

function validateTransportResult(result, label) {
  if (result === undefined || result === null) {
    throw createTransportError(label, "Premiere returned no response");
  }

  var raw = String(result);
  var trimmed = raw.trim();
  if (!trimmed) {
    throw createTransportError(label, "Premiere returned an empty response");
  }
  if (/^EvalScript error\.?$/i.test(trimmed)) {
    throw createTransportError(label, trimmed);
  }
  return raw;
}

function createHostRpc(evalScriptImpl) {
  if (typeof evalScriptImpl !== "function") {
    throw new Error("createHostRpc requires an evalScript implementation");
  }

  var queue = [];
  var activeEntry = null;
  var coalescedPromises = {};
  var nextSequence = 0;

  function sortQueue() {
    queue.sort(function (left, right) {
      if (left.priority !== right.priority) {
        return right.priority - left.priority;
      }
      return left.sequence - right.sequence;
    });
  }

  function pump() {
    if (activeEntry || queue.length === 0) {
      return;
    }

    var entry = queue.shift();
    activeEntry = entry;
    var settled = false;

    function settle(callback, value) {
      if (settled) {
        return;
      }
      settled = true;
      activeEntry = null;
      if (entry.coalesceKey) {
        delete coalescedPromises[entry.coalesceKey];
      }
      callback(value);
      pump();
    }

    try {
      evalScriptImpl(entry.script, function (result) {
        try {
          settle(entry.resolve, validateTransportResult(result, entry.label));
        } catch (validationError) {
          settle(entry.reject, validationError);
        }
      });
    } catch (dispatchError) {
      settle(
        entry.reject,
        createTransportError(
          entry.label,
          dispatchError && dispatchError.message
            ? dispatchError.message
            : String(dispatchError),
        ),
      );
    }
  }

  function call(script, options) {
    var normalizedScript = String(script || "");
    if (!normalizedScript.trim()) {
      return Promise.reject(new Error("Cannot evaluate an empty host script"));
    }

    var opts = options || {};
    var coalesceKey = String(opts.coalesceKey || "").trim();
    if (coalesceKey && coalescedPromises[coalesceKey]) {
      return coalescedPromises[coalesceKey];
    }

    var parsedPriority = Number(opts.priority || 0);
    var entry = {
      script: normalizedScript,
      label: String(opts.label || "Host call").trim() || "Host call",
      priority: isFinite(parsedPriority) ? parsedPriority : 0,
      coalesceKey: coalesceKey,
      sequence: nextSequence,
      resolve: null,
      reject: null,
    };
    nextSequence += 1;

    var promise = new Promise(function (resolve, reject) {
      entry.resolve = resolve;
      entry.reject = reject;
    });

    if (coalesceKey) {
      coalescedPromises[coalesceKey] = promise;
    }
    queue.push(entry);
    sortQueue();
    pump();
    return promise;
  }

  function getState() {
    return {
      active: !!activeEntry,
      active_label: activeEntry ? activeEntry.label : null,
      queued: queue.length,
    };
  }

  return {
    call: call,
    getState: getState,
  };
}

module.exports = {
  createHostRpc: createHostRpc,
  validateTransportResult: validateTransportResult,
};
