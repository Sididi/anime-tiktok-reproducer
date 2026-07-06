/**
 * lan_tasks.js — LAN transfer engine (HTTP to the PC1 FastAPI backend).
 * Same task interface as drive_tasks.js; selected per-job by main.js after
 * a successful probe. Spec: docs/superpowers/specs/2026-07-05-lan-transfer-design.md
 */
var fs = require("fs");
var path = require("path");
var http = require("http");
var https = require("https");
var urlModule = require("url");

var constants = require("./constants");
var driveTasks = require("./drive_tasks.js");
var subtitleArchive = require("./subtitle_archive");
var downloadProgress = require("./download_progress");

var LAN_API_VERSION = 1;
var FILE_MAX_ATTEMPTS = 3;
var FILE_RETRY_DELAY_MS = 2000;
var DOWNLOAD_CONCURRENCY = 2;
var UPLOAD_PROGRESS_BUCKET_PCT = 5;
var SUBTITLES_DIRNAME = constants.SUBTITLES_DIRNAME;
var SUBTITLES_ARCHIVE_FILENAME = constants.SUBTITLES_ARCHIVE_FILENAME;
var PROJECT_CONTEXT_FILENAME = constants.PROJECT_CONTEXT_FILENAME;
var OUTPUT_FILENAME = constants.OUTPUT_FILENAME;
var FINALIZE_RENAME_MAX_ATTEMPTS = 3;
var FINALIZE_RENAME_DELAY_MS = 150;

function lanRequestOptions(settings, apiPath, method, extraHeaders) {
  var base = String(settings.lan_base_url || "").replace(/\/+$/, "");
  var parsed = urlModule.parse(base + apiPath);
  var headers = { "X-ATR-LAN-Token": String(settings.lan_token || "") };
  Object.keys(extraHeaders || {}).forEach(function (key) {
    headers[key] = extraHeaders[key];
  });
  return {
    transport: parsed.protocol === "https:" ? https : http,
    options: {
      protocol: parsed.protocol,
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.path,
      method: method || "GET",
      headers: headers,
    },
  };
}

function requestJson(settings, apiPath, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var built = lanRequestOptions(settings, apiPath, "GET");
    var req = built.transport.request(built.options, function (res) {
      var chunks = [];
      res.on("data", function (c) {
        chunks.push(c);
      });
      res.on("end", function () {
        var body = Buffer.concat(chunks).toString("utf8");
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(
            new Error(
              "LAN HTTP " + res.statusCode + " on " + apiPath + ": " + body.slice(0, 200),
            ),
          );
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (e) {
          reject(new Error("LAN invalid JSON on " + apiPath));
        }
      });
    });
    req.on("error", reject);
    if (timeoutMs) {
      req.setTimeout(timeoutMs, function () {
        req.destroy(new Error("LAN request timed out after " + timeoutMs + "ms"));
      });
    }
    req.end();
  });
}

function probe(settings) {
  if (!settings || !settings.lan_base_url) {
    return Promise.reject(new Error("lan_base_url not configured"));
  }
  var timeoutMs = Number(settings.lan_probe_timeout_ms || 2500);
  return requestJson(settings, "/api/lan/ping", timeoutMs).then(function (body) {
    if (!body || body.ok !== true) {
      throw new Error("LAN ping returned unexpected body");
    }
    if (Number(body.api_version) !== LAN_API_VERSION) {
      throw new Error(
        "LAN api_version mismatch: got " + body.api_version + ", need " + LAN_API_VERSION,
      );
    }
    return body;
  });
}

function ensureDir(dirPath) {
  if (!fs.existsSync(dirPath)) {
    fs.mkdirSync(dirPath, { recursive: true });
  }
}

