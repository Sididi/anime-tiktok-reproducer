"use strict";

var fs = require("fs");
var path = require("path");
var childProcess = require("child_process");
var crypto = require("crypto");

var SUBTITLES_DIRNAME = "subtitles";
var SUBTITLES_ARCHIVE_FILENAME = "atr_subtitles.zip";
var SUBTITLE_TIMING_FILENAME = "subtitle_timings.srt";
var POWERSHELL_TIMEOUT_MS = 300000;
var TEMP_DIR_PREFIX = "__atr_subtitles_extract__";

function pathExists(targetPath) {
  try {
    return fs.existsSync(targetPath);
  } catch (e) {
    return false;
  }
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function removeIfExists(targetPath) {
  try {
    if (pathExists(targetPath)) {
      fs.rmSync(targetPath, { recursive: true, force: true });
    }
  } catch (e) {
    // best effort cleanup
  }
}

function escapePowerShellSingleQuoted(value) {
  return String(value || "").replace(/'/g, "''");
}

function buildZipExtractScript(archivePath, destinationPath) {
  return [
    "$ErrorActionPreference = 'Stop'",
    "Add-Type -AssemblyName 'System.IO.Compression.FileSystem'",
    "[System.IO.Compression.ZipFile]::ExtractToDirectory('" +
      escapePowerShellSingleQuoted(archivePath) +
      "', '" +
      escapePowerShellSingleQuoted(destinationPath) +
      "')",
  ].join("; ");
}

function runPowerShellScriptSync(script, timeoutMs) {
  var encoded = Buffer.from(String(script || ""), "utf16le").toString("base64");
  var result = childProcess.spawnSync(
    "powershell.exe",
    [
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-EncodedCommand",
      encoded,
    ],
    {
      windowsHide: true,
      encoding: "utf8",
      timeout: timeoutMs || POWERSHELL_TIMEOUT_MS,
    },
  );

  if (result.error) {
    throw result.error;
  }

  if (Number(result.status || 0) !== 0) {
    var stderr = String(result.stderr || "").trim();
    var stdout = String(result.stdout || "").trim();
    throw new Error(
      "PowerShell command failed" +
        (stderr ? ": " + stderr : stdout ? ": " + stdout : ""),
    );
  }
}

function runPowerShellScriptAsync(script, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var encoded = Buffer.from(String(script || ""), "utf16le").toString("base64");
    childProcess.execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
      ],
      {
        windowsHide: true,
        timeout: timeoutMs || POWERSHELL_TIMEOUT_MS,
        encoding: "utf8",
      },
      function (error, stdout, stderr) {
        if (error) {
          var detail = String(stderr || stdout || "").trim();
          if (detail && String(error.message || "").indexOf(detail) === -1) {
            error.message += ": " + detail;
          }
          reject(error);
          return;
        }
        resolve({
          stdout: String(stdout || ""),
          stderr: String(stderr || ""),
        });
      },
    );
  });
}

function createRandomSuffix() {
  try {
    return crypto.randomBytes(4).toString("hex");
  } catch (e) {
    return (
      String(process.pid || "0") +
      "_" +
      String(Date.now()) +
      "_" +
      String(Math.floor(Math.random() * 100000))
    );
  }
}

function createTempExtractDir(localRootPath) {
  return path.join(localRootPath, TEMP_DIR_PREFIX + createRandomSuffix());
}

function mergeDirRecursive(sourceDir, targetDir) {
  ensureDir(targetDir);
  fs.readdirSync(sourceDir).forEach(function (entryName) {
    var sourcePath = path.join(sourceDir, entryName);
    var targetPath = path.join(targetDir, entryName);
    var stat = fs.statSync(sourcePath);
    if (stat.isDirectory()) {
      mergeDirRecursive(sourcePath, targetPath);
      return;
    }
    fs.copyFileSync(sourcePath, targetPath);
  });
}

function listDirectFiles(dirPath, ignoredNames) {
  if (!dirPath || !pathExists(dirPath)) {
    return [];
  }

  var ignored = {};
  (Array.isArray(ignoredNames) ? ignoredNames : []).forEach(function (name) {
    ignored[String(name || "")] = true;
  });

  var entries = [];
  fs.readdirSync(dirPath).forEach(function (name) {
    if (ignored[name]) {
      return;
    }
    var entryPath = path.join(dirPath, name);
    try {
      if (fs.statSync(entryPath).isFile()) {
        entries.push(name);
      }
    } catch (e) {
      // ignore transient filesystem errors during validation
    }
  });
  entries.sort();
  return entries;
}

