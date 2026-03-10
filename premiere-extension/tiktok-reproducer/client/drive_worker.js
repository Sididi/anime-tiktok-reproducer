"use strict";

var tasks = require("./drive_tasks");
var activeTask = null;
var finished = false;

function send(message) {
  if (process.send) {
    process.send(message);
  }
}

function normalizeError(error) {
  return {
    message: error && error.message ? error.message : String(error),
    stack: error && error.stack ? error.stack : null,
  };
}

function sendThenExit(message, exitCode) {
  if (finished) {
    return;
  }
  finished = true;

  function doExit() {
    process.exit(exitCode);
  }

  if (!process.send) {
    doExit();
    return;
  }

  var timeout = setTimeout(doExit, 100);
  if (timeout && typeof timeout.unref === "function") {
    timeout.unref();
  }

  try {
    process.send(message, function () {
      clearTimeout(timeout);
      doExit();
    });
  } catch (e) {
    clearTimeout(timeout);
    doExit();
  }
}

process.on("message", function (message) {
  if (!message || message.type !== "run" || finished) {
    return;
  }

  activeTask = message.task || null;
  var taskName = activeTask;
  var payload = message.payload || {};

  tasks
    .runTask(taskName, payload, function (progress) {
      send({
        type: "progress",
        task: taskName,
        progress: progress,
      });
    })
    .then(function (result) {
      sendThenExit(
        {
          type: "result",
          task: taskName,
          result: result,
        },
        0,
      );
    })
    .catch(function (error) {
      sendThenExit(
        {
          type: "error",
          task: taskName,
          error: normalizeError(error),
        },
        1,
      );
    });
});

process.on("uncaughtException", function (error) {
  sendThenExit(
    {
      type: "error",
      task: activeTask,
      error: normalizeError(error),
    },
    1,
  );
});

process.on("unhandledRejection", function (reason) {
  sendThenExit(
    {
      type: "error",
      task: activeTask,
      error: normalizeError(reason),
    },
    1,
  );
});