function delay(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

// The download manifest is untrusted over the wire: it travels as plain HTTP
// on the LAN (no TLS), so a spoofed server or a MITM could inject
// relative_path values containing "../" segments. Even though the backend
// is trusted and strips path prefixes server-side, this client-side check
// is defense-in-depth against a tampered manifest writing files outside the
// intended download folder (e.g. overwriting arbitrary files under APPDATA).
function assertInsideDir(baseDir, candidatePath) {
  var resolvedBase = path.resolve(baseDir);
  var resolvedCandidate = path.resolve(candidatePath);
  // Contained if equal to base or under base + separator. path.sep handles Windows.
  if (
    resolvedCandidate !== resolvedBase &&
    resolvedCandidate.indexOf(resolvedBase + path.sep) !== 0
  ) {
    throw new Error("Refusing path outside download root: " + candidatePath);
  }
  return resolvedCandidate;
}

function downloadOneFile(settings, projectId, file, destination, onBytes) {
  ensureDir(path.dirname(destination));
  var apiPath =
    "/api/lan/projects/" +
    encodeURIComponent(projectId) +
    "/files/" +
    file.relative_path.split("/").map(encodeURIComponent).join("/");
  return new Promise(function (resolve, reject) {
    var built = lanRequestOptions(settings, apiPath, "GET");
    var req = built.transport.request(built.options, function (res) {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error("LAN HTTP " + res.statusCode + " downloading " + file.relative_path));
        return;
      }
      var out = fs.createWriteStream(destination);
      var received = 0;
      res.on("data", function (chunk) {
        received += chunk.length;
        onBytes(chunk.length);
      });
      res.pipe(out);
      out.on("finish", function () {
        if (Number(file.size) >= 0 && received !== Number(file.size)) {
          reject(
            new Error(
              "Size mismatch for " +
                file.relative_path +
                ": expected " +
                file.size +
                ", got " +
                received,
            ),
          );
          return;
        }
        resolve(received);
      });
      out.on("error", reject);
      res.on("error", reject);
    });
    req.on("error", reject);
    req.end();
  });
}

function downloadFileWithRetries(settings, projectId, file, destination, onBytes) {
  var attempt = 0;
  function tryOnce() {
    attempt += 1;
    var attemptBytes = 0;
    return downloadOneFile(settings, projectId, file, destination, function (n) {
      attemptBytes += n;
      onBytes(n);
    }).catch(function (err) {
      onBytes(-attemptBytes); // roll back this attempt's progress contribution
      try {
        fs.unlinkSync(destination);
      } catch (e) {}
      if (attempt >= FILE_MAX_ATTEMPTS) {
        throw err;
      }
      return delay(FILE_RETRY_DELAY_MS * attempt).then(tryOnce);
    });
  }
  return tryOnce();
}

function runWithConcurrency(items, limit, workerFn) {
  return new Promise(function (resolve, reject) {
    var nextIndex = 0;
    var active = 0;
    var failed = false;
    function launch() {
      if (failed) {
        return;
      }
      if (nextIndex >= items.length && active === 0) {
        resolve();
        return;
      }
      while (active < limit && nextIndex < items.length) {
        var item = items[nextIndex];
        nextIndex += 1;
        active += 1;
        workerFn(item).then(
          function () {
            active -= 1;
            launch();
          },
          function (err) {
            if (!failed) {
              failed = true;
              reject(err);
            }
          },
        );
      }
    }
    launch();
  });
}

function writeJsonAtomic(filePath, data) {
  var tmpPath = filePath + ".tmp";
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2));
  fs.renameSync(tmpPath, filePath);
}

// Bare fs.renameSync can transiently fail on Windows if the destination
// folder is momentarily locked (AV scan, Explorer handle, etc). This is a
// small local retry — not the full finalizeDownloadedFolderWithRetry from
// drive_tasks.js, which isn't exported. Real-Windows finalize behavior is
// validated separately in Task 11.
function renameWithRetry(sourcePath, destinationPath) {
  var attempt = 0;
  function tryRename() {
    attempt += 1;
    try {
      fs.renameSync(sourcePath, destinationPath);
      return Promise.resolve();
    } catch (err) {
      if (attempt >= FINALIZE_RENAME_MAX_ATTEMPTS) {
        return Promise.reject(err);
      }
      return delay(FINALIZE_RENAME_DELAY_MS * attempt).then(tryRename);
    }
  }
  return tryRename();
}

