"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const driveTasks = require("../tiktok-reproducer/client/drive_tasks");

test("pickTargetBasePaths prefers the internal ATR downloads directory", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "atr-drive-targets-"));
  const appDataPath = path.join(root, "AppData", "Roaming");
  const desktopParent = path.join(root, "Desktop");

  const result = driveTasks.pickTargetBasePaths("project", appDataPath, {
    fallbackParent: desktopParent,
  });

  assert.equal(
    result.parent,
    path.join(appDataPath, "Adobe", "TiktokReproducer", "downloads"),
  );
  assert.equal(result.isFallback, false);
  assert.match(result.folderName, /^project_/);
});

test("pickTargetBasePaths falls back to Desktop when the internal ATR directory is unavailable", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "atr-drive-targets-"));
  const appDataPath = path.join(root, "AppData", "Roaming");
  const preferredParent = path.join(
    appDataPath,
    "Adobe",
    "TiktokReproducer",
    "downloads",
  );
  const desktopParent = path.join(root, "Desktop");

  const result = driveTasks.pickTargetBasePaths("project", appDataPath, {
    fallbackParent: desktopParent,
    probeWritable(targetPath) {
      if (targetPath === preferredParent) {
        throw new Error("preferred directory is unavailable");
      }
    },
  });

  assert.equal(result.parent, desktopParent);
  assert.equal(result.isFallback, true);
  assert.equal(fs.existsSync(desktopParent), true);
  assert.match(result.folderName, /^project_/);
});