function normalizeContext(options) {
  var localRootPath = String((options && options.localRootPath) || "").trim();
  var subtitlesDir =
    String((options && options.subtitlesDir) || "").trim() ||
    path.join(localRootPath, SUBTITLES_DIRNAME);
  var archivePath =
    String((options && options.archivePath) || "").trim() ||
    path.join(subtitlesDir, SUBTITLES_ARCHIVE_FILENAME);

  return {
    localRootPath: localRootPath,
    subtitlesDir: subtitlesDir,
    archivePath: archivePath,
    requiredTimingPath: path.join(subtitlesDir, SUBTITLE_TIMING_FILENAME),
    tempExtractDir: createTempExtractDir(localRootPath || path.dirname(subtitlesDir)),
    timeoutMs: Number((options && options.timeoutMs) || POWERSHELL_TIMEOUT_MS),
  };
}

function finalizeExtraction(context) {
  var extractedTimingPath = path.join(
    context.tempExtractDir,
    SUBTITLE_TIMING_FILENAME,
  );
  if (!pathExists(extractedTimingPath)) {
    throw new Error(
      "Subtitle archive extraction did not recreate " + SUBTITLE_TIMING_FILENAME,
    );
  }

  mergeDirRecursive(context.tempExtractDir, context.subtitlesDir);

  if (!pathExists(context.requiredTimingPath)) {
    throw new Error(
      "Subtitle archive extraction did not recreate " + SUBTITLE_TIMING_FILENAME,
    );
  }

  var extractedFiles = listDirectFiles(context.subtitlesDir, [
    SUBTITLES_ARCHIVE_FILENAME,
  ]);
  if (extractedFiles.length === 0) {
    throw new Error("Subtitle archive extraction produced no files");
  }

  fs.unlinkSync(context.archivePath);

  return {
    extracted: true,
    archive_path: context.archivePath,
    extracted_file_count: extractedFiles.length,
  };
}

function defaultExtractArchiveSync(archivePath, destinationPath, timeoutMs) {
  runPowerShellScriptSync(
    buildZipExtractScript(archivePath, destinationPath),
    timeoutMs,
  );
}

function defaultExtractArchiveAsync(archivePath, destinationPath, timeoutMs) {
  return runPowerShellScriptAsync(
    buildZipExtractScript(archivePath, destinationPath),
    timeoutMs,
  );
}

function expandSubtitleArchiveSync(options) {
  var context = normalizeContext(options || {});
  var extractor =
    (options && options.extractArchiveSync) || defaultExtractArchiveSync;

  if (!pathExists(context.archivePath)) {
    return {
      extracted: false,
      archive_path: context.archivePath,
    };
  }

  try {
    extractor(context.archivePath, context.tempExtractDir, context.timeoutMs);
    return finalizeExtraction(context);
  } finally {
    removeIfExists(context.tempExtractDir);
  }
}

function expandSubtitleArchiveAsync(options) {
  var context = normalizeContext(options || {});
  var extractor =
    (options && options.extractArchiveAsync) || defaultExtractArchiveAsync;

  if (!pathExists(context.archivePath)) {
    return Promise.resolve({
      extracted: false,
      archive_path: context.archivePath,
    });
  }

  return Promise.resolve()
    .then(function () {
      return extractor(context.archivePath, context.tempExtractDir, context.timeoutMs);
    })
    .then(function () {
      return finalizeExtraction(context);
    })
    .finally(function () {
      removeIfExists(context.tempExtractDir);
    });
}

module.exports = {
  SUBTITLES_DIRNAME: SUBTITLES_DIRNAME,
  SUBTITLES_ARCHIVE_FILENAME: SUBTITLES_ARCHIVE_FILENAME,
  SUBTITLE_TIMING_FILENAME: SUBTITLE_TIMING_FILENAME,
  buildZipExtractScript: buildZipExtractScript,
  expandSubtitleArchiveSync: expandSubtitleArchiveSync,
  expandSubtitleArchiveAsync: expandSubtitleArchiveAsync,
};