function extractSubtitles(targetRoot, projectId, emitProgress) {
  var archivePath = path.join(targetRoot, SUBTITLES_DIRNAME, SUBTITLES_ARCHIVE_FILENAME);
  if (!fs.existsSync(archivePath)) {
    return { extracted: false };
  }
  emitProgress({ stage: "subtitle_archive_extract_start", project_id: projectId });
  var extraction = subtitleArchive.expandSubtitleArchiveSync({
    localRootPath: targetRoot,
  });
  emitProgress({ stage: "subtitle_archive_extract_complete", project_id: projectId });
  return extraction || { extracted: true };
}

function performDownloadProject(payload, emitProgress) {
  var settings = payload.settings || {};
  var projectId = payload.project_id;
  if (!projectId) {
    return Promise.reject(new Error("Missing project_id"));
  }
  emitProgress({ stage: "resolve_folder", project_id: projectId, transfer_mode: "lan" });
  return requestJson(
    settings,
    "/api/lan/projects/" + encodeURIComponent(projectId) + "/manifest",
    30000,
  ).then(function (manifest) {
    var files = manifest.files || [];
    var folderName = String(manifest.folder_name || "project_" + projectId);
    var target = driveTasks.pickTargetBasePaths(folderName, payload.app_data_path);
    var targetRoot = path.join(target.parent, target.folderName);
    var partialRoot = targetRoot + ".partial";
    ensureDir(partialRoot);

    var totalBytes = 0;
    files.forEach(function (f) {
      totalBytes += Number(f.size || 0);
    });
    var downloadedBytes = 0;
    var progressState = downloadProgress.createProgressState();
    var startedAt = Date.now();

    emitProgress({
      stage: "download_start",
      project_id: projectId,
      file_count: files.length,
      total_bytes: totalBytes,
      target_root: targetRoot,
      transfer_mode: "lan",
    });

    function onBytes(n) {
      downloadedBytes += n;
      var summary = downloadProgress.buildSummaryEvent(progressState, {
        project_id: projectId,
        file_count: files.length,
        downloaded_bytes: downloadedBytes,
        total_bytes: totalBytes,
      });
      if (summary) {
        emitProgress(summary);
      }
    }

    return runWithConcurrency(files, DOWNLOAD_CONCURRENCY, function (file) {
      // Wrap in Promise.resolve().then so a synchronous throw from
      // assertInsideDir becomes a rejected promise: runWithConcurrency calls
      // workerFn(item).then(...) directly, and a bare synchronous throw here
      // would escape that .then attachment instead of failing the job.
      return Promise.resolve().then(function () {
        var destination = path.join(partialRoot, file.relative_path);
        assertInsideDir(partialRoot, destination);
        return downloadFileWithRetries(settings, projectId, file, destination, onBytes);
      });
    })
      .then(function () {
        return renameWithRetry(partialRoot, targetRoot);
      })
      .then(function () {
        var elapsedMs = Math.max(1, Date.now() - startedAt);
        var extraction = extractSubtitles(targetRoot, projectId, emitProgress);
        var avgMbPerSec =
          totalBytes > 0 ? totalBytes / (1024 * 1024) / (elapsedMs / 1000) : 0;
        var outputPath = path.join(targetRoot, OUTPUT_FILENAME);
        writeJsonAtomic(path.join(targetRoot, PROJECT_CONTEXT_FILENAME), {
          project_id: projectId,
          drive_folder_id: manifest.drive_folder_id || null,
          local_root: targetRoot,
          output_path: outputPath,
          downloaded_at: new Date().toISOString(),
          download_elapsed_ms: elapsedMs,
          download_avg_mb_per_sec: avgMbPerSec,
          download_file_count: files.length,
          subtitle_archive_extracted: !!(extraction && extraction.extracted),
          transfer_mode: "lan",
        });
        emitProgress({
          stage: "download_complete",
          project_id: projectId,
          target_root: targetRoot,
          output_path: outputPath,
          elapsed_ms: elapsedMs,
          avg_mb_per_sec: avgMbPerSec,
          file_count: files.length,
          total_bytes: totalBytes,
          transfer_mode: "lan",
        });
        return {
          project_id: projectId,
          drive_folder_id: manifest.drive_folder_id || null,
          drive_folder_name: folderName,
          local_root: targetRoot,
          output_path: outputPath,
          used_fallback_root: !!target.isFallback,
          download_elapsed_ms: elapsedMs,
          download_avg_mb_per_sec: avgMbPerSec,
          download_file_count: files.length,
          download_total_bytes: totalBytes,
          subtitle_archive_extracted: !!(extraction && extraction.extracted),
          orchestration_metrics: null,
          transfer_mode: "lan",
        };
      });
  });
}

function performUploadOutput(payload, emitProgress) {
  var settings = payload.settings || {};
  var projectId = payload.project_id;
  var outputPath = String(payload.output_path || "");
  var fileName = String(payload.output_file_name || path.basename(outputPath));
  if (!projectId || !outputPath) {
    return Promise.reject(new Error("Missing project_id or output_path"));
  }
  if (!fs.existsSync(outputPath)) {
    return Promise.reject(new Error("Output file not found: " + outputPath));
  }
  var totalBytes = fs.statSync(outputPath).size;
  var attempt = 0;

  function tryOnce() {
    attempt += 1;
    return new Promise(function (resolve, reject) {
      var apiPath =
        "/api/lan/projects/" + encodeURIComponent(projectId) + "/outputs/" + encodeURIComponent(fileName);
      var built = lanRequestOptions(settings, apiPath, "POST", {
        "Content-Type": "application/octet-stream",
        "Content-Length": totalBytes,
      });
      var req = built.transport.request(built.options, function (res) {
        var chunks = [];
        res.on("data", function (c) {
          chunks.push(c);
        });
        res.on("end", function () {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            reject(
              new Error(
                "LAN upload HTTP " +
                  res.statusCode +
                  ": " +
                  Buffer.concat(chunks).toString("utf8").slice(0, 200),
              ),
            );
            return;
          }
          resolve({ ok: true, transfer_mode: "lan", file_name: fileName });
        });
      });
      req.on("error", reject);
      var uploaded = 0;
      var lastEmittedBucket = -1;
      var source = fs.createReadStream(outputPath);
      // Emit at most one progress event per 5% bucket: through the worker
      // every emit is an IPC message, and per-chunk emission floods the
      // channel with thousands of messages on large outputs.
      source.on("data", function (chunk) {
        uploaded += chunk.length;
        var pct =
          totalBytes > 0 ? Math.floor((uploaded / totalBytes) * 100) : 0;
        var bucket = Math.floor(pct / UPLOAD_PROGRESS_BUCKET_PCT);
        if (bucket === lastEmittedBucket && uploaded < totalBytes) {
          return;
        }
        lastEmittedBucket = bucket;
        emitProgress({ stage: "upload_progress", uploaded_bytes: uploaded, total_bytes: totalBytes });
      });
      source.on("error", reject);
      source.pipe(req);
    }).catch(function (err) {
      if (attempt >= FILE_MAX_ATTEMPTS) {
        throw err;
      }
      return delay(FILE_RETRY_DELAY_MS * attempt).then(tryOnce);
    });
  }

  emitProgress({ stage: "upload_start", project_id: projectId, total_bytes: totalBytes, transfer_mode: "lan" });
  return tryOnce();
}

function runTask(task, payload, emitProgress) {
  var reporter = typeof emitProgress === "function" ? emitProgress : function () {};
  var safePayload = payload || {};
  if (task === "downloadProject") {
    return performDownloadProject(safePayload, reporter);
  }
  if (task === "uploadOutput") {
    return performUploadOutput(safePayload, reporter);
  }
  return Promise.reject(new Error("Unknown LAN task: " + task));
}

module.exports = {
  probe: probe,
  runTask: runTask,
};
