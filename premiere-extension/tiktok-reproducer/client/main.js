/**
 * Tiktok Reproducer - CEP Panel for Premiere Pro 2025
 *
 * Features:
 * - Hot-folder .trigger watcher + manual Browse & Run
 * - Local HTTP server (localhost trigger)
 * - Google Drive download/import automation
 * - Export monitoring for output.mp4 + ATR_*.mp4
 * - Managed AME export + encoder event polling
 * - Resumable Drive upload in worker process
 */

(function () {
  "use strict";

  var cs = new CSInterface();
  var fs = require("fs");
  var path = require("path");
  var os = require("os");
  var http = require("http");
  var url = require("url");
  var childProcess = require("child_process");

  var APPDATA =
    process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming");
  var LEGACY_BASE_DIR = path.join(APPDATA, "Adobe", "JSXRunner");
  var BASE_DIR = path.join(APPDATA, "Adobe", "TiktokReproducer");
  var INBOX_DIR = path.join(BASE_DIR, "inbox");
  var STATE_DIR = path.join(BASE_DIR, "state");
  var PROJECTS_STATE_DIR = path.join(STATE_DIR, "projects");
  var UPLOAD_SESSIONS_DIR = path.join(STATE_DIR, "upload_sessions");
  var SETTINGS_PATH = path.join(STATE_DIR, "settings.json");

  var DEFAULT_PORT = 48653;
  var OUTPUT_FILENAME = "output.mp4";
  var AUDIO_NO_MUSIC_OUTPUT_FILENAME = "output_no_music.wav";
  var PANEL_BUILD_ID = "2026-04-29-async-proxy-v8";
  var PROJECT_CONTEXT_FILENAME = ".atr_project_context.json";
  var PROXY_OUTPUT_SUFFIX = "__atr_proxy.mp4";
  var PROXY_MARKER_SUFFIX = ".atr_proxy.json";
  var PROXY_MARKER_VERSION = 1;
  var ATR_OUTPUT_PATTERN = /^ATR_.*\.mp4$/i;
  var KNOWN_VIDEO_EXTENSIONS = {
    ".avi": true,
    ".m4v": true,
    ".mkv": true,
    ".mov": true,
    ".mp4": true,
    ".webm": true,
  };
  var EXPORT_STABLE_MS = 10000;
  var EXPORT_POLL_INTERVAL_MS = 5000;
  var ENCODER_POLL_INTERVAL_MS = 1000;
  var FS_WATCH_RETRY_DELAY_MS = 10000;
  var CLEANUP_IMMEDIATE_MAX_ATTEMPTS = 8;
  var CLEANUP_BACKGROUND_MAX_ATTEMPTS = 3;
  var CLEANUP_RETRYABLE_MAX_PASSES = 240;
  var CLEANUP_RETRY_DELAY_MS = 15000;
  var CLEANUP_BACKOFF_BASE_MS = 250;
  var CLEANUP_BACKOFF_MAX_MS = 4000;
  var CLEANUP_NODE_RM_MAX_RETRIES = 18;
  var CLEANUP_NODE_RM_RETRY_DELAY_MS = 250;
  var CLEANUP_REMAINING_PREVIEW_LIMIT = 6;
  var PROXY_RECONCILE_MAX_ATTACH_ATTEMPTS = 120;
  var MAX_LOG_ENTRIES = 200;

  var DEFAULT_SETTINGS = {
    client_id: "",
    client_secret: "",
    refresh_token: "",
    parent_folder_id: "",
    port: DEFAULT_PORT,
    preset_epr_path: "",
    audio_preset_epr_path: "",
    delete_after_upload_default: true,
    export_audio_no_music_default: true,
    auto_proxy_non_h264_default: false,
  };

  var statusIndicator = document.getElementById("status-indicator");
  var btnBrowse = document.getElementById("btn-browse");
  var btnExportProject = document.getElementById("btn-export-project");
  var btnBrowsePreset = document.getElementById("btn-browse-preset");
  var btnBrowseAudioPreset = document.getElementById("btn-browse-audio-preset");
  var btnSaveSettings = document.getElementById("btn-save-settings");
  var btnTestDrive = document.getElementById("btn-test-drive");

  var projectSelect = document.getElementById("project-select");
  var exportAudioNoMusicCheckbox = document.getElementById(
    "chk-export-audio-no-music",
  );
  var deleteAfterUploadCheckbox = document.getElementById(
    "chk-delete-after-upload",
  );

  var settingClientId = document.getElementById("setting-client-id");
  var settingClientSecret = document.getElementById("setting-client-secret");
  var settingRefreshToken = document.getElementById("setting-refresh-token");
  var settingParentFolderId = document.getElementById(
    "setting-parent-folder-id",
  );
  var settingPort = document.getElementById("setting-port");
  var settingPresetEpr = document.getElementById("setting-preset-epr");
  var settingAudioPresetEpr = document.getElementById(
    "setting-audio-preset-epr",
  );
  var autoProxyNonH264Checkbox = document.getElementById(
    "chk-auto-proxy-non-h264",
  );
  var settingsStatus = document.getElementById("settings-status");
  var settingsSection = document.getElementById("settings-section");
  var settingsToggle = document.getElementById("settings-toggle");
  var latestProjectsSection = document.getElementById(
    "latest-projects-section",
  );
  var latestProjectsToggle = document.getElementById("latest-projects-toggle");

  var queueList = document.getElementById("queue-list");
  var projectStatusList = document.getElementById("project-status-list");
  var logEl = document.getElementById("log");

  var watcher = null;
  var processedTriggers = {};
  var localServer = null;
  var localServerStarted = false;
  var localServerError = null;

  var exportMonitors = {}; // project_id -> monitor
  var encoderJobMap = {}; // job_id -> { project_id, lease }
  var proxyRenderProcessMap = {}; // job_id -> { project_id, process, output_path }
  var proxyLeaseMap = {}; // project_id -> lease
  var proxyReconcileState = {}; // project_id -> { started_at_ms, last_attempt_ms }
  var encoderPollTimer = null;
  var driveTasksFallback = null;
  var batchRuntimeHelperModule = null;
  var runtimeStateHelperModule = null;
  var subtitleArchiveHelperModule = null;
  var panelLogHelperModule = null;
  var orchestrationMetricsHelperModule = null;
  var cleanupRetryTimers = {}; // project_id -> timeoutId

  var batchRuntime = null;
  var settings = null;
  var panelLogState = null;
  var projectStates = {}; // project_id -> state
  var jobStore = {
    queue: [],
    active: null,
  };
  var ffprobeAvailabilityChecked = false;
  var ffprobeAvailable = false;
  var ffmpegAvailabilityChecked = false;
  var ffmpegAvailable = false;

  // --- Utility ---

  function nowIso() {
    return new Date().toISOString();
  }

  function clearChildren(el) {
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function log(message, level) {
    level = level || "info";
    if (!panelLogState) {
      panelLogState = getPanelLogHelper().createLogState(MAX_LOG_ENTRIES);
    }
    var appended = getPanelLogHelper().appendLogEntry(panelLogState, {
      level: level,
      message: message,
      timestamp: new Date().toLocaleTimeString(),
    });
    while (appended.trimmed_count > 0 && logEl.firstChild) {
      logEl.removeChild(logEl.firstChild);
      appended.trimmed_count -= 1;
    }

    var entry = document.createElement("div");
    entry.className = "entry " + level;

    var ts = document.createElement("span");
    ts.className = "timestamp";
    ts.textContent = appended.entry.timestamp;

    entry.appendChild(ts);
    entry.appendChild(document.createTextNode(message));
    logEl.appendChild(entry);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function setStatus(state) {
    statusIndicator.className = state;
    var titles = {
      idle: "Not watching",
      watching: "Watching + server online",
      running: "Running task...",
      error: "Error occurred",
    };
    statusIndicator.title = titles[state] || state;
  }

  function ensureDir(dirPath) {
    try {
      fs.mkdirSync(dirPath, { recursive: true });
    } catch (e) {
      // ignore
    }
  }

  function pathExists(targetPath) {
    try {
      return fs.existsSync(targetPath);
    } catch (e) {
      return false;
    }
  }

  function ensureProjectSubtitlesExpanded(localRootPath) {
    var rootPath = String(localRootPath || "").trim();
    if (!rootPath) {
      return Promise.resolve({
        extracted: false,
      });
    }

    return getSubtitleArchiveHelper()
      .expandSubtitleArchiveAsync({
        localRootPath: rootPath,
      })
      .then(function (result) {
        return {
          extracted: !!(result && result.extracted),
          extractedFileCount: Number(
            (result && result.extracted_file_count) || 0,
          ),
        };
      });
  }

  function copyDirRecursive(sourceDir, targetDir) {
    ensureDir(targetDir);
    fs.readdirSync(sourceDir).forEach(function (entryName) {
      var sourcePath = path.join(sourceDir, entryName);
      var targetPath = path.join(targetDir, entryName);
      var stat = fs.statSync(sourcePath);
      if (stat.isDirectory()) {
        copyDirRecursive(sourcePath, targetPath);
        return;
      }
      fs.copyFileSync(sourcePath, targetPath);
    });
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
      if (!pathExists(targetPath)) {
        fs.copyFileSync(sourcePath, targetPath);
      }
    });
  }

  function migrateLegacyBaseDir() {
    if (!pathExists(LEGACY_BASE_DIR)) {
      return;
    }

    if (pathExists(BASE_DIR)) {
      try {
        mergeDirRecursive(LEGACY_BASE_DIR, BASE_DIR);
        log("Legacy JSX Runner state merged into Tiktok Reproducer", "info");
        return;
      } catch (mergeErr) {
        log("Legacy state migration failed: " + mergeErr.message, "warn");
        return;
      }
    }

    try {
      fs.renameSync(LEGACY_BASE_DIR, BASE_DIR);
      log("Legacy JSX Runner state migrated to Tiktok Reproducer", "info");
      return;
    } catch (renameErr) {
      try {
        copyDirRecursive(LEGACY_BASE_DIR, BASE_DIR);
        try {
          fs.rmSync(LEGACY_BASE_DIR, { recursive: true, force: true });
        } catch (cleanupErr) {
          // ignore best-effort legacy cleanup
        }
        log("Legacy JSX Runner state copied to Tiktok Reproducer", "info");
        return;
      } catch (copyErr) {
        log("Legacy state migration failed: " + copyErr.message, "warn");
      }
    }
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, Math.max(0, Number(ms) || 0));
    });
  }

  function isCleanupRetryableError(err) {
    var code = String(err && err.code ? err.code : "").toUpperCase();
    return (
      code === "EBUSY" ||
      code === "EPERM" ||
      code === "EACCES" ||
      code === "ENOTEMPTY"
    );
  }

  function collectCleanupRemainingEntries(rootPath, limit) {
    var normalizedRoot = String(rootPath || "").trim();
    var maxCount = Math.max(
      1,
      Number(limit || CLEANUP_REMAINING_PREVIEW_LIMIT),
    );
    var out = [];

    if (!normalizedRoot || !fs.existsSync(normalizedRoot)) {
      return out;
    }

    function walk(dirPath, relPrefix) {
      if (out.length >= maxCount) {
        return;
      }
      var entries = [];
      try {
        entries = fs.readdirSync(dirPath);
      } catch (e) {
        if (relPrefix) {
          out.push(relPrefix);
        }
        return;
      }

      entries.sort();
      for (var i = 0; i < entries.length; i += 1) {
        if (out.length >= maxCount) {
          break;
        }
        var name = entries[i];
        var absPath = path.join(dirPath, name);
        var relPath = relPrefix ? path.join(relPrefix, name) : name;
        out.push(relPath);
        try {
          var st = fs.statSync(absPath);
          if (st.isDirectory()) {
            walk(absPath, relPath);
          }
        } catch (statErr) {
          // ignore inaccessible entry
        }
      }
    }

    walk(normalizedRoot, "");
    return out.slice(0, maxCount);
  }

  function formatCleanupError(err) {
    if (!err) {
      return "Unknown cleanup failure";
    }
    var code = String(err.code || "").trim();
    var message = String(err.message || err).trim();
    if (code && message.indexOf(code + ":") !== 0) {
      return code + ": " + message;
    }
    return message || code || "Unknown cleanup failure";
  }

  function isRetryableRenameError(err) {
    var code = String(err && err.code ? err.code : "").toUpperCase();
    return (
      code === "EPERM" ||
      code === "EBUSY" ||
      code === "EACCES" ||
      code === "ENOTEMPTY"
    );
  }

  function renamePathWithRetry(sourcePath, destinationPath, options) {
    var opts = options || {};
    var maxAttempts = Math.max(1, Number(opts.maxAttempts || 10));
    var delayMs = Math.max(25, Number(opts.delayMs || 200));
    var attempt = 0;

    function tryRename() {
      attempt += 1;
      try {
        fs.renameSync(sourcePath, destinationPath);
        return Promise.resolve({
          ok: true,
          attempts: attempt,
        });
      } catch (err) {
        if (attempt < maxAttempts && isRetryableRenameError(err)) {
          return sleep(delayMs * attempt).then(tryRename);
        }
        throw err;
      }
    }

    return tryRename();
  }

  function removePathWithWindowsFallback(targetPath) {
    if (process.platform !== "win32" || !targetPath || !fs.existsSync(targetPath)) {
      return {
        attempted: false,
        ok: false,
      };
    }
    var errors = [];
    try {
      childProcess.execFileSync(
        "cmd.exe",
        ["/d", "/c", "rmdir", "/s", "/q", targetPath],
        { windowsHide: true },
      );
      return {
        attempted: true,
        ok: !fs.existsSync(targetPath),
        method: "cmd_rmdir",
      };
    } catch (e) {
      errors.push(e);
    }
    if (!fs.existsSync(targetPath)) {
      return {
        attempted: true,
        ok: true,
        method: "cmd_rmdir",
      };
    }
    try {
      childProcess.execFileSync(
        "powershell.exe",
        [
          "-NoProfile",
          "-NonInteractive",
          "-ExecutionPolicy",
          "Bypass",
          "-Command",
          "Remove-Item -LiteralPath $args[0] -Recurse -Force -ErrorAction Stop",
          targetPath,
        ],
        { windowsHide: true },
      );
      return {
        attempted: true,
        ok: !fs.existsSync(targetPath),
        method: "powershell_remove_item",
      };
    } catch (psErr) {
      errors.push(psErr);
    }
    return {
      attempted: true,
      ok: false,
      error: errors[errors.length - 1],
      errors: errors,
      method: "windows_fallbacks",
    };
  }

  function makePathWritableRecursive(targetPath) {
    var normalized = String(targetPath || "").trim();
    if (!normalized || !fs.existsSync(normalized)) {
      return;
    }

    function chmodPath(itemPath, isDirectory) {
      try {
        fs.chmodSync(itemPath, isDirectory ? 0o777 : 0o666);
      } catch (eFile) {
        // Best effort only; locked media will still be reported by deletion.
      }
    }

    function walk(dirPath) {
      var entries = [];
      try {
        entries = fs.readdirSync(dirPath);
      } catch (eRead) {
        chmodPath(dirPath, true);
        return;
      }
      for (var i = 0; i < entries.length; i += 1) {
        var entryPath = path.join(dirPath, entries[i]);
        var stat = null;
        try {
          stat = fs.lstatSync(entryPath);
        } catch (eStat) {
          chmodPath(entryPath, false);
          continue;
        }
        if (stat && stat.isDirectory()) {
          walk(entryPath);
        }
        chmodPath(entryPath, !!(stat && stat.isDirectory()));
      }
    }
    walk(normalized);
    chmodPath(normalized, true);
  }

  function removePathOnce(targetPath) {
    var normalized = String(targetPath || "").trim();
    var windowsFallbackUsed = false;
    if (!normalized) {
      return {
        ok: false,
        error: new Error("Cleanup path is empty"),
      };
    }

    if (!fs.existsSync(normalized)) {
      return {
        ok: true,
        removed: false,
      };
    }

    try {
      makePathWritableRecursive(normalized);
      if (fs.rmSync) {
        fs.rmSync(normalized, {
          recursive: true,
          force: true,
          maxRetries: CLEANUP_NODE_RM_MAX_RETRIES,
          retryDelay: CLEANUP_NODE_RM_RETRY_DELAY_MS,
        });
      } else {
        fs.rmdirSync(normalized, { recursive: true });
      }
    } catch (e) {
      var fallbackAfterError = removePathWithWindowsFallback(normalized);
      windowsFallbackUsed = !!fallbackAfterError.attempted;
      if (!fallbackAfterError.ok) {
        return {
          ok: false,
          error: fallbackAfterError.error || e,
          windows_delete_fallback_used: windowsFallbackUsed,
        };
      }
    }

    if (fs.existsSync(normalized)) {
      var fallbackAfterExists = removePathWithWindowsFallback(normalized);
      windowsFallbackUsed = !!fallbackAfterExists.attempted;
      if (!fallbackAfterExists.ok) {
        return {
          ok: false,
          error:
            fallbackAfterExists.error ||
            new Error("Path still exists after cleanup: " + normalized),
          windows_delete_fallback_used: windowsFallbackUsed,
        };
      }
    }

    return {
      ok: true,
      removed: true,
      windows_delete_fallback_used: windowsFallbackUsed,
    };
  }

  function removePathSafe(targetPath, options) {
    var normalized = String(targetPath || "").trim();
    var opts = options || {};
    var maxAttempts = Math.max(1, Number(opts.maxAttempts || 1));
    var attempt = 0;

    function tryRemove() {
      attempt += 1;
      var result = removePathOnce(normalized);
      if (result.ok) {
        result.attempts = attempt;
        return Promise.resolve(result);
      }

      var retryable = isCleanupRetryableError(result.error);
      if (retryable && attempt < maxAttempts) {
        var waitMs = Math.min(
          CLEANUP_BACKOFF_MAX_MS,
          CLEANUP_BACKOFF_BASE_MS * Math.pow(2, attempt - 1),
        );
        return sleep(waitMs).then(tryRemove);
      }

      result.attempts = attempt;
      result.retryable_lock = retryable;
      result.error = result.error || new Error("Unknown cleanup failure");
      result.error.message = formatCleanupError(result.error);
      result.remaining_entries = collectCleanupRemainingEntries(
        normalized,
        CLEANUP_REMAINING_PREVIEW_LIMIT,
      );
      return Promise.resolve(result);
    }

    return tryRemove();
  }

  function readJson(filePath, fallbackValue) {
    try {
      if (!fs.existsSync(filePath)) {
        return fallbackValue;
      }
      var raw = fs.readFileSync(filePath, "utf8");
      if (!raw.trim()) {
        return fallbackValue;
      }
      return JSON.parse(raw);
    } catch (e) {
      return fallbackValue;
    }
  }

  function writeJsonAtomic(filePath, value) {
    ensureDir(path.dirname(filePath));
    var tmp = filePath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2), "utf8");
    fs.renameSync(tmp, filePath);
  }

  function validateProjectId(projectId) {
    return /^[a-zA-Z0-9_-]+$/.test(String(projectId || ""));
  }

  function escapeForEval(value) {
    return String(value || "")
      .replace(/\\/g, "\\\\")
      .replace(/"/g, '\\"')
      .replace(/\n/g, "\\n")
      .replace(/\r/g, "");
  }

  function resolveClientDir() {
    var pathname = decodeURIComponent(window.location.pathname || "");
    if (/^\/[A-Za-z]:/.test(pathname)) {
      pathname = pathname.substring(1);
    }
    return path.dirname(pathname);
  }

  function getClientFilePath(fileName) {
    return path.join(resolveClientDir(), fileName);
  }

  function getExtensionRootPath() {
    try {
      return path.normalize(cs.getSystemPath(SystemPath.EXTENSION));
    } catch (e) {
      return resolveClientDir();
    }
  }

  function getExtensionFilePath(fileName) {
    return path.join(getExtensionRootPath(), fileName);
  }

  function getBatchRuntimeHelper() {
    if (!batchRuntimeHelperModule) {
      batchRuntimeHelperModule = require(getClientFilePath("batch_runtime.js"));
    }
    return batchRuntimeHelperModule;
  }

  function getRuntimeStateHelper() {
    if (!runtimeStateHelperModule) {
      runtimeStateHelperModule = require(getClientFilePath("runtime_state.js"));
    }
    return runtimeStateHelperModule;
  }

  function getSubtitleArchiveHelper() {
    if (!subtitleArchiveHelperModule) {
      subtitleArchiveHelperModule = require(
        getClientFilePath("subtitle_archive.js"),
      );
    }
    return subtitleArchiveHelperModule;
  }

  function getPanelLogHelper() {
    if (!panelLogHelperModule) {
      panelLogHelperModule = require(getClientFilePath("panel_log.js"));
    }
    return panelLogHelperModule;
  }

  function getOrchestrationMetricsHelper() {
    if (!orchestrationMetricsHelperModule) {
      orchestrationMetricsHelperModule = require(
        getClientFilePath("orchestration_metrics.js"),
      );
    }
    return orchestrationMetricsHelperModule;
  }

  function ensureBatchRuntime() {
    if (!batchRuntime) {
      batchRuntime = getBatchRuntimeHelper().createBatchRuntime();
    }
    return batchRuntime;
  }

  function captureAutomationLease(projectId) {
    return {
      project_id: String(projectId || "").trim(),
    };
  }

  function isAutomationLeaseActive(lease) {
    return !!lease;
  }

  function isAutomationProjectActive(projectId) {
    var id = String(projectId || "").trim();
    var runtime = ensureBatchRuntime();
    if (!id || getBatchRuntimeHelper().isProjectSleeping(runtime, id)) {
      return false;
    }
    return (
      getBatchRuntimeHelper().isProjectInCurrentBatch(runtime, id) ||
      getBatchRuntimeHelper().isProjectInExportBatch(runtime, id)
    );
  }

  function createAutomationCanceledError(projectId, reason) {
    var err = new Error(
      "Automation canceled for " +
        String(projectId || "unknown") +
        (reason ? ": " + String(reason) : ""),
    );
    err.code = "ATR_AUTOMATION_CANCELED";
    err.project_id = String(projectId || "");
    err.cancel_reason = String(reason || "superseded");
    return err;
  }

  function isAutomationCanceledError(err) {
    return !!err && String(err.code || "") === "ATR_AUTOMATION_CANCELED";
  }

  function ensureAutomationLeaseActive(lease, reason) {
    if (!isAutomationLeaseActive(lease)) {
      throw createAutomationCanceledError(
        lease && lease.project_id,
        reason || "superseded",
      );
    }
  }

  function createActiveJobController(job) {
    return {
      job_id: String((job && job.id) || ""),
      project_id: String(
        (job && job.payload && job.payload.project_id) || "",
      ).trim(),
      canceled: false,
      cancel_reason: "",
      child: null,
    };
  }

  function cancelJobController(controller, reason) {
    if (!controller || controller.canceled) {
      return false;
    }

    controller.canceled = true;
    controller.cancel_reason = String(reason || "superseded");
    try {
      if (controller.child) {
        controller.child.kill();
      }
    } catch (e) {
      // ignore kill failures during hard-precedence cancellation
    }
    controller.child = null;
    return true;
  }

  function isJobControllerCanceled(controller) {
    return !!(controller && controller.canceled);
  }

  function formatPercent(uploaded, total) {
    if (!total || total <= 0) {
      return "0%";
    }
    var pct = Math.max(0, Math.min(100, Math.round((uploaded / total) * 100)));
    return pct + "%";
  }

  function isWatchedOutputFileName(fileName) {
    var name = String(fileName || "");
    if (!name) {
      return false;
    }
    if (name.toLowerCase() === AUDIO_NO_MUSIC_OUTPUT_FILENAME.toLowerCase()) {
      return true;
    }
    if (name.toLowerCase() === OUTPUT_FILENAME.toLowerCase()) {
      return true;
    }
    return ATR_OUTPUT_PATTERN.test(name);
  }

  function clonePlainObject(value) {
    var next = {};
    Object.keys(value || {}).forEach(function (key) {
      next[key] = value[key];
    });
    return next;
  }

  function normalizeOutputPathList(paths) {
    var seen = {};
    var normalized = [];
    (paths || []).forEach(function (item) {
      var asPath = String(item || "").trim();
      if (!asPath || seen[asPath]) {
        return;
      }
      seen[asPath] = true;
      normalized.push(asPath);
    });
    return normalized;
  }

  function getExpectedOutputPaths(state) {
    var listed = normalizeOutputPathList(
      state && state.expected_outputs ? state.expected_outputs : [],
    );
    if (listed.length > 0) {
      return listed;
    }

    var fallback = [];
    if (state && state.output_path) {
      fallback.push(String(state.output_path));
    }
    if (state && state.audio_export_enabled && state.audio_output_path) {
      fallback.push(String(state.audio_output_path));
    }
    return normalizeOutputPathList(fallback);
  }

  function hasOutputUploadFinished(state, outputPath) {
    var targetPath = String(outputPath || "").trim();
    if (!targetPath || !state) {
      return false;
    }
    var uploadedOutputs = state.uploaded_outputs || {};
    return !!uploadedOutputs[targetPath];
  }

  function hasAllExpectedUploads(state) {
    var expected = getExpectedOutputPaths(state);
    if (expected.length <= 0) {
      return false;
    }
    for (var i = 0; i < expected.length; i += 1) {
      if (!hasOutputUploadFinished(state, expected[i])) {
        return false;
      }
    }
    return true;
  }

  function countUploadedExpectedOutputs(state) {
    var expected = getExpectedOutputPaths(state);
    var uploadedCount = 0;
    expected.forEach(function (outputPath) {
      if (hasOutputUploadFinished(state, outputPath)) {
        uploadedCount += 1;
      }
    });
    return {
      uploaded: uploadedCount,
      total: expected.length,
    };
  }

  function queueMissingUploadsForState(projectId, state, reason) {
    var expected = getExpectedOutputPaths(state);
    var queued = 0;
    expected.forEach(function (outputPath) {
      var normalizedPath = String(outputPath || "").trim();
      if (!normalizedPath) {
        return;
      }
      if (hasOutputUploadFinished(state, normalizedPath)) {
        return;
      }
      if (!fs.existsSync(normalizedPath)) {
        return;
      }
      if (queueUpload(projectId, reason, normalizedPath)) {
        queued += 1;
      }
    });
    return queued;
  }

  function listWatchedOutputPaths(dirPath) {
    try {
      var names = fs.readdirSync(dirPath);
      return names
        .filter(function (name) {
          return isWatchedOutputFileName(name);
        })
        .sort()
        .map(function (name) {
          return path.join(dirPath, name);
        });
    } catch (e) {
      return [];
    }
  }

  function buildProjectSequenceName(projectId) {
    return getBatchRuntimeHelper().buildSequenceName(projectId);
  }

  function getBatchPhase() {
    return String((ensureBatchRuntime().phase || "")).trim();
  }

  function listCurrentBatchProjectIds() {
    return ensureBatchRuntime().current_batch_ids.slice(0);
  }

  function listExportBatchProjectIds() {
    return ensureBatchRuntime().export_batch_ids.slice(0);
  }

  function listSleepingProjectIds() {
    return ensureBatchRuntime().sleeping_queue.slice(0);
  }

  function getTrackedBatchProjectIds() {
    return getBatchRuntimeHelper().getTrackedBatchProjectIds(ensureBatchRuntime());
  }

  // --- Settings ---

  function loadSettings() {
    var loaded = readJson(SETTINGS_PATH, {});
    var merged = {};
    Object.keys(DEFAULT_SETTINGS).forEach(function (key) {
      merged[key] = DEFAULT_SETTINGS[key];
    });
    Object.keys(loaded || {}).forEach(function (key) {
      merged[key] = loaded[key];
    });

    var parsedPort = Number(merged.port);
    if (!parsedPort || parsedPort < 1 || parsedPort > 65535) {
      merged.port = DEFAULT_PORT;
    } else {
      merged.port = Math.floor(parsedPort);
    }

    merged.delete_after_upload_default = !!merged.delete_after_upload_default;
    merged.export_audio_no_music_default =
      !!merged.export_audio_no_music_default;
    merged.auto_proxy_non_h264_default = !!merged.auto_proxy_non_h264_default;

    return merged;
  }

  function saveSettings(nextSettings) {
    settings = nextSettings;
    writeJsonAtomic(SETTINGS_PATH, settings);
  }

  function renderSettingsForm() {
    settingClientId.value = settings.client_id || "";
    settingClientSecret.value = settings.client_secret || "";
    settingRefreshToken.value = settings.refresh_token || "";
    settingParentFolderId.value = settings.parent_folder_id || "";
    settingPort.value = String(settings.port || DEFAULT_PORT);
    settingPresetEpr.value = settings.preset_epr_path || "";
    settingAudioPresetEpr.value = settings.audio_preset_epr_path || "";
    deleteAfterUploadCheckbox.checked = !!settings.delete_after_upload_default;
    exportAudioNoMusicCheckbox.checked =
      !!settings.export_audio_no_music_default;
    if (autoProxyNonH264Checkbox) {
      autoProxyNonH264Checkbox.checked = !!settings.auto_proxy_non_h264_default;
    }
  }

  function readSettingsForm() {
    var parsedPort = Number(settingPort.value || DEFAULT_PORT);
    if (!parsedPort || parsedPort < 1 || parsedPort > 65535) {
      parsedPort = DEFAULT_PORT;
    }

    return {
      client_id: String(settingClientId.value || "").trim(),
      client_secret: String(settingClientSecret.value || "").trim(),
      refresh_token: String(settingRefreshToken.value || "").trim(),
      parent_folder_id: String(settingParentFolderId.value || "").trim(),
      port: Math.floor(parsedPort),
      preset_epr_path: String(settingPresetEpr.value || "").trim(),
      audio_preset_epr_path: String(settingAudioPresetEpr.value || "").trim(),
      delete_after_upload_default: !!deleteAfterUploadCheckbox.checked,
      export_audio_no_music_default: !!exportAudioNoMusicCheckbox.checked,
      auto_proxy_non_h264_default: !!(
        autoProxyNonH264Checkbox && autoProxyNonH264Checkbox.checked
      ),
    };
  }

  function getProjectContextPath(localRootPath) {
    var normalizedRoot = String(localRootPath || "").trim();
    if (!normalizedRoot) {
      return "";
    }
    return path.join(normalizedRoot, PROJECT_CONTEXT_FILENAME);
  }

  function readProjectContext(localRootPath) {
    var contextPath = getProjectContextPath(localRootPath);
    if (!contextPath) {
      return {};
    }
    return readJson(contextPath, {}) || {};
  }

  function writeProjectContext(localRootPath, nextContext) {
    var contextPath = getProjectContextPath(localRootPath);
    if (!contextPath) {
      return {};
    }
    writeJsonAtomic(contextPath, nextContext || {});
    return nextContext || {};
  }

  function normalizeSlashes(targetPath) {
    return String(targetPath || "").replace(/\\/g, "/");
  }

  function normalizeRelativePath(rootPath, childPath) {
    return normalizeSlashes(
      path.relative(String(rootPath || ""), String(childPath || "")),
    );
  }

  function parseFfprobeRate(rateValue) {
    var raw = String(rateValue || "").trim();
    if (!raw || raw === "0" || raw === "0/0") {
      return 0;
    }
    if (raw.indexOf("/") !== -1) {
      var parts = raw.split("/");
      var numerator = Number(parts[0] || 0);
      var denominator = Number(parts[1] || 0);
      if (!numerator || !denominator) {
        return 0;
      }
      return numerator / denominator;
    }
    var parsed = Number(raw);
    return isNaN(parsed) ? 0 : parsed;
  }

  function ensureEvenPositive(value) {
    var rounded = Math.max(2, Math.round(Number(value) || 0));
    if (rounded % 2 !== 0) {
      rounded += 1;
    }
    return rounded;
  }

  function computeQuarterResolution(value) {
    return ensureEvenPositive((Number(value) || 0) / 4);
  }

  function isCodecH264(codecName) {
    var normalized = String(codecName || "").trim().toLowerCase();
    return normalized === "h264" || normalized === "avc1";
  }

  function isKnownVideoPath(filePath) {
    var ext = String(path.extname(String(filePath || "")) || "").toLowerCase();
    return !!KNOWN_VIDEO_EXTENSIONS[ext];
  }

  function collectFilesRecursive(rootPath) {
    var normalizedRoot = String(rootPath || "").trim();
    var out = [];
    if (!normalizedRoot || !fs.existsSync(normalizedRoot)) {
      return out;
    }

    function walk(dirPath) {
      var entries = [];
      try {
        entries = fs.readdirSync(dirPath);
      } catch (e) {
        return;
      }
      entries.sort();
      entries.forEach(function (entryName) {
        var entryPath = path.join(dirPath, entryName);
        var stat = null;
        try {
          stat = fs.statSync(entryPath);
        } catch (statErr) {
          stat = null;
        }
        if (!stat) {
          return;
        }
        if (stat.isDirectory()) {
          walk(entryPath);
          return;
        }
        if (stat.isFile()) {
          out.push(entryPath);
        }
      });
    }

    walk(normalizedRoot);
    return out;
  }

  function isFfprobeAvailable() {
    if (ffprobeAvailabilityChecked) {
      return ffprobeAvailable;
    }
    ffprobeAvailabilityChecked = true;
    try {
      childProcess.execFileSync("ffprobe", ["-version"], {
        stdio: ["ignore", "ignore", "ignore"],
        windowsHide: true,
      });
      ffprobeAvailable = true;
    } catch (e) {
      ffprobeAvailable = false;
    }
    return ffprobeAvailable;
  }

  function isFfmpegAvailable() {
    if (ffmpegAvailabilityChecked) {
      return ffmpegAvailable;
    }
    ffmpegAvailabilityChecked = true;
    try {
      childProcess.execFileSync("ffmpeg", ["-version"], {
        stdio: ["ignore", "ignore", "ignore"],
        windowsHide: true,
      });
      ffmpegAvailable = true;
    } catch (e) {
      ffmpegAvailable = false;
    }
    return ffmpegAvailable;
  }

  function probeVideoWithFfprobe(filePath) {
    var args = [
      "-v",
      "error",
      "-print_format",
      "json",
      "-show_streams",
      "-show_format",
      String(filePath || ""),
    ];
    var raw = childProcess.execFileSync("ffprobe", args, {
      encoding: "utf8",
      windowsHide: true,
    });
    var parsed = JSON.parse(raw || "{}");
    var streams = Array.isArray(parsed.streams) ? parsed.streams : [];
    var videoStream = null;
    var audioStream = null;
    var audioStreamCount = 0;
    for (var i = 0; i < streams.length; i += 1) {
      if (String((streams[i] && streams[i].codec_type) || "") === "video") {
        videoStream = streams[i];
      } else if (
        String((streams[i] && streams[i].codec_type) || "") === "audio"
      ) {
        audioStreamCount += 1;
        if (!audioStream) {
          audioStream = streams[i];
        }
      }
    }
    if (!videoStream) {
      return {
        hasVideo: false,
      };
    }
    var fps =
      parseFfprobeRate(videoStream.avg_frame_rate) ||
      parseFfprobeRate(videoStream.r_frame_rate);
    return {
      hasVideo: true,
      codec_name: String(videoStream.codec_name || "").toLowerCase(),
      width: Math.max(0, Number(videoStream.width || 0)),
      height: Math.max(0, Number(videoStream.height || 0)),
      fps: fps > 0 ? fps : 0,
      audio_codec_name: audioStream
        ? String(audioStream.codec_name || "").toLowerCase()
        : "",
      audio_channels: Math.max(
        0,
        Number(audioStream && audioStream.channels ? audioStream.channels : 0),
      ),
      audio_sample_rate: Math.max(
        0,
        Number(
          audioStream && audioStream.sample_rate ? audioStream.sample_rate : 0,
        ),
      ),
      audio_stream_count: audioStreamCount,
    };
  }

  function buildProxyPlanForLocalProject(localRootPath, projectId) {
    var normalizedRoot = String(localRootPath || "").trim();
    var enabled = !!settings.auto_proxy_non_h264_default;
    var plan = {
      enabled: enabled,
      project_id: String(projectId || "").trim(),
      auto_enable_proxy_view: true,
      required_codec: "h264",
      required_scale_divisor: 4,
      detection_mode: "ffprobe",
      targets: [],
    };

    if (!enabled || !normalizedRoot) {
      return plan;
    }

    var sourcesRoot = path.join(normalizedRoot, "sources");
    if (!fs.existsSync(sourcesRoot)) {
      return plan;
    }

    var canUseFfprobe = isFfprobeAvailable();
    if (!canUseFfprobe) {
      plan.detection_mode = "extension_only";
      plan.ffprobe_warning =
        "ffprobe is unavailable; proxy classification is using file extensions only.";
    }

    collectFilesRecursive(sourcesRoot).forEach(function (absolutePath) {
      if (!isKnownVideoPath(absolutePath)) {
        return;
      }

      var target = {
        media_path: normalizeSlashes(absolutePath),
        relative_source_path: normalizeRelativePath(normalizedRoot, absolutePath),
        source_codec: "",
        needs_proxy: false,
      };

      if (canUseFfprobe) {
        try {
          var probe = probeVideoWithFfprobe(absolutePath);
          if (!probe || !probe.hasVideo) {
            return;
          }
          target.source_codec = String(probe.codec_name || "").toLowerCase();
          target.source_width = Math.max(0, Number(probe.width || 0));
          target.source_height = Math.max(0, Number(probe.height || 0));
          target.source_fps = Number(probe.fps || 0);
          target.source_audio_codec = String(
            probe.audio_codec_name || "",
          ).toLowerCase();
          target.source_audio_channels = Math.max(
            0,
            Number(probe.audio_channels || 0),
          );
          target.source_audio_sample_rate = Math.max(
            0,
            Number(probe.audio_sample_rate || 0),
          );
          target.source_audio_stream_count = Math.max(
            0,
            Number(probe.audio_stream_count || 0),
          );
          if (target.source_width > 0) {
            target.expected_proxy_width = computeQuarterResolution(
              target.source_width,
            );
          }
          if (target.source_height > 0) {
            target.expected_proxy_height = computeQuarterResolution(
              target.source_height,
            );
          }
          target.needs_proxy = !isCodecH264(target.source_codec);
        } catch (probeErr) {
          target.ffprobe_error = probeErr.message;
          target.needs_proxy =
            String(path.extname(absolutePath) || "").toLowerCase() !== ".mp4";
        }
      } else {
        target.needs_proxy =
          String(path.extname(absolutePath) || "").toLowerCase() !== ".mp4";
      }

      plan.targets.push(target);
    });

    return plan;
  }

  function persistProxyPlanToContext(localRootPath, proxyPlan) {
    var nextContext = readProjectContext(localRootPath);
    nextContext.proxy_plan = proxyPlan || {
      enabled: false,
      targets: [],
    };
    return writeProjectContext(localRootPath, nextContext);
  }

  function summarizeProxyPlan(proxyPlan) {
    var summary = {
      total_video_targets: 0,
      proxy_needed_count: 0,
    };
    var targets = proxyPlan && Array.isArray(proxyPlan.targets)
      ? proxyPlan.targets
      : [];
    summary.total_video_targets = targets.length;
    targets.forEach(function (target) {
      if (target && target.needs_proxy) {
        summary.proxy_needed_count += 1;
      }
    });
    return summary;
  }

  function computeManagedProxyOutputPath(localRootPath, target) {
    var rootPath = String(localRootPath || "");
    var relativeSourcePath = normalizeSlashes(
      (target && target.relative_source_path) || "",
    );
    var sourceName = path.basename(
      relativeSourcePath || String((target && target.media_path) || ""),
    );
    var parsed = path.parse(sourceName);
    var baseName = parsed.name || sourceName;
    var parentRelative = normalizeSlashes(path.dirname(relativeSourcePath || ""));
    var relativeProxyDir = "proxies";
    if (parentRelative && parentRelative !== "." && parentRelative !== "/") {
      relativeProxyDir = path.join.apply(
        path,
        ["proxies"].concat(parentRelative.split("/").filter(Boolean)),
      );
    }
    return path.join(rootPath, relativeProxyDir, baseName + PROXY_OUTPUT_SUFFIX);
  }

  function getProxyMarkerPath(outputPath) {
    return String(outputPath || "") + PROXY_MARKER_SUFFIX;
  }

  function readProxyMarker(outputPath) {
    return readJson(getProxyMarkerPath(outputPath), null);
  }

  function isCleanProxyOutput(outputPath) {
    var normalizedOutputPath = String(outputPath || "").trim();
    if (!normalizedOutputPath || !fs.existsSync(normalizedOutputPath)) {
      return false;
    }
    var marker = readProxyMarker(normalizedOutputPath);
    return !!(
      marker &&
      Number(marker.marker_version || 0) === PROXY_MARKER_VERSION &&
      marker.panel_build_id
    );
  }

  function writeCleanProxyMarker(outputPath, target) {
    writeJsonAtomic(getProxyMarkerPath(outputPath), {
      marker_version: PROXY_MARKER_VERSION,
      panel_build_id: PANEL_BUILD_ID,
      created_at: nowIso(),
      output_path: String(outputPath || ""),
      media_path: String((target && target.media_path) || ""),
      source_codec: String((target && target.source_codec) || ""),
      expected_proxy_width: Number((target && target.expected_proxy_width) || 0),
      expected_proxy_height: Number((target && target.expected_proxy_height) || 0),
    });
  }

  function removeProxyOutputAndMarker(outputPath) {
    var normalizedOutputPath = String(outputPath || "").trim();
    if (!normalizedOutputPath) {
      return;
    }
    try {
      if (fs.existsSync(normalizedOutputPath)) {
        fs.unlinkSync(normalizedOutputPath);
      }
    } catch (eOutput) {}
    try {
      var markerPath = getProxyMarkerPath(normalizedOutputPath);
      if (fs.existsSync(markerPath)) {
        fs.unlinkSync(markerPath);
      }
    } catch (eMarker) {}
  }

  function getProxyRenderTempPath(outputPath, jobId) {
    var parsed = path.parse(String(outputPath || ""));
    return path.join(
      parsed.dir,
      "." + parsed.name + "." + String(jobId || Date.now()) + ".rendering.mp4",
    );
  }

  function hasActiveProxyRenderForOutput(outputPath) {
    var normalizedOutputPath = path.normalize(String(outputPath || ""));
    return Object.keys(proxyRenderProcessMap).some(function (jobId) {
      var entry = proxyRenderProcessMap[jobId];
      return (
        entry &&
        path.normalize(String(entry.output_path || "")) === normalizedOutputPath
      );
    });
  }

  function buildFfmpegProxyArgs(target, outputPath, audioMode) {
    var width = ensureEvenPositive(
      Number(target && target.expected_proxy_width) || 480,
    );
    var height = ensureEvenPositive(
      Number(target && target.expected_proxy_height) || 270,
    );
    var args = [
      "-hide_banner",
      "-y",
      "-i",
      String((target && target.media_path) || ""),
      "-map",
      "0:v:0",
    ];
    var hasAudio =
      Math.max(0, Number(target && target.source_audio_stream_count || 0)) > 0;
    if (hasAudio) {
      args.push("-map", "0:a?");
    } else {
      args.push("-an");
    }
    args.push(
      "-vf",
      "scale=" + width + ":" + height + ":flags=bicubic",
      "-c:v",
      "libx264",
      "-preset",
      "veryfast",
      "-crf",
      "28",
      "-pix_fmt",
      "yuv420p",
      "-fps_mode",
      "passthrough",
    );
    if (hasAudio) {
      if (audioMode === "aac") {
        args.push("-c:a", "aac");
        if (Number(target && target.source_audio_sample_rate || 0) > 0) {
          args.push("-ar", String(Number(target.source_audio_sample_rate)));
        }
        if (Number(target && target.source_audio_channels || 0) > 0) {
          args.push("-ac", String(Number(target.source_audio_channels)));
        }
        args.push("-b:a", Number(target.source_audio_channels || 0) > 2 ? "192k" : "128k");
      } else {
        args.push("-c:a", "copy");
      }
    }
    args.push("-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart");
    args.push(String(outputPath || ""));
    return args;
  }

  function cancelLocalProxyRenderProcessesForExport() {
    var canceled = 0;
    Object.keys(proxyRenderProcessMap).forEach(function (jobId) {
      var entry = proxyRenderProcessMap[jobId];
      if (!entry) {
        return;
      }
      entry.canceled = true;
      canceled += 1;
      try {
        if (entry.process && !entry.process.killed) {
          entry.process.kill();
        }
      } catch (killErr) {}
      delete proxyRenderProcessMap[jobId];
    });
    return canceled;
  }

  function updateProxyJobCompletion(projectId, jobId, errMessage) {
    var state = getProjectState(projectId) || {};
    var proxyJobIds = Array.isArray(state.proxy_job_ids)
      ? state.proxy_job_ids.slice(0)
      : [];
    var remainingJobIds = proxyJobIds.filter(function (candidateJobId) {
      return String(candidateJobId || "") !== String(jobId || "");
    });
    var patch = {
      proxy_job_ids: remainingJobIds,
      proxy_pending_count: Math.max(
        0,
        Number(state.proxy_pending_count || 0) - 1,
      ),
      proxy_last_run_at: nowIso(),
    };
    if (errMessage) {
      patch.proxy_status = remainingJobIds.length > 0 ? "proxying" : "warning";
      patch.proxy_error = errMessage;
    } else if (String(state.proxy_status || "").trim() !== "canceled") {
      patch.proxy_status = "proxying";
      patch.proxy_error = null;
    }
    upsertProjectState(projectId, patch);
  }

  function spawnFfmpegProxyJob(projectId, localRootPath, target, lease, audioMode) {
    var outputPath = computeManagedProxyOutputPath(localRootPath, target);
    ensureDir(path.dirname(outputPath));
    var jobId =
      "ffmpeg_proxy_" +
      String(projectId || "project") +
      "_" +
      Date.now() +
      "_" +
      Math.random().toString(16).slice(2);
    var tempOutputPath = getProxyRenderTempPath(outputPath, jobId);
    removeProxyOutputAndMarker(tempOutputPath);
    var args = buildFfmpegProxyArgs(target, tempOutputPath, audioMode || "copy");
    var child = childProcess.spawn("ffmpeg", args, {
      stdio: ["ignore", "ignore", "pipe"],
      windowsHide: true,
    });
    var entry = {
      project_id: projectId,
      process: child,
      output_path: outputPath,
      temp_output_path: tempOutputPath,
      media_path: target.media_path,
      lease: lease || null,
      audio_mode: audioMode || "copy",
      canceled: false,
      last_stderr: "",
    };
    proxyRenderProcessMap[jobId] = entry;
    encoderJobMap[jobId] = {
      project_id: String(projectId || "").trim(),
      lease: lease || null,
      render_kind: "proxy",
      engine: "ffmpeg",
    };
    if (child.stderr) {
      child.stderr.on("data", function (chunk) {
        entry.last_stderr = (entry.last_stderr + String(chunk || "")).slice(-1600);
      });
    }
    child.on("error", function (err) {
      delete proxyRenderProcessMap[jobId];
      delete encoderJobMap[jobId];
      updateProxyJobCompletion(projectId, jobId, err.message || "ffmpeg failed");
    });
    child.on("close", function (code) {
      var latestEntry = proxyRenderProcessMap[jobId] || entry;
      delete proxyRenderProcessMap[jobId];
      delete encoderJobMap[jobId];
      if (latestEntry.canceled) {
        removeProxyOutputAndMarker(tempOutputPath);
        return;
      }
      if (!isAutomationProjectActive(projectId)) {
        removeProxyOutputAndMarker(tempOutputPath);
        return;
      }
      if (code !== 0) {
        if ((latestEntry.audio_mode || "copy") === "copy") {
          removeProxyOutputAndMarker(tempOutputPath);
          log(
            "Proxy audio copy failed for " +
              projectId +
              "; retrying proxy render with AAC audio",
            "warn",
          );
          var retryJobId = spawnFfmpegProxyJob(
            projectId,
            localRootPath,
            target,
            lease,
            "aac",
          );
          var retryState = getProjectState(projectId) || {};
          var retryJobIds = Array.isArray(retryState.proxy_job_ids)
            ? retryState.proxy_job_ids.slice(0)
            : [];
          retryJobIds = retryJobIds.filter(function (candidateJobId) {
            return String(candidateJobId || "") !== String(jobId || "");
          });
          if (retryJobIds.indexOf(retryJobId) === -1) {
            retryJobIds.push(retryJobId);
          }
          upsertProjectState(projectId, {
            proxy_job_ids: retryJobIds,
            proxy_pending_count: Math.max(
              1,
              Number(retryState.proxy_pending_count || 1),
            ),
            proxy_status: "proxying",
            proxy_error: null,
            proxy_last_run_at: nowIso(),
          });
          return;
        }
        removeProxyOutputAndMarker(tempOutputPath);
        updateProxyJobCompletion(
          projectId,
          jobId,
          "ffmpeg proxy render failed for " +
            path.basename(String(target.media_path || "")) +
            ": " +
            (latestEntry.last_stderr || "exit code " + code),
        );
        return;
      }
      removeProxyOutputAndMarker(outputPath);
      renamePathWithRetry(tempOutputPath, outputPath, {
        maxAttempts: 10,
        delayMs: 200,
      })
        .then(function () {
          writeCleanProxyMarker(outputPath, target);
          updateProxyJobCompletion(projectId, jobId, null);
          log(
            "Proxy render completed for " +
              projectId +
              " (" +
              path.basename(outputPath) +
              ")",
            "info",
          );
        })
        .catch(function (renameErr) {
          removeProxyOutputAndMarker(tempOutputPath);
          updateProxyJobCompletion(
            projectId,
            jobId,
            "Proxy finalize failed for " +
              path.basename(outputPath) +
              ": " +
              (renameErr && renameErr.message ? renameErr.message : renameErr),
          );
        });
    });
    return jobId;
  }

  function scheduleProxyRenderingSidecar(projectId, localRootPath, proxyPlan, lease) {
    proxyLeaseMap[String(projectId || "").trim()] = lease || null;
    if (!proxyPlan || !proxyPlan.enabled) {
      return Promise.resolve({
        ok: true,
        scheduled: false,
        project_id: projectId,
        target_count: 0,
        job_ids: [],
      });
    }
    if (!isFfmpegAvailable()) {
      return Promise.reject(
        new Error("ffmpeg is unavailable; cannot create attachable proxies"),
      );
    }
    var targets = Array.isArray(proxyPlan.targets) ? proxyPlan.targets : [];
    var jobIds = [];
    var existingOutputs = 0;
    targets.forEach(function (target) {
      if (!target || !target.needs_proxy) {
        return;
      }
      var outputPath = computeManagedProxyOutputPath(localRootPath, target);
      if (isCleanProxyOutput(outputPath)) {
        existingOutputs += 1;
        return;
      }
      if (fs.existsSync(outputPath)) {
        removeProxyOutputAndMarker(outputPath);
      }
      if (hasActiveProxyRenderForOutput(outputPath)) {
        return;
      }
      jobIds.push(spawnFfmpegProxyJob(projectId, localRootPath, target, lease, "copy"));
    });
    if (jobIds.length > 0) {
      registerProxyEncoderJobs(projectId, jobIds, lease);
      log(
        "Queued " +
          jobIds.length +
          " timing-preserving proxy render(s) for " +
          projectId,
        "info",
      );
    } else if (existingOutputs > 0) {
      log(
        "Found " +
          existingOutputs +
          " existing proxy output(s) for " +
          projectId +
          "; waiting for attach",
        "info",
      );
    }
    return Promise.resolve({
      ok: true,
      scheduled: jobIds.length > 0,
      project_id: projectId,
      target_count: targets.length,
      job_ids: jobIds,
      existing_outputs: existingOutputs,
    });
  }

  function removeDirtyProxyOutputs(localRootPath, proxyPlan) {
    var targets = proxyPlan && Array.isArray(proxyPlan.targets)
      ? proxyPlan.targets
      : [];
    var removed = 0;
    targets.forEach(function (target) {
      if (!target || !target.needs_proxy) {
        return;
      }
      var outputPath = computeManagedProxyOutputPath(localRootPath, target);
      if (fs.existsSync(outputPath) && !isCleanProxyOutput(outputPath)) {
        removeProxyOutputAndMarker(outputPath);
        removed += 1;
      }
    });
    return removed;
  }

  function patchImportProjectForAsyncProxies(importPath) {
    var normalizedImportPath = String(importPath || "").trim();
    if (!normalizedImportPath || !fs.existsSync(normalizedImportPath)) {
      return {
        patched: false,
        reason: "missing_import_project",
      };
    }

    var source = fs.readFileSync(normalizedImportPath, "utf8");
    if (source.indexOf("ATR_ASYNC_PROXY_IMPORT_PATCH") !== -1) {
      return {
        patched: false,
        reason: "already_patched",
      };
    }

    var functionStart = source.indexOf(
      "function importMediaPathsWithProxyRouting(importPaths, targetBin) {",
    );
    if (functionStart < 0) {
      return {
        patched: false,
        reason: "proxy_routing_function_not_found",
      };
    }

    var nextFunction = source.indexOf(
      "\n  function buildMogrtSubtitlePlacement",
      functionStart,
    );
    if (nextFunction < 0) {
      return {
        patched: false,
        reason: "proxy_routing_function_end_not_found",
      };
    }

    var replacement = [
      "function importMediaPathsWithProxyRouting(importPaths, targetBin) {",
      "    // ATR_ASYNC_PROXY_IMPORT_PATCH: import high-res media normally;",
      "    // proxy rendering and attach are owned by the CEP sidecar flow.",
      "    var passthrough = [];",
      "    var seenPassthrough = {};",
      "    var bin = targetBin || getProjectSearchRoot();",
      "    var importedCount = 0;",
      "",
      "    if (!importPaths || !importPaths.length || !app || !app.project) {",
      "      return importedCount;",
      "    }",
      "",
      "    try {",
      "      if (app.project.setEnableTranscodeOnIngest) {",
      "        app.project.setEnableTranscodeOnIngest(false);",
      "      }",
      "    } catch (eDisableIngest) {}",
      "",
      "    for (var i = 0; i < importPaths.length; i++) {",
      "      var candidatePath = importPaths[i];",
      "      var normalizedPath = normalizeComparePath(candidatePath);",
      "      if (!normalizedPath || seenPassthrough[normalizedPath]) continue;",
      "      seenPassthrough[normalizedPath] = true;",
      "      passthrough.push(candidatePath);",
      "    }",
      "",
      "    if (passthrough.length > 0) {",
      "      app.project.importFiles(passthrough, true, bin, false);",
      "      importedCount += passthrough.length;",
      "    }",
      "",
      "    return importedCount;",
      "  }",
      "",
    ].join("\n");

    var nextSource =
      source.substring(0, functionStart) +
      replacement +
      source.substring(nextFunction);
    fs.writeFileSync(normalizedImportPath, nextSource, "utf8");
    return {
      patched: true,
      reason: "patched",
    };
  }

  function registerProxyEncoderJobs(projectId, jobIds, lease) {
    var ids = Array.isArray(jobIds) ? jobIds : [];
    ids.forEach(function (jobId) {
      var normalizedJobId = String(jobId || "").trim();
      if (!normalizedJobId) {
        return;
      }
      encoderJobMap[normalizedJobId] = {
        project_id: String(projectId || "").trim(),
        lease: lease || null,
        render_kind: "proxy",
      };
    });
  }

  function ensureAtrProjectProxiesInHost(projectId, localRootPath, proxyPlan, lease) {
    proxyLeaseMap[String(projectId || "").trim()] = lease || null;
    var normalizedRoot = normalizeSlashes(localRootPath);
    var proxyPresetTemplatePath = normalizeSlashes(
      getExtensionFilePath("assets/ATR Proxy H264.epr"),
    );
    var plan = proxyPlan || {
      enabled: false,
      targets: [],
    };

    if (!plan.enabled) {
      return Promise.resolve({
        ok: true,
        scheduled: false,
      });
    }

    var hostCall =
      'scheduleAtrProjectProxies("' +
      escapeForEval(normalizedRoot) +
      '","' +
      escapeForEval(JSON.stringify(plan)) +
      '","' +
      escapeForEval(proxyPresetTemplatePath) +
      '")';

    return evalHost(hostCall).then(function (result) {
      var raw = String(result || "");
      if (raw.indexOf("ERROR:") === 0) {
        throw new Error(raw);
      }
      var parsed = {};
      try {
        parsed = JSON.parse(raw || "{}");
      } catch (parseErr) {
        throw new Error("Invalid proxy host response: " + raw);
      }
      if (
        !parsed.scheduled &&
        (Object.prototype.hasOwnProperty.call(parsed, "queued") ||
          Object.prototype.hasOwnProperty.call(parsed, "attached") ||
          Object.prototype.hasOwnProperty.call(parsed, "errors"))
      ) {
        handleProxySummaryEvent({
          type: "proxy_summary",
          project_id: String(projectId || "").trim(),
          detail: parsed,
        });
      }
      return parsed;
    });
  }

  function scheduleAtrProjectProxyEncodingInHost(
    projectId,
    localRootPath,
    proxyPlan,
    lease,
  ) {
    proxyLeaseMap[String(projectId || "").trim()] = lease || null;
    var normalizedRoot = normalizeSlashes(localRootPath);
    var proxyPresetTemplatePath = normalizeSlashes(
      getExtensionFilePath("assets/ATR Proxy H264.epr"),
    );
    var plan = proxyPlan || {
      enabled: false,
      targets: [],
    };

    if (!plan.enabled) {
      return Promise.resolve({
        ok: true,
        scheduled: false,
      });
    }

    var hostCall =
      'scheduleAtrProjectProxyEncoding("' +
      escapeForEval(normalizedRoot) +
      '","' +
      escapeForEval(JSON.stringify(plan)) +
      '","' +
      escapeForEval(proxyPresetTemplatePath) +
      '")';

    return evalHost(hostCall).then(function (result) {
      var raw = String(result || "");
      if (raw.indexOf("ERROR:") === 0) {
        throw new Error(raw);
      }
      var parsed = {};
      try {
        parsed = JSON.parse(raw || "{}");
      } catch (parseErr) {
        throw new Error("Invalid proxy schedule response: " + raw);
      }
      if (
        !parsed.scheduled &&
        (Object.prototype.hasOwnProperty.call(parsed, "queued") ||
          Object.prototype.hasOwnProperty.call(parsed, "existing_outputs") ||
          Object.prototype.hasOwnProperty.call(parsed, "errors"))
      ) {
        handleProxySummaryEvent({
          type: "proxy_summary",
          project_id: String(projectId || "").trim(),
          detail: parsed,
        });
      }
      return parsed;
    });
  }

  function queueAtrProjectProxiesInHost(projectId, localRootPath, proxyPlan, lease) {
    proxyLeaseMap[String(projectId || "").trim()] = lease || null;
    var normalizedRoot = normalizeSlashes(localRootPath);
    var proxyPresetTemplatePath = normalizeSlashes(
      getExtensionFilePath("assets/ATR Proxy H264.epr"),
    );
    var plan = proxyPlan || {
      enabled: false,
      targets: [],
    };

    if (!plan.enabled) {
      return Promise.resolve({
        ok: true,
        queued: 0,
        skipped_h264: 0,
        existing_outputs: 0,
        errors: [],
        job_ids: [],
      });
    }

    var hostCall =
      'queueAtrProjectProxyEncoding("' +
      escapeForEval(normalizedRoot) +
      '","' +
      escapeForEval(JSON.stringify(plan)) +
      '","' +
      escapeForEval(proxyPresetTemplatePath) +
      '")';

    return evalHost(hostCall).then(function (result) {
      var raw = String(result || "");
      if (raw.indexOf("ERROR:") === 0) {
        throw new Error(raw);
      }
      try {
        return JSON.parse(raw || "{}");
      } catch (parseErr) {
        throw new Error("Invalid proxy queue response: " + raw);
      }
    });
  }

  function reconcileAtrProjectProxiesInHost(projectId, localRootPath, proxyPlan) {
    var normalizedRoot = normalizeSlashes(localRootPath);
    var plan = proxyPlan || {
      enabled: false,
      targets: [],
    };

    if (!plan.enabled) {
      return Promise.resolve({
        ok: true,
        attached: 0,
        already_compliant: 0,
        pending: 0,
        missing_items: 0,
        missing_outputs: 0,
        attach_pending: 0,
        errors: [],
        attach_pending_errors: [],
        ignored_items: 0,
        completed_targets: 0,
        total_targets: 0,
      });
    }

    var hostCall =
      'reconcileAtrProjectProxies("' +
      escapeForEval(normalizedRoot) +
      '","' +
      escapeForEval(JSON.stringify(plan)) +
      '")';

    return evalHost(hostCall).then(function (result) {
      var raw = String(result || "");
      if (raw.indexOf("ERROR:") === 0) {
        throw new Error(raw);
      }
      try {
        return JSON.parse(raw || "{}");
      } catch (parseErr) {
        throw new Error("Invalid proxy reconcile response: " + raw);
      }
    });
  }

  function prepareMediaEncoderForExportInHost() {
    return evalHost("cancelAtrProxyRenderingAndClearMediaEncoder()").then(
      function (result) {
        var raw = String(result || "");
        if (raw.indexOf("ERROR:") === 0) {
          throw new Error(raw);
        }
        try {
          return JSON.parse(raw || "{}");
        } catch (parseErr) {
          throw new Error("Invalid AME clear response: " + raw);
        }
      },
    );
  }

  function preflightManagedBatchExportInHost(batchIds) {
    var entries = [];
    (batchIds || []).forEach(function (projectId) {
      var state = getProjectState(projectId);
      if (!state || hasAllExpectedUploads(state)) {
        return;
      }
      entries.push({
        project_id: projectId,
        sequence_name: String(
          state.sequence_name || buildProjectSequenceName(projectId),
        ),
      });
    });
    if (entries.length === 0) {
      return Promise.resolve({
        ok: true,
        checked: 0,
        missing: [],
      });
    }
    var hostCall =
      'preflightManagedBatchExport("' +
      escapeForEval(JSON.stringify(entries)) +
      '")';
    return evalHost(hostCall).then(function (result) {
      var raw = String(result || "");
      if (raw.indexOf("ERROR:") === 0) {
        throw new Error(raw);
      }
      try {
        return JSON.parse(raw || "{}");
      } catch (parseErr) {
        throw new Error("Invalid batch export preflight response: " + raw);
      }
    });
  }

  function clearLocalProxyTrackingForExport() {
    var droppedJobs = cancelLocalProxyRenderProcessesForExport();
    Object.keys(encoderJobMap).forEach(function (jobId) {
      var mapped = encoderJobMap[jobId];
      var renderKind = String((mapped && mapped.render_kind) || "").trim();
      if (renderKind === "proxy") {
        delete encoderJobMap[jobId];
        droppedJobs += 1;
      }
    });
    proxyLeaseMap = {};
    proxyReconcileState = {};

    Object.keys(projectStates).forEach(function (projectId) {
      var state = projectStates[projectId] || {};
      var proxyStatus = String(state.proxy_status || "").trim();
      if (
        proxyStatus !== "starting" &&
        proxyStatus !== "proxying" &&
        proxyStatus !== "planned"
      ) {
        return;
      }
      upsertProjectState(projectId, {
        proxy_status: "canceled",
        proxy_error: null,
        proxy_pending_count: 0,
        proxy_job_ids: [],
        proxy_last_run_at: nowIso(),
      });
    });

    return droppedJobs;
  }

  function setSettingsStatus(message, isError) {
    settingsStatus.textContent = message || "";
    settingsStatus.className = isError ? "status-error" : "status-ok";
  }

  function setSectionCollapsed(sectionEl, toggleEl, collapsed) {
    if (!sectionEl || !toggleEl) {
      return;
    }

    if (collapsed) {
      sectionEl.classList.add("is-collapsed");
      toggleEl.setAttribute("aria-expanded", "false");
    } else {
      sectionEl.classList.remove("is-collapsed");
      toggleEl.setAttribute("aria-expanded", "true");
    }
  }

  function toggleSection(sectionEl, toggleEl) {
    if (!sectionEl || !toggleEl) {
      return;
    }
    setSectionCollapsed(
      sectionEl,
      toggleEl,
      !sectionEl.classList.contains("is-collapsed"),
    );
  }

  function setSettingsSectionCollapsed(collapsed) {
    setSectionCollapsed(settingsSection, settingsToggle, collapsed);
  }

  function toggleSettingsSection() {
    toggleSection(settingsSection, settingsToggle);
  }

  function setLatestProjectsSectionCollapsed(collapsed) {
    setSectionCollapsed(latestProjectsSection, latestProjectsToggle, collapsed);
  }

  function toggleLatestProjectsSection() {
    toggleSection(latestProjectsSection, latestProjectsToggle);
  }

  function isDriveConfigured() {
    return !!(
      settings.client_id &&
      settings.client_secret &&
      settings.refresh_token &&
      settings.parent_folder_id
    );
  }

  function buildDrivePayloadBase() {
    return {
      settings: {
        client_id: settings.client_id,
        client_secret: settings.client_secret,
        refresh_token: settings.refresh_token,
        parent_folder_id: settings.parent_folder_id,
      },
      app_data_path: APPDATA,
    };
  }

  // --- Host eval ---

  function evalHost(script) {
    return new Promise(function (resolve) {
      cs.evalScript(script, function (result) {
        resolve(result || "");
      });
    });
  }

  function runScript(jsxPath) {
    var normalized = String(jsxPath || "").replace(/\\/g, "/");
    var runStartedAt = Date.now();
    var subtitleExpandStartedAt = runStartedAt;
    var subtitleExpandElapsedMs = 0;
    var hostStartedAt = 0;

    if (!fs.existsSync(jsxPath)) {
      return Promise.reject(new Error("Script not found: " + jsxPath));
    }

    setStatus("running");
    log("Running: " + path.basename(jsxPath), "info");

    return ensureProjectSubtitlesExpanded(path.dirname(jsxPath))
      .then(function (preparation) {
        subtitleExpandElapsedMs = Math.max(
          0,
          Date.now() - subtitleExpandStartedAt,
        );
        if (preparation && preparation.extracted) {
          log(
            "Expanded subtitle archive before JSX run (" +
              Number(preparation.extractedFileCount || 0) +
              " files)",
            "info",
          );
        }
        hostStartedAt = Date.now();
        return evalHost('runScript("' + escapeForEval(normalized) + '")');
      })
      .then(function (result) {
        var hostImportElapsedMs = hostStartedAt
          ? Math.max(0, Date.now() - hostStartedAt)
          : 0;
        if (result && result.indexOf("ERROR:") === 0) {
          setStatus("error");
          throw new Error(result);
        }
        log("Completed: " + path.basename(jsxPath), "success");
        updateGlobalStatus();
        return {
          host_import_elapsed_ms: hostImportElapsedMs,
          host_result: result,
          subtitle_expand_elapsed_ms: subtitleExpandElapsedMs,
          total_elapsed_ms: Math.max(0, Date.now() - runStartedAt),
        };
      })
      .catch(function (err) {
        setStatus("error");
        throw err;
      });
  }

  function cleanupImportedProjectInHost(
    projectId,
    localRootPath,
    suppressLogs,
  ) {
    var quiet = !!suppressLogs;
    var cleanupRoots = [];
    if (Array.isArray(localRootPath)) {
      cleanupRoots = localRootPath
        .map(function (rootPath) {
          return String(rootPath || "").trim();
        })
        .filter(Boolean);
    } else if (String(localRootPath || "").trim()) {
      cleanupRoots = [String(localRootPath || "").trim()];
    }
    var hostCall =
      'cleanupImportedProjectsForLocalRoots("' +
      escapeForEval(JSON.stringify(cleanupRoots)) +
      '")';

    return evalHost(hostCall).then(function (result) {
      var raw = String(result || "").trim();
      if (!raw) {
        throw new Error("Host cleanup returned an empty response");
      }
      if (raw.indexOf("ERROR:") === 0) {
        throw new Error(raw);
      }

      var parsed = null;
      try {
        parsed = JSON.parse(raw);
      } catch (eJson) {
        parsed = null;
      }

      var summary = parsed || { ok: true, raw: raw };
      if (!quiet && summary) {
        var sequencesDeleted = Number(summary.sequences_deleted || 0);
        var movedItems = Number(summary.items_moved_to_purge_bin || 0);
        var remainingSequences = Number(summary.remaining_sequences || 0);
        var remainingRootItems = Number(summary.remaining_root_items || 0);
        var warningCount = Array.isArray(summary.warnings)
          ? summary.warnings.length
          : Number(summary.warning_count || 0);
        var detachedProxyCount = Number(summary.detached_proxy_count || 0);
        var mediaOfflineCount = Number(summary.media_offline_count || 0);
        log(
          "Premiere cleanup for " +
            projectId +
            ": sequencesDeleted=" +
            sequencesDeleted +
            ", movedToPurgeBin=" +
            movedItems +
            ", remainingSequences=" +
            remainingSequences +
            ", remainingRootItems=" +
            remainingRootItems +
            ", detachedProxies=" +
            detachedProxyCount +
            ", mediaOffline=" +
            mediaOfflineCount +
            ", warnings=" +
            warningCount,
          "info",
        );
      }
      return summary;
    });
  }

  // --- Persistent project states ---

  function projectStatePath(projectId) {
    return path.join(PROJECTS_STATE_DIR, projectId + ".json");
  }

  function loadProjectStates() {
    ensureDir(PROJECTS_STATE_DIR);
    var map = {};
    var files = [];
    try {
      files = fs.readdirSync(PROJECTS_STATE_DIR);
    } catch (e) {
      files = [];
    }

    files.forEach(function (fileName) {
      if (!/\.json$/i.test(fileName)) {
        return;
      }
      var fullPath = path.join(PROJECTS_STATE_DIR, fileName);
      var payload = readJson(fullPath, null);
      if (!payload || !payload.project_id) {
        return;
      }
      map[payload.project_id] = payload;
    });

    return map;
  }

  function loadNormalizedProjectStates() {
    var normalization = getRuntimeStateHelper().normalizeLoadedProjectStates(
      loadProjectStates(),
      nowIso(),
    );

    if (normalization.changed_count > 0) {
      normalization.changed_project_ids.forEach(function (projectId) {
        writeJsonAtomic(
          projectStatePath(projectId),
          normalization.states[projectId],
        );
      });
      log(
        "Startup normalized " +
          normalization.changed_count +
          " project state(s); automatic recovery remains disabled",
        "info",
      );
    }

    return normalization.states;
  }

  function getProjectState(projectId) {
    return projectStates[projectId] || null;
  }

  function upsertProjectState(projectId, patch) {
    var normalizedProjectId = String(projectId || "").trim();
    var previous = projectStates[normalizedProjectId] || {
      project_id: normalizedProjectId,
      created_at: nowIso(),
    };
    var hasMetricsPatch =
      !!patch &&
      Object.prototype.hasOwnProperty.call(patch, "orchestration_metrics");

    var merged = {};
    Object.keys(previous).forEach(function (key) {
      merged[key] = previous[key];
    });
    Object.keys(patch || {}).forEach(function (key) {
      merged[key] = patch[key];
    });
    if (hasMetricsPatch) {
      merged.orchestration_metrics = getOrchestrationMetricsHelper().mergeMetrics(
        previous.orchestration_metrics,
        patch.orchestration_metrics,
      );
    } else if (previous.orchestration_metrics) {
      merged.orchestration_metrics = getOrchestrationMetricsHelper().normalizeMetrics(
        previous.orchestration_metrics,
      );
    }

    merged.project_id = normalizedProjectId;
    merged.panel_build_id = PANEL_BUILD_ID;
    merged.sequence_name =
      String(merged.sequence_name || "").trim() ||
      buildProjectSequenceName(normalizedProjectId);
    merged.batch_phase = getBatchPhase();
    merged.is_sleeping = getBatchRuntimeHelper().isProjectSleeping(
      ensureBatchRuntime(),
      normalizedProjectId,
    );
    merged.updated_at = nowIso();
    projectStates[normalizedProjectId] = merged;
    writeJsonAtomic(projectStatePath(normalizedProjectId), merged);
    renderProjectSelect();
    renderProjectStates();
    return merged;
  }

  function removeProjectState(projectId) {
    clearCleanupRetry(projectId);
    delete projectStates[projectId];
    try {
      fs.unlinkSync(projectStatePath(projectId));
    } catch (e) {
      // ignore
    }
    renderProjectSelect();
    renderProjectStates();
  }

  function resetProjectState(projectId) {
    var id = String(projectId || "").trim();
    var state = projectStates[id];
    if (!id || !state) {
      return;
    }

    if (
      jobStore.active &&
      jobStore.active.payload &&
      jobStore.active.payload.project_id === id
    ) {
      log("Cannot reset " + id + " while a job is active", "warn");
      return;
    }

    var removedJobs = 0;
    jobStore.queue = jobStore.queue.filter(function (job) {
      var sameProject = job && job.payload && job.payload.project_id === id;
      if (sameProject) {
        removedJobs += 1;
        return false;
      }
      return true;
    });
    if (removedJobs > 0) {
      persistJobs();
      renderQueue();
    }

    clearCleanupRetry(id);
    disarmExportMonitor(id);

    upsertProjectState(id, {
      status: state.output_path ? "ready_for_export" : "idle",
      last_error: null,
      cleanup_error: null,
      host_cleanup_error: null,
      host_cleanup_result: null,
      cleanup_retryable: false,
      cleanup_retry_count: 0,
      cleanup_next_retry_at: null,
      export_job_id: null,
      video_export_job_id: null,
      audio_export_job_id: null,
      encoder_progress: 0,
      upload_pending: false,
      expected_outputs: getExpectedOutputPaths(state),
      uploaded_outputs: {},
      upload_results_by_output: {},
      completion_notified_status: null,
      completion_notified_at: null,
      proxy_status: null,
      proxy_error: null,
      proxy_pending_count: 0,
      proxy_job_ids: [],
      proxy_summary: null,
    });

    log(
      "Project reset: " +
        id +
        (removedJobs > 0 ? " (removed " + removedJobs + " queued job(s))" : ""),
      "success",
    );
    updateGlobalStatus();
  }

  function clearCleanupRetry(projectId) {
    var id = String(projectId || "").trim();
    if (!id || !cleanupRetryTimers[id]) {
      return;
    }
    try {
      clearTimeout(cleanupRetryTimers[id]);
    } catch (e) {
      // ignore
    }
    delete cleanupRetryTimers[id];
  }

  function buildCleanupErrorDetail(cleanupResult) {
    if (!cleanupResult) {
      return "Unknown cleanup failure";
    }
    var message = formatCleanupError(cleanupResult.error);
    var remaining = Array.isArray(cleanupResult.remaining_entries)
      ? cleanupResult.remaining_entries
      : [];
    if (remaining.length > 0) {
      message += " | Remaining: " + remaining.join(", ");
    }
    return message;
  }

  function buildHostCleanupErrorDetail(hostSummary) {
    if (!hostSummary) {
      return "Premiere cleanup incomplete";
    }

    var parts = [];
    if (hostSummary.error) {
      parts.push(String(hostSummary.error));
    } else {
      parts.push("Premiere cleanup incomplete");
    }

    parts.push(
      "Remaining sequences: " + Number(hostSummary.remaining_sequences || 0),
    );
    parts.push(
      "Remaining root items: " +
        Number(hostSummary.remaining_root_items || 0),
    );
    parts.push(
      "Deleted sequences: " + Number(hostSummary.sequences_deleted || 0),
    );
    parts.push(
      "Moved to purge bin: " +
        Number(hostSummary.items_moved_to_purge_bin || 0),
    );
    var warningCount = Array.isArray(hostSummary.warnings)
      ? hostSummary.warnings.length
      : Number(hostSummary.warning_count || 0);
    if (warningCount > 0) {
      parts.push("Warnings: " + warningCount);
    }

    return parts.join(" | ");
  }

  function maybeNotifyProjectCompletion(state) {
    return state || null;
  }

  function scheduleCleanupRetry(projectId, delayMs) {
    var id = String(projectId || "").trim();
    if (!id) {
      return;
    }
    clearCleanupRetry(id);
    cleanupRetryTimers[id] = setTimeout(
      function () {
        delete cleanupRetryTimers[id];
        retryPendingCleanup(id, "scheduled");
      },
      Math.max(500, Number(delayMs) || CLEANUP_RETRY_DELAY_MS),
    );
  }

  function retryPendingCleanup(projectId, triggerSource) {
    var id = String(projectId || "").trim();
    var source = String(triggerSource || "retry");
    if (!id) {
      return Promise.resolve(false);
    }

    var state = getProjectState(id);
    if (!state || !state.local_root) {
      clearCleanupRetry(id);
      return Promise.resolve(false);
    }

    var status = String(state.status || "");
    var canRetry =
      status === "cleanup_pending" ||
      (status === "cleanup_failed" && !!state.cleanup_retryable);
    if (!canRetry) {
      clearCleanupRetry(id);
      return Promise.resolve(false);
    }

    var retryCount = Number(state.cleanup_retry_count || 0);
    if (
      retryCount >= CLEANUP_RETRYABLE_MAX_PASSES &&
      source !== "manual"
    ) {
      clearCleanupRetry(id);
      return Promise.resolve(false);
    }

    disarmExportMonitor(id);
    cancelLocalProxyRenderProcessesForExport();
    var isBatchProject = getBatchRuntimeHelper().isProjectInExportBatch(
      ensureBatchRuntime(),
      id,
    );

    return cleanupImportedProjectInHost(id, state.local_root, true)
      .then(function (hostSummary) {
        upsertProjectState(id, {
          host_cleanup_result: hostSummary || null,
          host_cleanup_error: null,
        });
        if (hostSummary && hostSummary.ok === false) {
          var hostDetail = buildHostCleanupErrorDetail(hostSummary);
          var nextRetryCount = retryCount + 1;
          var canRetryHost = nextRetryCount < CLEANUP_RETRYABLE_MAX_PASSES;
          if (canRetryHost) {
            var nextRetryAt = new Date(
              Date.now() + CLEANUP_RETRY_DELAY_MS,
            ).toISOString();
            upsertProjectState(id, {
              status: "cleanup_pending",
              cleanup_error: hostDetail,
              cleanup_retryable: true,
              cleanup_retry_count: nextRetryCount,
              cleanup_next_retry_at: nextRetryAt,
            });
            log(
              "Premiere cleanup incomplete for " +
                id +
                " (attempt " +
                nextRetryCount +
                "/" +
                CLEANUP_RETRYABLE_MAX_PASSES +
                "), retrying in " +
                Math.round(CLEANUP_RETRY_DELAY_MS / 1000) +
                "s",
              "warn",
            );
            scheduleCleanupRetry(id, CLEANUP_RETRY_DELAY_MS);
            return false;
          }

          clearCleanupRetry(id);
          upsertProjectState(id, {
            status: "cleanup_failed",
            cleanup_error: hostDetail,
            cleanup_retryable: false,
            cleanup_retry_count: nextRetryCount,
            cleanup_next_retry_at: null,
          });
          log("Premiere cleanup failed for " + id + ": " + hostDetail, "warn");
          if (isBatchProject) {
            handleBatchFailure(id, "Premiere cleanup failed: " + hostDetail);
          }
          return false;
        }
        return removePathSafe(state.local_root, {
          maxAttempts: CLEANUP_BACKGROUND_MAX_ATTEMPTS,
        });
      })
      .catch(function (hostErr) {
        clearCleanupRetry(id);
        upsertProjectState(id, {
          host_cleanup_error: hostErr.message,
          status: "cleanup_failed",
          cleanup_error: hostErr.message,
          cleanup_retryable: false,
          cleanup_next_retry_at: null,
        });
        log(
          "Premiere cleanup warning for " +
            id +
            " during retry: " +
            hostErr.message,
          "warn",
        );
        if (isBatchProject) {
          handleBatchFailure(id, "Premiere cleanup crashed: " + hostErr.message);
        }
        return false;
      })
      .then(function (cleanupResult) {
        if (cleanupResult === false) {
          return false;
        }
        if (cleanupResult.ok) {
          clearCleanupRetry(id);
          var cleanedState = upsertProjectState(id, {
            status: "uploaded_cleaned",
            cleanup_deleted: true,
            cleanup_error: null,
            cleanup_retryable: false,
            cleanup_retry_count: 0,
            cleanup_next_retry_at: null,
          });
          log(
            "Cleanup succeeded for " + id + " after retry (" + source + ")",
            "success",
          );
          maybeNotifyProjectCompletion(cleanedState);
          maybeFinalizeBatchCleanup();
          return true;
        }

        var detail = buildCleanupErrorDetail(cleanupResult);
        var nextRetryCount = retryCount + 1;
        var retryableLock = !!cleanupResult.retryable_lock;
        var retryable =
          retryableLock &&
          nextRetryCount < CLEANUP_RETRYABLE_MAX_PASSES;
        if (retryable) {
          var nextRetryAt = new Date(
            Date.now() + CLEANUP_RETRY_DELAY_MS,
          ).toISOString();
          upsertProjectState(id, {
            status: "cleanup_pending",
            cleanup_error: detail,
            cleanup_retryable: true,
            cleanup_retry_count: nextRetryCount,
            cleanup_next_retry_at: nextRetryAt,
          });
          log(
            "Cleanup still locked for " +
              id +
              " (attempt " +
              nextRetryCount +
              "/" +
              CLEANUP_RETRYABLE_MAX_PASSES +
              "), retrying in " +
              Math.round(CLEANUP_RETRY_DELAY_MS / 1000) +
              "s",
            "warn",
          );
          scheduleCleanupRetry(id, CLEANUP_RETRY_DELAY_MS);
          return false;
        }

        clearCleanupRetry(id);
        upsertProjectState(id, {
          status: "cleanup_failed",
          cleanup_error: detail,
          cleanup_retryable: retryableLock,
          cleanup_retry_count: nextRetryCount,
          cleanup_next_retry_at: null,
        });
        log("Cleanup failed for " + id + ": " + detail, "warn");
        if (!retryableLock && isBatchProject) {
          handleBatchFailure(id, "Cleanup failed: " + detail);
        }
        return false;
      })
      .catch(function (err) {
        clearCleanupRetry(id);
        upsertProjectState(id, {
          status: "cleanup_failed",
          cleanup_error: formatCleanupError(err),
          cleanup_retryable: false,
          cleanup_next_retry_at: null,
        });
        if (isBatchProject) {
          handleBatchFailure(id, "Cleanup crashed: " + err.message);
        }
        log("Cleanup retry crashed for " + id + ": " + err.message, "error");
        return false;
      });
  }

  // --- Queue runtime ---

  function loadJobs() {
    return getRuntimeStateHelper().createEmptyJobStore();
  }

  function persistJobs() {
    return;
  }

  function setBatchPhase(nextPhase) {
    ensureBatchRuntime().phase = String(nextPhase || "").trim();
  }

  function syncProjectBatchMetadata(projectIds) {
    var seen = {};
    (projectIds || []).forEach(function (projectId) {
      var id = String(projectId || "").trim();
      if (!id || seen[id] || !projectStates[id]) {
        return;
      }
      seen[id] = true;
      upsertProjectState(id, {});
    });
  }

  function syncTrackedBatchProjectMetadata() {
    syncProjectBatchMetadata(
      getTrackedBatchProjectIds().concat(listSleepingProjectIds()),
    );
  }

  function generateJobId(type) {
    return type + "_" + Date.now() + "_" + Math.floor(Math.random() * 100000);
  }

  function isJobQueued(type, projectId) {
    var active = jobStore.active;
    if (
      active &&
      active.type === type &&
      active.payload &&
      active.payload.project_id === projectId
    ) {
      return true;
    }
    return jobStore.queue.some(function (job) {
      return (
        job.type === type && job.payload && job.payload.project_id === projectId
      );
    });
  }

  function isUploadJobQueuedForOutput(projectId, outputPath) {
    var normalizedProject = String(projectId || "").trim();
    var normalizedOutput = String(outputPath || "").trim();
    if (!normalizedProject || !normalizedOutput) {
      return false;
    }

    var active = jobStore.active;
    if (
      active &&
      active.type === "upload_output" &&
      active.payload &&
      active.payload.project_id === normalizedProject &&
      String(active.payload.output_path || "").trim() === normalizedOutput
    ) {
      return true;
    }

    return jobStore.queue.some(function (job) {
      return (
        job &&
        job.type === "upload_output" &&
        job.payload &&
        job.payload.project_id === normalizedProject &&
        String(job.payload.output_path || "").trim() === normalizedOutput
      );
    });
  }

  function enqueueJob(type, payload) {
    var job = {
      id: generateJobId(type),
      type: type,
      payload: payload || {},
      status: "pending",
      created_at: nowIso(),
      updated_at: nowIso(),
    };
    jobStore.queue.push(job);
    persistJobs();
    renderQueue();
    processJobQueue();
    return job;
  }

  function renderQueue() {
    clearChildren(queueList);

    var rows = [];
    if (jobStore.active) {
      rows.push({ job: jobStore.active, active: true });
    }
    jobStore.queue.forEach(function (job) {
      rows.push({ job: job, active: false });
    });
    listSleepingProjectIds().forEach(function (projectId) {
      rows.push({
        job: {
          type: "sleeping_queue",
          payload: {
            project_id: projectId,
          },
        },
        active: false,
      });
    });

    if (rows.length === 0) {
      var empty = document.createElement("li");
      empty.className = "empty-msg";
      empty.textContent = "No jobs queued";
      queueList.appendChild(empty);
      return;
    }

    rows.forEach(function (row) {
      var li = document.createElement("li");
      li.className = row.active ? "queue-active" : "queue-pending";
      var projectPart =
        row.job.payload && row.job.payload.project_id
          ? row.job.payload.project_id
          : "-";
      li.textContent =
        (row.active ? "[ACTIVE] " : "[PENDING] ") +
        row.job.type +
        " :: " +
        projectPart;
      queueList.appendChild(li);
    });
  }

  function dropQueuedJobsForOtherProjects(activeProjectId) {
    var kept = [];
    var removed = 0;

    jobStore.queue.forEach(function (job) {
      var queuedProjectId = String(
        (job && job.payload && job.payload.project_id) || "",
      ).trim();
      if (queuedProjectId && queuedProjectId !== activeProjectId) {
        removed += 1;
        return;
      }
      kept.push(job);
    });

    jobStore.queue = kept;
    return removed;
  }

  function dropEncoderJobsForOtherProjects(activeProjectId) {
    var removed = 0;
    Object.keys(encoderJobMap).forEach(function (jobId) {
      var mapped = encoderJobMap[jobId];
      var mappedProjectId = String(
        mapped && mapped.project_id ? mapped.project_id : mapped || "",
      ).trim();
      if (!mappedProjectId || mappedProjectId === activeProjectId) {
        return;
      }
      delete encoderJobMap[jobId];
      removed += 1;
    });
    return removed;
  }

  function dropEncoderJobsForProject(projectId) {
    var normalizedProjectId = String(projectId || "").trim();
    var removed = 0;
    Object.keys(encoderJobMap).forEach(function (jobId) {
      var mapped = encoderJobMap[jobId];
      var mappedProjectId = String(
        mapped && mapped.project_id ? mapped.project_id : mapped || "",
      ).trim();
      if (!mappedProjectId || mappedProjectId !== normalizedProjectId) {
        return;
      }
      delete encoderJobMap[jobId];
      removed += 1;
    });
    return removed;
  }

  function deactivateAutomationForOtherProjects(activeProjectId) {
    var disarmedMonitors = 0;
    var clearedRetries = 0;

    Object.keys(exportMonitors).forEach(function (projectId) {
      if (projectId === activeProjectId) {
        return;
      }
      disarmExportMonitor(projectId);
      disarmedMonitors += 1;
    });

    Object.keys(cleanupRetryTimers).forEach(function (projectId) {
      if (projectId === activeProjectId) {
        return;
      }
      clearCleanupRetry(projectId);
      clearedRetries += 1;
    });

    return {
      disarmed_monitors: disarmedMonitors,
      cleared_retries: clearedRetries,
    };
  }

  function takeHardPrecedence(projectId, reason) {
    var nextProjectId = String(projectId || "").trim();
    if (!nextProjectId) {
      throw new Error("Missing project id for precedence switch");
    }
    return captureAutomationLease(nextProjectId);
  }

  function processJobQueue() {
    if (jobStore.active || jobStore.queue.length === 0) {
      updateGlobalStatus();
      return;
    }

    var job = jobStore.queue.shift();
    job.status = "running";
    job.updated_at = nowIso();
    job.controller = createActiveJobController(job);
    jobStore.active = job;
    persistJobs();
    renderQueue();
    setStatus("running");

    executeJob(job, job.controller)
      .then(function () {
        if (isJobControllerCanceled(job.controller)) {
          return;
        }
        log(
          "Job completed: " +
            job.type +
            " (" +
            (job.payload.project_id || "-") +
            ")",
          "success",
        );
      })
      .catch(function (err) {
        if (
          isJobControllerCanceled(job.controller) ||
          isAutomationCanceledError(err)
        ) {
          return;
        }
        log("Job failed: " + job.type + " -> " + err.message, "error");
      })
      .finally(function () {
        if (jobStore.active && jobStore.active.id === job.id) {
          jobStore.active = null;
        }
        delete job.controller;
        persistJobs();
        renderQueue();
        if (job.type === "upload_output") {
          maybeAdvanceBatchAfterUpload();
        }
        processJobQueue();
      });
  }

  function executeJob(job, controller) {
    if (job.type === "download_import") {
      return executeDownloadImport(job.payload.project_id, controller);
    }
    if (job.type === "upload_output") {
      var outputPath =
        job.payload && job.payload.output_path
          ? String(job.payload.output_path)
          : "";
      return executeUploadOutput(
        job.payload.project_id,
        !!job.payload.cleanup_after_upload,
        String(job.payload.reason || "watch"),
        outputPath,
        controller,
      );
    }
    return Promise.reject(new Error("Unknown job type: " + job.type));
  }

  // --- Worker runner ---

  function runDriveTaskFallback(taskName, payload, onProgress) {
    if (!driveTasksFallback) {
      driveTasksFallback = require(getClientFilePath("drive_tasks.js"));
    }
    return driveTasksFallback.runTask(taskName, payload, onProgress);
  }

  function runDriveTask(taskName, payload, onProgress, options) {
    return new Promise(function (resolve, reject) {
      var controller = options && options.controller ? options.controller : null;
      var taskProjectId = String(
        (options && options.projectId) ||
          (payload && payload.project_id) ||
          (controller && controller.project_id) ||
          "",
      ).trim();
      var workerPath = getClientFilePath("drive_worker.js");
      var child;
      var exitingWithFallback = false;

      function buildCanceledError() {
        return createAutomationCanceledError(
          taskProjectId,
          (controller && controller.cancel_reason) || taskName,
        );
      }

      if (isJobControllerCanceled(controller)) {
        reject(buildCanceledError());
        return;
      }

      try {
        child = childProcess.fork(workerPath, [], {
          stdio: ["ignore", "ignore", "ignore", "ipc"],
        });
      } catch (forkErr) {
        if (isJobControllerCanceled(controller)) {
          reject(buildCanceledError());
          return;
        }
        log("Worker unavailable, using in-process fallback", "warn");
        runDriveTaskFallback(taskName, payload, onProgress)
          .then(function (result) {
            if (isJobControllerCanceled(controller)) {
              reject(buildCanceledError());
              return;
            }
            resolve(result);
          })
          .catch(function (err) {
            if (isJobControllerCanceled(controller)) {
              reject(buildCanceledError());
              return;
            }
            reject(err);
          });
        return;
      }

      if (controller) {
        controller.child = child;
      }
      var settled = false;

      function completeWithError(err) {
        if (settled) {
          return;
        }
        settled = true;
        try {
          child.kill();
        } catch (e) {}
        if (controller && controller.child === child) {
          controller.child = null;
        }
        reject(err);
      }

      child.on("message", function (msg) {
        if (!msg || settled) {
          return;
        }
        if (msg.type === "progress") {
          if (isJobControllerCanceled(controller)) {
            return;
          }
          if (typeof onProgress === "function") {
            onProgress(msg.progress || {});
          }
          return;
        }
        if (msg.type === "result") {
          if (isJobControllerCanceled(controller)) {
            completeWithError(buildCanceledError());
            return;
          }
          settled = true;
          resolve(msg.result);
          try {
            child.kill();
          } catch (eKill) {}
          if (controller && controller.child === child) {
            controller.child = null;
          }
          return;
        }
        if (msg.type === "error") {
          if (isJobControllerCanceled(controller)) {
            completeWithError(buildCanceledError());
            return;
          }
          completeWithError(
            new Error((msg.error && msg.error.message) || "Worker error"),
          );
        }
      });

      child.on("error", function (err) {
        completeWithError(err);
      });

      child.on("exit", function (code) {
        if (settled) {
          return;
        }
        if (controller && controller.child === child) {
          controller.child = null;
        }
        if (isJobControllerCanceled(controller)) {
          completeWithError(buildCanceledError());
          return;
        }
        if (code === 0) {
          completeWithError(new Error("Worker exited before sending result"));
          return;
        }

        if (exitingWithFallback) {
          completeWithError(new Error("Worker exited with code " + code));
          return;
        }

        exitingWithFallback = true;
        log(
          "Worker exited with code " + code + ", retrying in-process fallback",
          "warn",
        );
        runDriveTaskFallback(taskName, payload, onProgress)
          .then(function (result) {
            if (isJobControllerCanceled(controller)) {
              reject(buildCanceledError());
              return;
            }
            resolve(result);
          })
          .catch(function (err) {
            if (isJobControllerCanceled(controller)) {
              reject(buildCanceledError());
              return;
            }
            reject(err);
          });
      });

      child.send({
        type: "run",
        task: taskName,
        payload: payload,
      });
    });
  }

  // --- Drive automation jobs ---

  function executeDownloadImport(projectId, controller) {
    if (!validateProjectId(projectId)) {
      return Promise.reject(new Error("Invalid project ID: " + projectId));
    }
    if (!isDriveConfigured()) {
      return Promise.reject(new Error("Drive settings are incomplete"));
    }

    var lease = captureAutomationLease(projectId);
    ensureAutomationLeaseActive(lease, "download_import_start");

    upsertProjectState(projectId, {
      status: "downloading",
      last_error: null,
      cleanup_deleted: false,
      cleanup_error: null,
      host_cleanup_error: null,
      host_cleanup_result: null,
      cleanup_retryable: false,
      cleanup_retry_count: 0,
      cleanup_next_retry_at: null,
      completion_notified_status: null,
      completion_notified_at: null,
      proxy_status: null,
      proxy_error: null,
      proxy_pending_count: 0,
      proxy_job_ids: [],
      proxy_summary: null,
    });

    var downloadPayload = buildDrivePayloadBase();
    downloadPayload.project_id = projectId;

    return runDriveTask(
      "downloadProject",
      downloadPayload,
      function (progress) {
        if (!isAutomationLeaseActive(lease)) {
          return;
        }
        if (progress.stage === "download_tuning") {
          log(
            "Download tuning for " +
              projectId +
              ": concurrency=" +
              Number(progress.selected_concurrency || 0) +
              " (" +
              Number(progress.file_count || 0) +
              " files)",
            "info",
          );
        } else if (progress.stage === "download_start") {
          log(
            "Download started for " +
              projectId +
              " (" +
              progress.file_count +
              " files)",
            "info",
          );
        } else if (progress.stage === "download_progress_summary") {
          log(
            String(progress.message || "Download progress for " + projectId),
            "info",
          );
        } else if (progress.stage === "subtitle_archive_extract_start") {
          log("Extracting subtitle archive for " + projectId, "info");
        } else if (progress.stage === "subtitle_archive_extract_complete") {
          log(
            "Subtitle archive extracted for " +
              projectId +
              " (" +
              Number(progress.extracted_file_count || 0) +
              " files)",
            "info",
          );
        } else if (progress.stage === "download_complete") {
          var elapsedSec = Math.max(
            0.001,
            Number(progress.elapsed_ms || 0) / 1000,
          );
          var totalMb = Number(progress.total_bytes || 0) / (1024 * 1024);
          var speed = totalMb / elapsedSec;
          log(
            "Download completed for " +
              projectId +
              " (" +
              totalMb.toFixed(1) +
              " MB in " +
              elapsedSec.toFixed(1) +
              "s, " +
              speed.toFixed(2) +
              " MB/s)",
            "success",
          );
        }
      },
      {
        controller: controller,
        projectId: projectId,
      },
    )
      .then(function (result) {
        ensureAutomationLeaseActive(lease, "download_import_complete");
        var importPath = path.join(result.local_root, "import_project.jsx");
        var proxyPlan = buildProxyPlanForLocalProject(
          result.local_root,
          projectId,
        );
        var proxySummary = summarizeProxyPlan(proxyPlan);
        if (!fs.existsSync(importPath)) {
          throw new Error(
            "import_project.jsx not found in downloaded folder: " +
              result.local_root,
          );
        }

        upsertProjectState(projectId, {
          status: "importing",
          drive_folder_id: result.drive_folder_id,
          drive_folder_name: result.drive_folder_name,
          local_root: result.local_root,
          output_path: result.output_path,
          used_fallback_root: !!result.used_fallback_root,
          download_elapsed_ms: result.download_elapsed_ms || null,
          download_avg_mb_per_sec: result.download_avg_mb_per_sec || null,
          download_file_count: result.download_file_count || null,
          download_total_bytes: result.download_total_bytes || null,
          last_error: null,
          orchestration_metrics: result.orchestration_metrics || {},
          proxy_status:
            proxyPlan.enabled && proxySummary.proxy_needed_count > 0
              ? "starting"
              : null,
          proxy_error: null,
          proxy_pending_count: 0,
          proxy_job_ids: [],
          proxy_summary: proxySummary,
        });

        persistProxyPlanToContext(result.local_root, proxyPlan);
        try {
          var importPatch = patchImportProjectForAsyncProxies(importPath);
          if (importPatch.patched) {
            log(
              "Patched import_project.jsx for asynchronous proxy handling",
              "info",
            );
          } else if (
            proxyPlan.enabled &&
            proxySummary.proxy_needed_count > 0 &&
            importPatch.reason !== "already_patched"
          ) {
            log(
              "Could not patch import_project.jsx proxy routing: " +
                importPatch.reason,
              "warn",
            );
          }
        } catch (importPatchErr) {
          log(
            "Could not patch import_project.jsx proxy routing: " +
              importPatchErr.message,
            "warn",
          );
        }

        if (proxyPlan.enabled) {
          if (proxyPlan.ffprobe_warning) {
            log(
              "Proxy planning warning for " +
                projectId +
                ": " +
                proxyPlan.ffprobe_warning,
              "warn",
            );
          }
          log(
            "Proxy plan for " +
              projectId +
              ": " +
              Number(proxySummary.proxy_needed_count || 0) +
              "/" +
              Number(proxySummary.total_video_targets || 0) +
              " video file(s) need proxies",
            "info",
          );
        }

        if (proxyPlan.enabled && proxySummary.proxy_needed_count > 0) {
          scheduleProxyRenderingSidecar(
            projectId,
            result.local_root,
            proxyPlan,
            lease,
          )
            .then(function (scheduleResult) {
              if (!isAutomationProjectActive(projectId)) {
                return;
              }
              if (
                String((getProjectState(projectId) || {}).proxy_status || "").trim() ===
                "canceled"
              ) {
                return;
              }
              log(
                "Proxy rendering scheduled asynchronously for " + projectId,
                "info",
              );
              upsertProjectState(projectId, {
                proxy_status: scheduleResult && scheduleResult.scheduled
                  ? "starting"
                  : "proxying",
                proxy_error: null,
                proxy_pending_count:
                  scheduleResult && Array.isArray(scheduleResult.job_ids)
                    ? scheduleResult.job_ids.length
                    : 0,
                proxy_job_ids:
                  scheduleResult && Array.isArray(scheduleResult.job_ids)
                    ? scheduleResult.job_ids
                    : [],
                proxy_last_run_at: nowIso(),
              });
            })
            .catch(function (proxyErr) {
              log(
                "Automatic proxy scheduling failed for " +
                  projectId +
                  ": " +
                  proxyErr.message,
                "warn",
              );
              if (
                String((getProjectState(projectId) || {}).proxy_status || "").trim() ===
                "canceled"
              ) {
                return;
              }
              upsertProjectState(projectId, {
                proxy_status: "warning",
                proxy_error: proxyErr.message,
                proxy_last_run_at: nowIso(),
              });
            });
        }

        return runScript(importPath).then(function (runOutcome) {
          ensureAutomationLeaseActive(lease, "download_import_after_jsx");
          var readyAtIso = nowIso();
          var audioOutputPath = path.join(
            path.dirname(String(result.output_path || "")),
            AUDIO_NO_MUSIC_OUTPUT_FILENAME,
          );
          var expectedOutputs = normalizeOutputPathList(
            [
              String(result.output_path || ""),
              String(audioOutputPath || ""),
            ],
          );
          var orchestrationMetrics =
            getOrchestrationMetricsHelper().finalizeReadyMetrics(
              (
                (getProjectState(projectId) || {}).orchestration_metrics ||
                result.orchestration_metrics
              ),
              readyAtIso,
              runOutcome && runOutcome.host_import_elapsed_ms,
            );
          var previousProxyState = getProjectState(projectId) || {};
          var nextProxyStatus = null;
          if (proxyPlan.enabled && proxySummary.proxy_needed_count > 0) {
            nextProxyStatus = String(previousProxyState.proxy_status || "").trim();
            if (
              nextProxyStatus !== "proxying" &&
              nextProxyStatus !== "warning" &&
              nextProxyStatus !== "ready"
            ) {
              nextProxyStatus = "starting";
            }
          }
          var nextState = upsertProjectState(projectId, {
            status: "ready_for_export",
            imported_at: readyAtIso,
            upload_pending: false,
            pending_cleanup_choice: false,
            audio_export_enabled: true,
            audio_output_path: audioOutputPath,
            expected_outputs: expectedOutputs,
            uploaded_outputs: {},
            upload_results_by_output: {},
            orchestration_metrics: orchestrationMetrics,
            proxy_status: nextProxyStatus,
            proxy_error:
              proxyPlan.enabled && proxySummary.proxy_needed_count > 0
                ? previousProxyState.proxy_error || null
                : null,
            proxy_pending_count:
              proxyPlan.enabled && proxySummary.proxy_needed_count > 0
                ? Math.max(0, Number(previousProxyState.proxy_pending_count || 0))
                : 0,
            proxy_job_ids:
              proxyPlan.enabled && proxySummary.proxy_needed_count > 0
                ? Array.isArray(previousProxyState.proxy_job_ids)
                  ? previousProxyState.proxy_job_ids.slice(0)
                  : []
                : [],
            proxy_summary:
              proxyPlan.enabled && proxySummary.proxy_needed_count > 0
                ? previousProxyState.proxy_summary || proxySummary
                : proxySummary,
            proxy_last_run_at:
              proxyPlan.enabled && proxySummary.proxy_needed_count > 0
                ? previousProxyState.proxy_last_run_at || readyAtIso
                : null,
          });
          ensureAutomationLeaseActive(lease, "download_import_ready");
          projectSelect.value = projectId;

          return nextState;
        });
      })
      .catch(function (err) {
        if (isAutomationCanceledError(err)) {
          throw err;
        }
        if (!isAutomationLeaseActive(lease)) {
          throw createAutomationCanceledError(
            projectId,
            "download_import_superseded",
          );
        }
        upsertProjectState(projectId, {
          status: "error",
          last_error: err.message,
        });
        throw err;
      });
  }

  function executeUploadOutput(
    projectId,
    cleanupAfterUpload,
    reason,
    outputPathOverride,
    controller,
  ) {
    var state = getProjectState(projectId);
    var selectedOutputPath = String(
      outputPathOverride || (state && state.output_path) || "",
    ).trim();
    var selectedOutputName = path.basename(selectedOutputPath || "");
    var safeOutputNameKey = selectedOutputName
      ? selectedOutputName.replace(/[^a-zA-Z0-9._-]/g, "_")
      : "output";
    if (!state) {
      return Promise.reject(new Error("Unknown project state: " + projectId));
    }
    if (!state.drive_folder_id) {
      return Promise.reject(
        new Error("Project has no resolved Drive folder id"),
      );
    }
    if (!selectedOutputPath || !fs.existsSync(selectedOutputPath)) {
      return Promise.reject(
        new Error(
          "Missing output file for project " +
            projectId +
            ": " +
            selectedOutputPath,
        ),
      );
    }
    if (!isDriveConfigured()) {
      return Promise.reject(new Error("Drive settings are incomplete"));
    }

    var lease = captureAutomationLease(projectId);
    ensureAutomationLeaseActive(lease, "upload_output_start");

    var expectedOutputs = getExpectedOutputPaths(state);
    if (expectedOutputs.length <= 0 && selectedOutputPath) {
      expectedOutputs = [selectedOutputPath];
    }

    upsertProjectState(projectId, {
      status: "uploading",
      upload_pending: false,
      pending_cleanup_choice: false,
      upload_reason: reason,
      output_path: selectedOutputPath,
      expected_outputs: expectedOutputs,
      last_error: null,
      cleanup_deleted: false,
      cleanup_error: null,
      host_cleanup_error: null,
      host_cleanup_result: null,
      cleanup_retryable: false,
      cleanup_retry_count: 0,
      cleanup_next_retry_at: null,
    });

    var uploadPayload = buildDrivePayloadBase();
    uploadPayload.project_id = projectId;
    uploadPayload.drive_folder_id = state.drive_folder_id;
    uploadPayload.output_path = selectedOutputPath;
    uploadPayload.output_file_name = selectedOutputName;
    uploadPayload.session_state_path = path.join(
      UPLOAD_SESSIONS_DIR,
      projectId + "__" + safeOutputNameKey + ".json",
    );

    var lastProgressPct = -1;

    return runDriveTask(
      "uploadOutput",
      uploadPayload,
      function (progress) {
        if (!isAutomationLeaseActive(lease)) {
          return;
        }
        if (progress.stage === "upload_progress") {
          var pct = Math.round(
            (Number(progress.uploaded_bytes || 0) /
              Math.max(1, Number(progress.total_bytes || 1))) *
              100,
          );
          if (pct !== lastProgressPct && (pct % 5 === 0 || pct === 100)) {
            lastProgressPct = pct;
            log("Upload " + projectId + ": " + pct + "%", "info");
          }
        }
      },
      {
        controller: controller,
        projectId: projectId,
      },
    )
      .then(function (result) {
        ensureAutomationLeaseActive(lease, "upload_output_complete");
        var stat = fs.statSync(selectedOutputPath);
        var freshState = getProjectState(projectId) || state;
        var uploadedOutputs = clonePlainObject(
          freshState.uploaded_outputs || {},
        );
        var uploadResultsByOutput = clonePlainObject(
          freshState.upload_results_by_output || {},
        );
        uploadedOutputs[selectedOutputPath] = nowIso();
        uploadResultsByOutput[selectedOutputPath] = result;

        var expectedForCompletion = getExpectedOutputPaths(freshState);
        if (expectedForCompletion.length <= 0) {
          expectedForCompletion = expectedOutputs;
        }
        var allUploaded =
          expectedForCompletion.length > 0 &&
          expectedForCompletion.every(function (outputPath) {
            return !!uploadedOutputs[String(outputPath || "")];
          });

        var newState = upsertProjectState(projectId, {
          status: allUploaded ? "uploaded" : "uploaded_partial",
          uploaded_at: nowIso(),
          upload_pending: false,
          output_path: selectedOutputPath,
          expected_outputs: expectedForCompletion,
          uploaded_outputs: uploadedOutputs,
          upload_results_by_output: uploadResultsByOutput,
          last_uploaded_mtime_ms: stat.mtimeMs,
          last_uploaded_output_path: selectedOutputPath,
          last_upload_result: result,
          last_error: null,
        });

        log(
          "Drive upload complete for " +
            projectId +
            " [" +
            path.basename(selectedOutputPath) +
            "]" +
            " (" +
            (result.drive_file_id || "unknown") +
            ")",
          "success",
        );

        var uploadProgress = countUploadedExpectedOutputs(newState);
        if (uploadProgress.total > 0) {
          log(
            "Upload artifacts for " +
              projectId +
              ": " +
              uploadProgress.uploaded +
              "/" +
              uploadProgress.total +
              " completed",
            uploadProgress.uploaded >= uploadProgress.total
              ? "success"
              : "info",
          );
        }
        resetMonitorCandidateSelection(projectId);

        if (!allUploaded) {
          ensureAutomationLeaseActive(lease, "upload_output_partial");
          armExportMonitor(projectId);
          return null;
        }

        ensureAutomationLeaseActive(lease, "upload_output_post_complete");
        disarmExportMonitor(projectId);
        maybeAdvanceBatchAfterUpload();
        return null;
      })
      .catch(function (err) {
        if (isAutomationCanceledError(err)) {
          throw err;
        }
        if (!isAutomationLeaseActive(lease)) {
          throw createAutomationCanceledError(
            projectId,
            "upload_output_superseded",
          );
        }
        upsertProjectState(projectId, {
          status: "upload_failed",
          upload_pending: false,
          last_error: err.message,
        });
        handleBatchFailure(projectId, "Upload failed: " + err.message);
        throw err;
      });
  }

  function queueDownloadImport(projectId, source, options) {
    if (!validateProjectId(projectId)) {
      throw new Error("Invalid project id: " + projectId);
    }
    var queueOptions = options || {};

    var id = String(projectId || "").trim();
    var runtime = ensureBatchRuntime();
    var acceptance = getBatchRuntimeHelper().acceptProject(runtime, id);
    if (!acceptance.accepted) {
      log("Duplicate project ignored for this Premiere session: " + id, "warn");
      return false;
    }

    if (!acceptance.is_sleeping) {
      if (isJobQueued("download_import", id)) {
        log("Download/import already queued for " + id, "warn");
        return false;
      }
    }

    var nextPatch = {
      status: acceptance.is_sleeping ? "sleeping_download" : "queued_download",
      enqueue_source: source || "manual",
      upload_pending: false,
      cleanup_deleted: false,
      cleanup_error: null,
      host_cleanup_error: null,
      host_cleanup_result: null,
      cleanup_retryable: false,
      cleanup_retry_count: 0,
      cleanup_next_retry_at: null,
      completion_notified_status: null,
      completion_notified_at: null,
      batch_queue_state: acceptance.is_sleeping ? "sleeping" : "active",
    };
    if (source === "http") {
      nextPatch.orchestration_metrics =
        getOrchestrationMetricsHelper().createInitialMetrics(
          queueOptions.httpReceivedAt || nowIso(),
        );
    }
    upsertProjectState(id, nextPatch);

    if (!acceptance.is_sleeping) {
      enqueueJob("download_import", {
        project_id: id,
        source: source || "manual",
      });
    }

    projectSelect.value = id;
    syncTrackedBatchProjectMetadata();
    renderQueue();

    return true;
  }

  function queueUpload(projectId, reason, outputPathOverride) {
    if (!isAutomationProjectActive(projectId)) {
      return false;
    }

    var state = getProjectState(projectId);
    if (!state || !state.drive_folder_id) {
      return false;
    }

    var selectedOutputPath = String(
      outputPathOverride || state.output_path || "",
    ).trim();
    if (!selectedOutputPath || !fs.existsSync(selectedOutputPath)) {
      return false;
    }

    if (hasOutputUploadFinished(state, selectedOutputPath)) {
      return false;
    }

    if (isUploadJobQueuedForOutput(projectId, selectedOutputPath)) {
      return false;
    }

    enqueueJob("upload_output", {
      project_id: projectId,
      output_path: selectedOutputPath,
      cleanup_after_upload: false,
      reason: reason || "watch",
    });

    upsertProjectState(projectId, {
      upload_pending: true,
      output_path: selectedOutputPath,
      pending_cleanup_choice: false,
      upload_reason: reason || "watch",
    });

    return true;
  }

  // --- Export monitoring (watch output.mp4 + ATR_*.mp4 stability) ---

  function readOutputStat(outputPath) {
    try {
      if (!fs.existsSync(outputPath)) {
        return null;
      }
      var st = fs.statSync(outputPath);
      if (!st.isFile()) {
        return null;
      }
      return {
        size: st.size,
        mtimeMs: st.mtimeMs,
      };
    } catch (e) {
      return null;
    }
  }

  function listExpectedCandidatesForMonitor(projectId, monitor, state) {
    var candidates = listWatchedOutputPaths(monitor.dir);
    var expected = getExpectedOutputPaths(state).map(function (p) {
      return path.basename(String(p || ""));
    });
    if (expected.length <= 0) {
      return candidates;
    }
    var expectedSet = {};
    expected.forEach(function (name) {
      expectedSet[name.toLowerCase()] = true;
    });
    return candidates.filter(function (candidatePath) {
      var base = path.basename(candidatePath).toLowerCase();
      return !!expectedSet[base];
    });
  }

  function checkExportStability(projectId, source) {
    if (!isAutomationProjectActive(projectId)) {
      return;
    }

    var monitor = exportMonitors[projectId];
    var state = getProjectState(projectId);
    if (!monitor || !state) {
      return;
    }

    if (state.status === "uploading") {
      return;
    }

    var candidatePaths = listExpectedCandidatesForMonitor(
      projectId,
      monitor,
      state,
    );
    var candidateSet = {};
    candidatePaths.forEach(function (candidatePath) {
      candidateSet[candidatePath] = true;
    });

    Object.keys(monitor.output_signatures).forEach(function (trackedPath) {
      if (!candidateSet[trackedPath]) {
        delete monitor.output_signatures[trackedPath];
        delete monitor.output_last_changed_at[trackedPath];
      }
    });

    candidatePaths.forEach(function (candidatePath) {
      if (
        hasOutputUploadFinished(state, candidatePath) ||
        isUploadJobQueuedForOutput(projectId, candidatePath)
      ) {
        return;
      }

      var stat = readOutputStat(candidatePath);
      if (!stat || stat.size <= 0) {
        delete monitor.output_signatures[candidatePath];
        monitor.output_last_changed_at[candidatePath] = Date.now();
        return;
      }

      var signature = stat.size + ":" + stat.mtimeMs;
      if (signature !== monitor.output_signatures[candidatePath]) {
        monitor.output_signatures[candidatePath] = signature;
        monitor.output_last_changed_at[candidatePath] = Date.now();
        return;
      }

      var stableSince = Number(
        monitor.output_last_changed_at[candidatePath] || Date.now(),
      );
      var stableMs = Date.now() - stableSince;
      if (stableMs < EXPORT_STABLE_MS) {
        return;
      }

      if (
        state.last_uploaded_output_path === candidatePath &&
        state.last_uploaded_mtime_ms &&
        Math.abs(state.last_uploaded_mtime_ms - stat.mtimeMs) < 1
      ) {
        return;
      }

      if (queueUpload(projectId, source || "watch_stable", candidatePath)) {
        log(
          "Detected stable " +
            path.basename(candidatePath) +
            " for " +
            projectId +
            " -> upload queued",
          "info",
        );
      }
    });
  }

  function armFsWatcherForMonitor(projectId, monitor) {
    if (!monitor || monitor.fsWatcher) {
      return true;
    }
    try {
      monitor.fsWatcher = fs.watch(monitor.dir, function () {
        checkExportStability(projectId, "watch_event");
      });
      monitor.watch_last_error_key = "";
      return true;
    } catch (watchErr) {
      var code = watchErr && watchErr.code ? String(watchErr.code) : "unknown";
      var msg =
        watchErr && watchErr.message
          ? String(watchErr.message)
          : String(watchErr);
      var errKey = code + ":" + msg;
      if (monitor.watch_last_error_key !== errKey) {
        monitor.watch_last_error_key = errKey;
        log(
          "fs.watch unavailable for " +
            projectId +
            " (" +
            code +
            "), polling only",
          "warn",
        );
      }

      if (!monitor.watchRetryTimer) {
        monitor.watchRetryTimer = setTimeout(function () {
          monitor.watchRetryTimer = null;
          if (exportMonitors[projectId] === monitor && !monitor.fsWatcher) {
            armFsWatcherForMonitor(projectId, monitor);
          }
        }, FS_WATCH_RETRY_DELAY_MS);
      }
      return false;
    }
  }

  function armExportMonitor(projectId) {
    if (!isAutomationProjectActive(projectId)) {
      return;
    }

    var state = getProjectState(projectId);
    if (!state) {
      return;
    }
    if (exportMonitors[projectId]) {
      return;
    }

    var dirPath = "";
    if (state.output_path) {
      dirPath = path.dirname(state.output_path);
    } else if (state.local_root) {
      dirPath = state.local_root;
    }
    if (!dirPath) {
      return;
    }

    var monitor = {
      project_id: projectId,
      dir: dirPath,
      output_signatures: {},
      output_last_changed_at: {},
      intervalId: null,
      fsWatcher: null,
      watchRetryTimer: null,
      watch_last_error_key: "",
    };

    armFsWatcherForMonitor(projectId, monitor);

    monitor.intervalId = setInterval(function () {
      if (!monitor.fsWatcher && !monitor.watchRetryTimer) {
        armFsWatcherForMonitor(projectId, monitor);
      }
      checkExportStability(projectId, "watch_poll");
    }, EXPORT_POLL_INTERVAL_MS);

    exportMonitors[projectId] = monitor;
    log("Export monitor armed for " + projectId, "info");
  }

  function resetMonitorCandidateSelection(projectId) {
    var monitor = exportMonitors[projectId];
    if (!monitor) {
      return;
    }
    monitor.output_signatures = {};
    monitor.output_last_changed_at = {};
  }

  function disarmExportMonitor(projectId) {
    var monitor = exportMonitors[projectId];
    if (!monitor) {
      return;
    }
    try {
      if (monitor.fsWatcher) {
        monitor.fsWatcher.close();
      }
    } catch (e) {}
    try {
      if (monitor.intervalId) {
        clearInterval(monitor.intervalId);
      }
    } catch (e2) {}
    try {
      if (monitor.watchRetryTimer) {
        clearTimeout(monitor.watchRetryTimer);
        monitor.watchRetryTimer = null;
      }
    } catch (e3) {}
    delete exportMonitors[projectId];
  }

  // --- Render project list ---

  function projectStateSort(a, b) {
    var ta =
      Date.parse(a.updated_at || a.created_at || "1970-01-01T00:00:00Z") || 0;
    var tb =
      Date.parse(b.updated_at || b.created_at || "1970-01-01T00:00:00Z") || 0;
    return tb - ta;
  }

  function renderProjectSelect() {
    var previous = projectSelect.value;
    clearChildren(projectSelect);

    var states = Object.keys(projectStates)
      .map(function (id) {
        return projectStates[id];
      })
      .sort(projectStateSort);
    if (states.length === 0) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "No tracked project";
      projectSelect.appendChild(empty);
      return;
    }

    states.forEach(function (state) {
      var option = document.createElement("option");
      option.value = state.project_id;
      option.textContent =
        state.project_id + "  [" + (state.status || "unknown") + "]";
      projectSelect.appendChild(option);
    });

    if (previous && projectStates[previous]) {
      projectSelect.value = previous;
    }
  }

  function renderProjectStates() {
    clearChildren(projectStatusList);

    var states = Object.keys(projectStates)
      .map(function (id) {
        return projectStates[id];
      })
      .sort(projectStateSort)
      .slice(0, 3);

    if (states.length === 0) {
      var empty = document.createElement("li");
      empty.className = "empty-msg";
      empty.textContent = "No recent project yet";
      projectStatusList.appendChild(empty);
      return;
    }

    states.forEach(function (state) {
      var li = document.createElement("li");

      var lineTop = document.createElement("div");
      lineTop.className = "project-line";

      var idSpan = document.createElement("span");
      idSpan.textContent = state.project_id;

      var statusSpan = document.createElement("span");
      statusSpan.className = "project-state";
      statusSpan.textContent = state.status || "unknown";

      var actions = document.createElement("div");
      actions.className = "project-actions";
      actions.appendChild(statusSpan);

      if (
        state.status === "error" ||
        state.status === "upload_failed" ||
        state.status === "cleanup_failed" ||
        state.status === "cleanup_pending" ||
        state.last_error
      ) {
        var resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.className = "project-reset-btn";
        resetBtn.textContent = "Reset";
        resetBtn.setAttribute("data-action", "reset-project");
        resetBtn.setAttribute("data-project-id", state.project_id);
        actions.appendChild(resetBtn);
      }

      if (
        state.local_root &&
        (state.status === "cleanup_failed" ||
          state.status === "cleanup_pending")
      ) {
        var retryCleanupBtn = document.createElement("button");
        retryCleanupBtn.type = "button";
        retryCleanupBtn.className = "project-reset-btn";
        retryCleanupBtn.textContent = "Retry cleanup";
        retryCleanupBtn.setAttribute("data-action", "retry-cleanup");
        retryCleanupBtn.setAttribute("data-project-id", state.project_id);
        actions.appendChild(retryCleanupBtn);
      }

      lineTop.appendChild(idSpan);
      lineTop.appendChild(actions);
      li.appendChild(lineTop);

      var details = [];
      if (state.batch_phase) {
        details.push("Batch: " + state.batch_phase);
      }
      if (state.is_sleeping) {
        details.push("Sleeping: yes");
      }
      if (state.sequence_name) {
        details.push("Sequence: " + state.sequence_name);
      }
      if (state.drive_folder_id) {
        details.push("Drive: " + state.drive_folder_id);
      }
      if (state.output_path) {
        details.push("Output: " + state.output_path);
      }
      if (state.proxy_status) {
        var proxyLabel = "Proxy: " + state.proxy_status;
        if (Number(state.proxy_pending_count || 0) > 0) {
          proxyLabel +=
            " (" + Number(state.proxy_pending_count || 0) + " pending)";
        }
        details.push(proxyLabel);
      }
      if (state.proxy_error) {
        details.push("Proxy error: " + state.proxy_error);
      }
      if (state.last_error) {
        details.push("Error: " + state.last_error);
      }
      if (state.cleanup_error) {
        details.push("Cleanup: " + state.cleanup_error);
      }
      if (state.host_cleanup_error) {
        details.push("Premiere cleanup: " + state.host_cleanup_error);
      }
      if (state.cleanup_next_retry_at && state.status === "cleanup_pending") {
        details.push("Next retry: " + state.cleanup_next_retry_at);
      }
      if (
        state.last_upload_result &&
        state.last_upload_result.drive_file_web_view_link
      ) {
        details.push(
          "Uploaded: " + state.last_upload_result.drive_file_web_view_link,
        );
      }

      if (details.length > 0) {
        var detailsLine = document.createElement("div");
        detailsLine.textContent = details.join(" | ");
        li.appendChild(detailsLine);
      }

      projectStatusList.appendChild(li);
    });
  }

  function handleProjectStatusClick(evt) {
    var target = evt.target;
    if (!target || !target.getAttribute) {
      return;
    }

    var action = target.getAttribute("data-action");
    if (action !== "reset-project" && action !== "retry-cleanup") {
      return;
    }

    var projectId = target.getAttribute("data-project-id") || "";
    if (action === "retry-cleanup") {
      retryPendingCleanup(projectId, "manual");
      return;
    }
    resetProjectState(projectId);
  }

  function hasPendingUploadJobs() {
    if (jobStore.active && String(jobStore.active.type || "") === "upload_output") {
      return true;
    }
    return jobStore.queue.some(function (job) {
      return job && String(job.type || "") === "upload_output";
    });
  }

  function getIncompleteBatchUploadProjectIds() {
    return listExportBatchProjectIds().filter(function (projectId) {
      var state = getProjectState(projectId);
      return !state || !hasAllExpectedUploads(state);
    });
  }

  function handleBatchFailure(projectId, detail) {
    var phases = getBatchRuntimeHelper().PHASES;
    var phase = getBatchPhase();
    if (
      phase !== phases.exporting &&
      phase !== phases.cleaning &&
      phase !== phases.blocked_error
    ) {
      return;
    }

    getBatchRuntimeHelper().markBatchBlocked(ensureBatchRuntime());
    syncTrackedBatchProjectMetadata();
    if (detail) {
      log(
        "Batch blocked" +
          (projectId ? " for " + projectId : "") +
          ": " +
          detail,
        "error",
      );
    }
    updateGlobalStatus();
  }

  function cleanupBatchLocalRoots(batchIds, hostSummary) {
    var results = [];
    var chain = Promise.resolve();

    batchIds.forEach(function (projectId) {
      chain = chain.then(function () {
        var state = getProjectState(projectId);
        if (!state || !state.local_root) {
          results.push({
            project_id: projectId,
            cleanup_result: {
              ok: false,
              error: new Error("Missing local project folder"),
            },
          });
          return null;
        }

        return removePathSafe(state.local_root, {
          maxAttempts: CLEANUP_IMMEDIATE_MAX_ATTEMPTS,
        }).then(function (cleanupResult) {
          results.push({
            project_id: projectId,
            host_summary: hostSummary || null,
            cleanup_result: cleanupResult,
          });
        });
      });
    });

    return chain.then(function () {
      return results;
    });
  }

  function resumeSleepingQueueAfterFinalAck() {
    var promoted = getBatchRuntimeHelper().acknowledgeFinalPopup(
      ensureBatchRuntime(),
    );
    promoted.forEach(function (projectId) {
      if (isJobQueued("download_import", projectId)) {
        return;
      }
      upsertProjectState(projectId, {
        status: "queued_download",
        enqueue_source: "sleeping_queue",
        batch_queue_state: "active",
      });
      enqueueJob("download_import", {
        project_id: projectId,
        source: "sleeping_queue",
      });
    });
    syncTrackedBatchProjectMetadata();
    renderQueue();
    updateGlobalStatus();
    processJobQueue();
  }

  function showFinalBatchCompletionAlert(batchIds) {
    var projectCount = Number((batchIds && batchIds.length) || 0);
    getBatchRuntimeHelper().markAwaitingFinalAck(ensureBatchRuntime());
    syncTrackedBatchProjectMetadata();
    try {
      window.alert(
        "Tiktok Reproducer finished batch generation, export, upload, and cleanup for " +
          projectCount +
          " project(s).",
      );
    } catch (e) {
      log("Final batch alert failed: " + e.message, "warn");
    }
    resumeSleepingQueueAfterFinalAck();
  }

  function runBatchCleanupPhase() {
    var phases = getBatchRuntimeHelper().PHASES;
    var batchIds = listExportBatchProjectIds();
    if (
      batchIds.length === 0 ||
      getBatchPhase() === phases.cleaning ||
      getBatchPhase() === phases.awaiting_final_ack
    ) {
      return;
    }

    getBatchRuntimeHelper().beginCleaningPhase(ensureBatchRuntime());
    batchIds.forEach(function (projectId) {
      disarmExportMonitor(projectId);
      clearCleanupRetry(projectId);
      upsertProjectState(projectId, {
        status: "cleaning",
        host_cleanup_error: null,
        host_cleanup_result: null,
        cleanup_error: null,
        cleanup_retryable: false,
        cleanup_retry_count: 0,
        cleanup_next_retry_at: null,
      });
    });
    log(
      "All batch uploads are complete. Starting global cleanup for " +
        batchIds.length +
        " project(s).",
      "info",
    );
    cancelLocalProxyRenderProcessesForExport();

    var batchCleanupRoots = batchIds
      .map(function (projectId) {
        var state = getProjectState(projectId);
        return state && state.local_root ? String(state.local_root) : "";
      })
      .filter(Boolean);

    cleanupImportedProjectInHost("batch", batchCleanupRoots, false)
      .then(function (hostSummary) {
        batchIds.forEach(function (projectId) {
          upsertProjectState(projectId, {
            host_cleanup_result: hostSummary || null,
            host_cleanup_error: null,
          });
        });
        if (hostSummary && hostSummary.ok === false) {
          var hostDetail = buildHostCleanupErrorDetail(hostSummary);
          batchIds.forEach(function (projectId) {
            upsertProjectState(projectId, {
              status: "cleanup_failed",
              cleanup_error: hostDetail,
              host_cleanup_result: hostSummary || null,
            });
          });
          handleBatchFailure("batch", hostDetail);
          return null;
        }
        return cleanupBatchLocalRoots(batchIds, hostSummary);
      })
      .then(function (results) {
        if (!results) {
          return;
        }

        var failures = [];
        var cleanupSummary =
          getBatchRuntimeHelper().partitionCleanupResults(results);

        cleanupSummary.completed.forEach(function (entry) {
          upsertProjectState(entry.project_id, {
            status: "uploaded_cleaned",
            cleanup_deleted: true,
            cleanup_error: null,
            cleanup_retryable: false,
            cleanup_retry_count: 0,
            cleanup_next_retry_at: null,
          });
        });

        cleanupSummary.retryable.forEach(function (entry) {
          var detail = buildCleanupErrorDetail(entry.cleanup_result || null);
          var nextRetryAt = new Date(
            Date.now() + CLEANUP_RETRY_DELAY_MS,
          ).toISOString();
          upsertProjectState(entry.project_id, {
            status: "cleanup_pending",
            cleanup_error: detail,
            cleanup_retryable: true,
            cleanup_retry_count: 1,
            cleanup_next_retry_at: nextRetryAt,
          });
          log(
            "Cleanup still locked for " +
              entry.project_id +
              " (attempt 1/" +
              CLEANUP_RETRYABLE_MAX_PASSES +
              "), retrying in " +
              Math.round(CLEANUP_RETRY_DELAY_MS / 1000) +
              "s",
            "warn",
          );
          scheduleCleanupRetry(entry.project_id, CLEANUP_RETRY_DELAY_MS);
        });

        cleanupSummary.terminal.forEach(function (entry) {
          var cleanupResult = entry.cleanup_result || null;
          var detail = buildCleanupErrorDetail(cleanupResult);
          failures.push({
            project_id: entry.project_id,
            detail: detail,
          });
          upsertProjectState(entry.project_id, {
            status: "cleanup_failed",
            cleanup_error: detail,
            cleanup_retryable: false,
            cleanup_retry_count: Number(
              (cleanupResult && cleanupResult.attempts) || 1,
            ),
            cleanup_next_retry_at: null,
          });
        });

        if (failures.length > 0) {
          handleBatchFailure(
            failures[0].project_id,
            "Cleanup failed: " + failures[0].detail,
          );
        }
        maybeFinalizeBatchCleanup();
      })
      .catch(function (err) {
        batchIds.forEach(function (projectId) {
          upsertProjectState(projectId, {
            status: "cleanup_failed",
            cleanup_error: err.message,
            host_cleanup_error: err.message,
            cleanup_retryable: false,
            cleanup_next_retry_at: null,
          });
        });
        handleBatchFailure("batch", "Cleanup crashed: " + err.message);
      });
  }

  function maybeAdvanceBatchAfterUpload() {
    if (getBatchPhase() !== getBatchRuntimeHelper().PHASES.exporting) {
      return;
    }
    if (listExportBatchProjectIds().length === 0) {
      return;
    }
    if (hasPendingUploadJobs()) {
      return;
    }
    if (getIncompleteBatchUploadProjectIds().length > 0) {
      return;
    }
    runBatchCleanupPhase();
  }

  function maybeFinalizeBatchCleanup() {
    if (
      !getBatchRuntimeHelper().isBatchCleanupComplete(
        ensureBatchRuntime(),
        projectStates,
      )
    ) {
      return;
    }

    var batchIds = listExportBatchProjectIds();
    if (batchIds.length === 0) {
      return;
    }

    showFinalBatchCompletionAlert(batchIds);
  }

  // --- Local HTTP server ---

  function respondJson(res, statusCode, payload) {
    var body = JSON.stringify(payload);
    res.writeHead(statusCode, {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Length": Buffer.byteLength(body),
      "Cache-Control": "no-store",
    });
    res.end(body);
  }

  function respondHtml(res, statusCode, bodyHtml) {
    var html = String(bodyHtml || "");
    res.writeHead(statusCode, {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Length": Buffer.byteLength(html),
      "Cache-Control": "no-store",
    });
    res.end(html);
  }

  function buildTriggerAcceptedHtml(projectId, queued, reason) {
    var queuedText = queued ? "true" : "false";
    var reasonText = String(reason || "").trim();
    return [
      "<!doctype html><html><head><meta charset='utf-8'><title>Tiktok Reproducer Trigger</title></head><body>",
      "<h3>Job recu</h3>",
      "<p>Projet: <code>",
      projectId,
      "</code></p>",
      "<p>Queued: ",
      queuedText,
      "</p>",
      "<p>Reason: ",
      reasonText || "accepted",
      "</p>",
      "<p>Cette page va se fermer automatiquement.</p>",
      "<script>",
      "(function(){",
      "function closeNow(){",
      "try { window.close(); } catch (e) {}",
      "setTimeout(function(){",
      "if (!window.closed) {",
      "document.body.innerHTML='<p>Operation prise en compte. Cette page va etre fermee.</p>';",
      'setTimeout(function(){ location.replace("about:blank"); }, 250);',
      "}",
      "}, 250);",
      "}",
      "if (document.readyState === 'loading') {",
      "document.addEventListener('DOMContentLoaded', closeNow);",
      "} else {",
      "closeNow();",
      "}",
      "})();",
      "</script>",
      "</body></html>",
    ].join("");
  }

  function buildHealthPayload() {
    return {
      ok: !!(localServerStarted && !localServerError),
      server_started: !!localServerStarted,
      server_error: localServerError,
      port: settings.port,
      drive_configured: isDriveConfigured(),
      batch_phase: getBatchPhase(),
      active_batch_projects: getTrackedBatchProjectIds().length,
      sleeping_projects: listSleepingProjectIds().length,
      active_job: jobStore.active ? jobStore.active.type : null,
      queued_jobs: jobStore.queue.length,
    };
  }

  function handleLocalRequest(req, res) {
    var parsed = url.parse(req.url || "", true);
    var pathname = parsed.pathname || "/";

    if (pathname === "/health") {
      respondJson(res, 200, buildHealthPayload());
      return;
    }

    var triggerMatch = pathname.match(/^\/p\/([A-Za-z0-9_-]+)$/);
    if (triggerMatch) {
      var projectId = triggerMatch[1];
      var httpReceivedAt = nowIso();
      try {
        var queued = queueDownloadImport(projectId, "http", {
          httpReceivedAt: httpReceivedAt,
        });
        respondHtml(
          res,
          202,
          buildTriggerAcceptedHtml(
            projectId,
            queued,
            queued ? "accepted" : "already_tracked",
          ),
        );
        log("HTTP trigger received for project " + projectId, "info");
      } catch (err) {
        respondHtml(
          res,
          400,
          "<html><body><h3>Error</h3><pre>" +
            String(err.message) +
            "</pre></body></html>",
        );
      }
      return;
    }

    var statusMatch = pathname.match(/^\/status\/([A-Za-z0-9_-]+)$/);
    if (statusMatch) {
      var state = getProjectState(statusMatch[1]);
      if (!state) {
        respondJson(res, 404, { error: "Project not tracked" });
        return;
      }
      respondJson(res, 200, state);
      return;
    }

    respondJson(res, 404, { error: "Not found" });
  }

  function stopLocalServer() {
    if (!localServer) {
      return Promise.resolve();
    }

    return new Promise(function (resolve) {
      try {
        localServer.close(function () {
          localServer = null;
          localServerStarted = false;
          resolve();
        });
      } catch (e) {
        localServer = null;
        localServerStarted = false;
        resolve();
      }
    });
  }

  function startLocalServer() {
    return stopLocalServer().then(function () {
      localServerError = null;
      localServerStarted = false;

      return new Promise(function (resolve) {
        var server = http.createServer(handleLocalRequest);
        localServer = server;
        var settled = false;

        function settleOnce() {
          if (settled) {
            return;
          }
          settled = true;
          resolve();
        }

        server.on("error", function (err) {
          localServerError = err.message;
          localServerStarted = false;
          if (err && err.code === "EADDRINUSE") {
            log(
              "Local server failed: port " +
                settings.port +
                " is already in use",
              "error",
            );
          } else {
            log("Local server error: " + err.message, "error");
          }
          updateGlobalStatus();
          settleOnce();
        });

        server.listen(settings.port, "127.0.0.1", function () {
          localServerStarted = true;
          localServerError = null;
          log(
            "Local server listening on http://127.0.0.1:" + settings.port,
            "info",
          );
          updateGlobalStatus();
          settleOnce();
        });
      });
    });
  }

  // --- Hot folder watcher ---

  function processTriggerFile(triggerPath) {
    try {
      var content = fs.readFileSync(triggerPath, "utf8").trim();
      fs.unlinkSync(triggerPath);

      if (!content) {
        log("Empty trigger file ignored", "warn");
        return;
      }
      if (!fs.existsSync(content)) {
        log("Script not found from trigger: " + content, "error");
        return;
      }

      runScript(content).catch(function (err) {
        log("Trigger run failed: " + err.message, "error");
      });
    } catch (e) {
      log("Trigger error: " + e.message, "error");
    }
  }

  function startTriggerWatcher() {
    ensureDir(INBOX_DIR);

    try {
      var existing = fs.readdirSync(INBOX_DIR);
      var cleaned = 0;
      existing.forEach(function (filename) {
        if (filename.endsWith(".trigger")) {
          try {
            fs.unlinkSync(path.join(INBOX_DIR, filename));
            cleaned += 1;
          } catch (e) {
            // ignore
          }
        }
      });
      if (cleaned > 0) {
        log("Cleaned " + cleaned + " stale trigger(s)", "info");
      }
    } catch (eRead) {
      // ignore
    }

    try {
      watcher = fs.watch(INBOX_DIR, function (_eventType, filename) {
        if (!filename || !filename.endsWith(".trigger")) {
          return;
        }
        if (processedTriggers[filename]) {
          return;
        }

        processedTriggers[filename] = true;
        var triggerPath = path.join(INBOX_DIR, filename);

        setTimeout(function () {
          delete processedTriggers[filename];
          if (fs.existsSync(triggerPath)) {
            processTriggerFile(triggerPath);
          }
        }, 200);
      });

      log("Watching hot folder: " + INBOX_DIR, "info");
    } catch (e) {
      log("Watch error: " + e.message, "error");
      setStatus("error");
    }
  }

  // --- Encoder event integration ---

  function startEncoderPolling() {
    if (encoderPollTimer) {
      return;
    }

    encoderPollTimer = setInterval(function () {
      evalHost("pullEncoderEvents()")
        .then(function (result) {
          if (!result || result === "[]") {
            return;
          }

          var events;
          try {
            events = JSON.parse(result);
          } catch (e) {
            return;
          }

          if (!Array.isArray(events) || events.length === 0) {
            return;
          }

          events.forEach(function (eventItem) {
            handleEncoderEvent(eventItem);
          });
        })
        .catch(function () {
          // ignore poll failures
        });

      reconcileProxyingProjects();
    }, ENCODER_POLL_INTERVAL_MS);
  }

  function reconcileProxyingProjects() {
    Object.keys(projectStates).forEach(function (projectId) {
      var state = projectStates[projectId] || {};
      var proxyStatus = String(state.proxy_status || "").trim();
      var previousProxySummary = state.proxy_summary || {};
      var shouldRetryWarning =
        proxyStatus === "warning" &&
        Math.max(0, Number(previousProxySummary.total_targets || 0)) > 0 &&
        Math.max(0, Number(previousProxySummary.completed_targets || 0)) <
          Math.max(0, Number(previousProxySummary.total_targets || 0));
      if (
        proxyStatus !== "proxying" &&
        proxyStatus !== "starting" &&
        !shouldRetryWarning
      ) {
        return;
      }
      if (!state.local_root) {
        return;
      }

      var runtime = proxyReconcileState[projectId] || {
        started_at_ms: 0,
        last_attempt_ms: 0,
        attach_attempts: 0,
        in_flight: false,
      };
      if (runtime.in_flight) {
        proxyReconcileState[projectId] = runtime;
        return;
      }
      var nowMs = Date.now();
      if (!runtime.started_at_ms) {
        runtime.started_at_ms = nowMs;
      }
      if (nowMs - runtime.started_at_ms < 5000) {
        proxyReconcileState[projectId] = runtime;
        return;
      }
      if (nowMs - runtime.last_attempt_ms < 5000) {
        proxyReconcileState[projectId] = runtime;
        return;
      }
      runtime.last_attempt_ms = nowMs;
      runtime.in_flight = true;
      proxyReconcileState[projectId] = runtime;

      var context = readProjectContext(state.local_root);
      var proxyPlan = context && context.proxy_plan ? context.proxy_plan : null;
      if (!proxyPlan || !proxyPlan.enabled) {
        runtime.in_flight = false;
        proxyReconcileState[projectId] = runtime;
        return;
      }

      var dirtyProxyOutputs = removeDirtyProxyOutputs(state.local_root, proxyPlan);
      if (dirtyProxyOutputs > 0) {
        scheduleProxyRenderingSidecar(
          projectId,
          state.local_root,
          proxyPlan,
          proxyLeaseMap[projectId] || null,
        )
          .then(function (scheduleResult) {
            runtime.in_flight = false;
            proxyReconcileState[projectId] = runtime;
            var jobIds =
              scheduleResult && Array.isArray(scheduleResult.job_ids)
                ? scheduleResult.job_ids
                : [];
            if (jobIds.length > 0) {
              registerProxyEncoderJobs(
                projectId,
                jobIds,
                proxyLeaseMap[projectId] || null,
              );
            }
            upsertProjectState(projectId, {
              proxy_status: "proxying",
              proxy_error: null,
              proxy_pending_count: Math.max(1, jobIds.length),
              proxy_job_ids: jobIds,
              proxy_last_run_at: nowIso(),
            });
            log(
              "Regenerating " +
                dirtyProxyOutputs +
                " legacy proxy output(s) for " +
                projectId,
              "info",
            );
          })
          .catch(function (err) {
            runtime.in_flight = false;
            proxyReconcileState[projectId] = runtime;
            upsertProjectState(projectId, {
              proxy_status: "warning",
              proxy_error: err.message,
              proxy_last_run_at: nowIso(),
            });
          });
        return;
      }

      reconcileAtrProjectProxiesInHost(projectId, state.local_root, proxyPlan)
        .then(function (summary) {
          var latestState = getProjectState(projectId) || {};
          if (String(latestState.proxy_status || "").trim() === "canceled") {
            delete proxyReconcileState[projectId];
            return;
          }
          runtime.in_flight = false;
          var errors = Array.isArray(summary.errors)
            ? summary.errors.slice(0)
            : [];
          var totalTargets = Math.max(0, Number(summary.total_targets || 0));
          var completedTargets = Math.max(
            0,
            Number(summary.completed_targets || 0),
          );
          var pendingTargets = Math.max(0, Number(summary.pending || 0));
          var missingOutputs = Math.max(0, Number(summary.missing_outputs || 0));
          var attachPending = Math.max(0, Number(summary.attach_pending || 0));
          var pendingAttachErrors = Array.isArray(summary.attach_pending_errors)
            ? summary.attach_pending_errors
            : [];
          var reconcileJobIds = Array.isArray(summary.job_ids)
            ? summary.job_ids
                .map(function (jobId) {
                  return String(jobId || "").trim();
                })
                .filter(Boolean)
            : [];
          if (reconcileJobIds.length > 0) {
            registerProxyEncoderJobs(
              projectId,
              reconcileJobIds,
              proxyLeaseMap[projectId] || null,
            );
          }
          var mergedProxyJobIds = Array.isArray(latestState.proxy_job_ids)
            ? latestState.proxy_job_ids
                .map(function (jobId) {
                  return String(jobId || "").trim();
                })
                .filter(Boolean)
            : [];
          reconcileJobIds.forEach(function (jobId) {
            if (mergedProxyJobIds.indexOf(jobId) === -1) {
              mergedProxyJobIds.push(jobId);
            }
          });
          var nextStatus = proxyStatus;

          if (completedTargets > 0 && completedTargets >= totalTargets) {
            nextStatus = "ready";
            delete proxyReconcileState[projectId];
          } else if (
            pendingTargets > 0 &&
            missingOutputs <= 0 &&
            attachPending > 0
          ) {
            runtime.attach_attempts = Math.max(
              0,
              Number(runtime.attach_attempts || 0),
            ) + 1;
            proxyReconcileState[projectId] = runtime;
            if (
              runtime.attach_attempts >= PROXY_RECONCILE_MAX_ATTACH_ATTEMPTS
            ) {
              nextStatus = "warning";
              errors.push("Proxy attach retry limit reached");
              delete proxyReconcileState[projectId];
            } else {
              nextStatus = "proxying";
              proxyReconcileState[projectId] = runtime;
              if (
                runtime.attach_attempts === 1 ||
                runtime.attach_attempts % 12 === 0
              ) {
                log(
                  "Proxy attach pending for " +
                    projectId +
                    ": " +
                    completedTargets +
                    "/" +
                    totalTargets +
                    " complete, " +
                    attachPending +
                    " waiting for Premiere attach verification",
                  "info",
                );
              }
            }
          } else if (errors.length > 0 && pendingTargets <= 0) {
            nextStatus = "warning";
            proxyReconcileState[projectId] = runtime;
          } else if (totalTargets > 0) {
            nextStatus = "proxying";
            runtime.attach_attempts = 0;
            proxyReconcileState[projectId] = runtime;
          } else {
            proxyReconcileState[projectId] = runtime;
          }

          upsertProjectState(projectId, {
            proxy_status: nextStatus,
            proxy_error: errors.length > 0 ? errors.join(" | ") : null,
            proxy_pending_count:
              nextStatus === "ready"
                ? 0
                : Math.max(pendingTargets, mergedProxyJobIds.length),
            proxy_job_ids: nextStatus === "ready" ? [] : mergedProxyJobIds,
            proxy_summary: summary,
            proxy_last_run_at: nowIso(),
          });

          if (reconcileJobIds.length > 0) {
            log(
              "Queued " +
                reconcileJobIds.length +
                " proxy repair job(s) for " +
                projectId,
              "info",
            );
          }
          if (completedTargets > 0) {
            log(
              "Proxy reconcile for " +
                projectId +
                ": " +
                completedTargets +
                "/" +
                totalTargets +
                " attached or already compliant",
              nextStatus === "ready" ? "success" : "info",
            );
          }
          if (errors.length > 0) {
            log(
              "Proxy reconcile reported " +
                errors.length +
                " issue(s) for " +
                projectId,
              "warn",
            );
          }
        })
        .catch(function (err) {
          runtime.in_flight = false;
          proxyReconcileState[projectId] = runtime;
          upsertProjectState(projectId, {
            proxy_status: "warning",
            proxy_error: err.message,
            proxy_last_run_at: nowIso(),
          });
          log(
            "Proxy reconcile failed for " + projectId + ": " + err.message,
            "warn",
          );
        });
    });
  }

  function handleHostTraceEvent(eventItem) {
    if (!eventItem || eventItem.type !== "trace") {
      return;
    }

    var projectId = String(eventItem.project_id || "").trim();
    var detail = eventItem.detail || {};
    var message = String(detail.message || "").trim();
    var level = String(detail.level || "info").trim() || "info";

    if (!message) {
      return;
    }

    log(
      (projectId ? "Proxy host " + projectId + ": " : "Proxy host: ") + message,
      level,
    );
  }

  function handleProxySummaryEvent(eventItem) {
    if (!eventItem || eventItem.type !== "proxy_summary") {
      return;
    }

    var projectId = String(eventItem.project_id || "").trim();
    if (!projectId) {
      return;
    }

    var summary = eventItem.detail || {};
    var errors = Array.isArray(summary.errors) ? summary.errors : [];
    var jobIds = Array.isArray(summary.job_ids) ? summary.job_ids : [];
    var existingOutputs = Math.max(0, Number(summary.existing_outputs || 0));
    var previousState = getProjectState(projectId) || {};
    if (String(previousState.proxy_status || "").trim() === "canceled") {
      return;
    }
    var plannedCount = Number(
      (previousState.proxy_summary && previousState.proxy_summary.proxy_needed_count) ||
        0,
    );
    var proxyStatus = null;

    registerProxyEncoderJobs(projectId, jobIds, proxyLeaseMap[projectId] || null);

    if (summary.ffprobe_warning) {
      log(
        "Proxying warning for " + projectId + ": " + summary.ffprobe_warning,
        "warn",
      );
    }
    if (jobIds.length > 0) {
      proxyStatus = "proxying";
      log(
        "Queued " + Number(jobIds.length || 0) + " proxy job(s) for " + projectId,
        "info",
      );
    } else if (existingOutputs > 0) {
      proxyStatus = "proxying";
      log(
        "Found " +
          existingOutputs +
          " existing proxy output(s) for " +
          projectId +
          "; waiting for attach",
        "info",
      );
    } else if (errors.length > 0) {
      proxyStatus = "warning";
      log(
        "Proxy audit reported " + errors.length + " issue(s) for " + projectId,
        "warn",
      );
    } else if (
      Number(summary.attached || 0) > 0 ||
      Number(summary.already_compliant || 0) > 0
    ) {
      proxyStatus = "ready";
      log(
        "Proxy audit for " +
          projectId +
          ": attached=" +
          Number(summary.attached || 0) +
          ", already_compliant=" +
          Number(summary.already_compliant || 0),
        "success",
      );
    } else if (plannedCount > 0) {
      proxyStatus = "warning";
    }

    upsertProjectState(projectId, {
      proxy_status: proxyStatus,
      proxy_error: errors.length > 0 ? errors.join(" | ") : null,
      proxy_pending_count: jobIds.length,
      proxy_job_ids: jobIds,
      proxy_summary: summary,
      proxy_last_run_at: nowIso(),
    });
  }

  function handleEncoderEvent(eventItem) {
    if (!eventItem || !eventItem.type) {
      return;
    }

    if (eventItem.type === "trace") {
      handleHostTraceEvent(eventItem);
      return;
    }
    if (eventItem.type === "proxy_summary") {
      handleProxySummaryEvent(eventItem);
      return;
    }

    var jobId = String(eventItem.job_id || "");
    var renderKind = String(
      (eventItem.detail && eventItem.detail.render_kind) || "video",
    );
    var mappedJob = jobId ? encoderJobMap[jobId] || null : null;
    var fallbackProjectId = String(eventItem.project_id || "").trim();
    var projectId = String(
      mappedJob && mappedJob.project_id ? mappedJob.project_id : fallbackProjectId,
    ).trim();
    var jobLease =
      mappedJob && mappedJob.lease
        ? mappedJob.lease
        : renderKind === "proxy"
          ? proxyLeaseMap[projectId] || null
          : null;
    var outputPath = String(
      (eventItem.detail && eventItem.detail.output_path) || "",
    ).trim();

    if (!jobId || !projectId) {
      return;
    }

    if (jobLease && !isAutomationLeaseActive(jobLease)) {
      if (jobId) {
        delete encoderJobMap[jobId];
      }
      return;
    }
    if (renderKind !== "proxy" && !mappedJob) {
      return;
    }
    if (!jobLease || !isAutomationProjectActive(projectId)) {
      delete encoderJobMap[jobId];
      return;
    }

    if (renderKind === "proxy") {
      handleProxyEncoderEvent(eventItem, jobId, projectId);
      return;
    }

    if (eventItem.type === "queued") {
      var queuedPatch = {
        status: "exporting",
        export_job_id: jobId,
        last_error: null,
      };
      if (renderKind === "audio_no_music") {
        queuedPatch.audio_export_job_id = jobId;
      } else {
        queuedPatch.video_export_job_id = jobId;
      }
      upsertProjectState(projectId, queuedPatch);
      log("Encoder queued for " + projectId + " (job " + jobId + ")", "info");
      return;
    }

    if (eventItem.type === "progress") {
      var progressVal =
        eventItem.detail && typeof eventItem.detail.progress !== "undefined"
          ? Number(eventItem.detail.progress)
          : null;

      if (progressVal !== null && !isNaN(progressVal)) {
        upsertProjectState(projectId, {
          status: "exporting",
          export_job_id: jobId,
          encoder_progress: progressVal,
        });
      }
      return;
    }

    if (eventItem.type === "complete") {
      delete encoderJobMap[jobId];
      var completeState = getProjectState(projectId);
      var completePatch = {
        encoder_progress: null,
      };
      if (renderKind === "audio_no_music") {
        completePatch.audio_export_job_id = null;
      } else {
        completePatch.video_export_job_id = null;
      }

      var videoPending =
        completeState &&
        completeState.video_export_job_id &&
        String(completeState.video_export_job_id) !== String(jobId);
      var audioPending =
        completeState &&
        completeState.audio_export_job_id &&
        String(completeState.audio_export_job_id) !== String(jobId);
      completePatch.export_job_id =
        videoPending || audioPending
          ? completeState.export_job_id || jobId
          : null;
      completePatch.status =
        videoPending || audioPending ? "exporting" : "ready_for_export";

      upsertProjectState(projectId, completePatch);
      log(
        "Encoder completed for " + projectId + " (job " + jobId + ")",
        "success",
      );

      if (
        outputPath &&
        !queueUpload(projectId, "encoder_complete_" + renderKind, outputPath)
      ) {
        armExportMonitor(projectId);
      } else if (!outputPath && !queueUpload(projectId, "encoder_complete")) {
        // fallback to monitor flow
        armExportMonitor(projectId);
      }
      return;
    }

    if (eventItem.type === "error") {
      delete encoderJobMap[jobId];
      var errorText =
        eventItem.detail && eventItem.detail.error
          ? String(eventItem.detail.error)
          : "Encoder error";
      var errorPatch = {
        status: "error",
        export_job_id: null,
        encoder_progress: null,
        last_error: errorText,
      };
      if (renderKind === "audio_no_music") {
        errorPatch.audio_export_job_id = null;
      } else {
        errorPatch.video_export_job_id = null;
      }
      upsertProjectState(projectId, errorPatch);
      log(
        "Encoder error for " + projectId + " (job " + jobId + "): " + errorText,
        "error",
      );
      handleBatchFailure(projectId, "Encoder error: " + errorText);
    }
  }

  function handleProxyEncoderEvent(eventItem, jobId, projectId) {
    var state = getProjectState(projectId) || {};
    if (String(state.proxy_status || "").trim() === "canceled") {
      delete encoderJobMap[jobId];
      return;
    }
    var pendingCount = Math.max(0, Number(state.proxy_pending_count || 0));
    var proxyJobIds = Array.isArray(state.proxy_job_ids)
      ? state.proxy_job_ids.slice(0)
      : [];
    var remainingJobIds = proxyJobIds.filter(function (candidateJobId) {
      return String(candidateJobId || "") !== String(jobId || "");
    });
    var outputPath = String(
      (eventItem.detail && eventItem.detail.output_path) || "",
    ).trim();

    if (eventItem.type === "queued") {
      log(
        "Proxy encoder queued for " + projectId + " (job " + jobId + ")",
        "info",
      );
      upsertProjectState(projectId, {
        proxy_status: "proxying",
      });
      return;
    }

    if (eventItem.type === "progress") {
      if (pendingCount > 0) {
        upsertProjectState(projectId, {
          proxy_status: "proxying",
        });
      }
      return;
    }

    if (eventItem.type === "complete") {
      delete encoderJobMap[jobId];
      var nextPending = Math.max(0, pendingCount - 1);
      var attachedOk = !!(eventItem.detail && eventItem.detail.proxy_attached);
      var attachPending = !!(
        eventItem.detail && eventItem.detail.proxy_attach_pending
      );
      var attachError = String(
        (eventItem.detail && eventItem.detail.proxy_attach_error) || "",
      ).trim();
      upsertProjectState(projectId, {
        proxy_status:
          nextPending > 0 || attachPending
            ? "proxying"
            : attachedOk
              ? "ready"
              : "warning",
        proxy_pending_count: Math.max(nextPending, attachPending ? 1 : 0),
        proxy_error: attachPending ? null : attachError || null,
        proxy_job_ids: remainingJobIds,
      });
      if (attachedOk) {
        log(
          "Proxy attached for " +
            projectId +
            " (" +
            path.basename(outputPath || "proxy") +
            ")",
          "success",
        );
      } else if (attachPending) {
        log(
          "Proxy encode completed for " +
            projectId +
            " and is waiting for Premiere proxy attach verification",
          "info",
        );
      } else {
        log(
          "Proxy encode completed for " +
            projectId +
            " but attach failed: " +
            (attachError || "unknown error"),
          "warn",
        );
      }
      return;
    }

    if (eventItem.type === "error") {
      delete encoderJobMap[jobId];
      var nextPendingError = Math.max(0, pendingCount - 1);
      var errorText =
        eventItem.detail && eventItem.detail.error
          ? String(eventItem.detail.error)
          : "Proxy encoder error";
      upsertProjectState(projectId, {
        proxy_status: nextPendingError > 0 ? "proxying" : "warning",
        proxy_pending_count: nextPendingError,
        proxy_error: errorText,
        proxy_job_ids: remainingJobIds,
      });
      log(
        "Proxy encoder error for " +
          projectId +
          " (job " +
          jobId +
          "): " +
          errorText,
        "warn",
      );
    }
  }

  function startManagedExportForSelectedProject() {
    var runtime = ensureBatchRuntime();
    var exportReadiness = getBatchRuntimeHelper().canStartExport(
      runtime,
      projectStates,
      jobStore,
    );
    if (!exportReadiness.current_batch_ids.length) {
      log("No batch is ready for Export via CEP", "warn");
      return;
    }
    if (!exportReadiness.ok) {
      var blockerSummary = exportReadiness.blockers
        .map(function (blocker) {
          if (blocker.type === "project_status") {
            return blocker.project_id + "=" + blocker.status;
          }
          if (blocker.type === "active_job") {
            return "active_job=" + blocker.job_type;
          }
          if (blocker.type === "queued_job") {
            return "queued_job=" + blocker.job_type;
          }
          return blocker.type || "unknown";
        })
        .join(", ");
      log(
        "Export via CEP blocked until intake is fully ready: " + blockerSummary,
        "warn",
      );
      return;
    }

    var presetPath = String(settings.preset_epr_path || "").trim();
    if (!presetPath) {
      log("Missing .epr preset path in settings", "error");
      setSettingsStatus("Preset .epr path is required", true);
      return;
    }

    if (!fs.existsSync(presetPath)) {
      log("Preset file not found: " + presetPath, "error");
      setSettingsStatus("Preset path is invalid", true);
      return;
    }

    var audioPresetPath = String(settings.audio_preset_epr_path || "").trim();
    if (!audioPresetPath) {
      log("Missing audio preset path for no-music export", "error");
      setSettingsStatus(
        "Audio preset .epr path is required for audio export",
        true,
      );
      return;
    }
    if (!fs.existsSync(audioPresetPath)) {
      log("Audio preset file not found: " + audioPresetPath, "error");
      setSettingsStatus("Audio preset path is invalid", true);
      return;
    }

    setStatus("running");
    log("Clearing Adobe Media Encoder before Export via CEP", "info");
    var locallyDroppedProxyJobs = clearLocalProxyTrackingForExport();

    prepareMediaEncoderForExportInHost()
      .then(function (clearResult) {
        var clearErrors =
          clearResult && Array.isArray(clearResult.errors)
            ? clearResult.errors
            : [];
        if (!clearResult || clearResult.ok === false || !clearResult.cleared_queue) {
          throw new Error(
            clearErrors.length > 0
              ? clearErrors.join(" | ")
              : "Adobe Media Encoder queue was not cleared",
          );
        }

        log(
          "Media Encoder cleared; canceled " +
            Number(
              clearResult.canceled_proxy_jobs || locallyDroppedProxyJobs || 0,
            ) +
            " proxy job(s)",
          "info",
        );

        var preflightBatchIds = exportReadiness.current_batch_ids.slice(0);
        return preflightManagedBatchExportInHost(preflightBatchIds);
      })
      .then(function (preflightResult) {
        if (!preflightResult || preflightResult.ok === false) {
          var missing = preflightResult && Array.isArray(preflightResult.missing)
            ? preflightResult.missing
            : [];
          var missingText = missing
            .map(function (entry) {
              return (
                String(entry.project_id || "unknown") +
                "=" +
                String(entry.sequence_name || "missing")
              );
            })
            .join(", ");
          throw new Error(
            "Batch sequence preflight failed" +
              (missingText ? ": " + missingText : ""),
          );
        }

        var batchIds = getBatchRuntimeHelper().beginExportPhase(runtime);
        syncTrackedBatchProjectMetadata();
        log(
          "Starting batch export for " + batchIds.length + " project(s)",
          "info",
        );

        var sequence = Promise.resolve();
        batchIds.forEach(function (projectId) {
          sequence = sequence.then(function () {
        var state = getProjectState(projectId);
        if (!state || !state.output_path) {
          throw new Error("Project is missing output path: " + projectId);
        }

        if (hasAllExpectedUploads(state)) {
          upsertProjectState(projectId, {
            status: "uploaded",
            pending_cleanup_choice: false,
            audio_export_enabled: true,
            last_error: null,
          });
          log(
            "Skipping export for already uploaded project " + projectId,
            "info",
          );
          return null;
        }

        var audioOutputPath = path.join(
          path.dirname(String(state.output_path)),
          AUDIO_NO_MUSIC_OUTPUT_FILENAME,
        );
        var expectedOutputs = normalizeOutputPathList([
          String(state.output_path),
          String(audioOutputPath),
        ]);
        var sequenceName = String(
          state.sequence_name || buildProjectSequenceName(projectId),
        );
        var hostCall = [
          "startManagedExport(",
          '"',
          escapeForEval(projectId),
          '",',
          '"',
          escapeForEval(String(sequenceName).replace(/\\/g, "/")),
          '",',
          '"',
          escapeForEval(String(state.output_path).replace(/\\/g, "/")),
          '",',
          '"',
          escapeForEval(String(presetPath).replace(/\\/g, "/")),
          '",',
          "1,",
          '"',
          escapeForEval(String(audioOutputPath).replace(/\\/g, "/")),
          '",',
          '"',
          escapeForEval(String(audioPresetPath).replace(/\\/g, "/")),
          '"',
          ")",
        ].join("");

        disarmExportMonitor(projectId);
        resetMonitorCandidateSelection(projectId);
        armExportMonitor(projectId);
        dropEncoderJobsForProject(projectId);

        return evalHost(hostCall).then(function (result) {
          if (result && result.indexOf("ERROR:") === 0) {
            throw new Error(result);
          }

          var jobId = String(result || "").trim();
          var videoJobId = "";
          var audioJobId = "";

          if (jobId && jobId.charAt(0) === "{") {
            try {
              var parsed = JSON.parse(jobId);
              videoJobId = String(parsed.video_job_id || "").trim();
              audioJobId = String(parsed.audio_job_id || "").trim();
            } catch (parseErr) {
              // fallback to legacy payload below
            }
          }

          if (!videoJobId && !audioJobId) {
            videoJobId = jobId;
          }
          if (!videoJobId) {
            throw new Error("Host did not return an encoder job ID");
          }

          encoderJobMap[videoJobId] = {
            project_id: projectId,
            lease: captureAutomationLease(projectId),
          };
          if (audioJobId) {
            encoderJobMap[audioJobId] = {
              project_id: projectId,
              lease: captureAutomationLease(projectId),
            };
          }

          upsertProjectState(projectId, {
            status: "exporting",
            export_job_id: videoJobId,
            video_export_job_id: videoJobId,
            audio_export_job_id: audioJobId || null,
            encoder_progress: 0,
            pending_cleanup_choice: false,
            audio_export_enabled: true,
            audio_output_path: audioOutputPath,
            expected_outputs: expectedOutputs,
            uploaded_outputs: state && state.status === "uploaded"
              ? clonePlainObject(state.uploaded_outputs || {})
              : {},
            upload_results_by_output:
              state && state.status === "uploaded"
                ? clonePlainObject(state.upload_results_by_output || {})
                : {},
            last_upload_result:
              state && state.status === "uploaded"
                ? state.last_upload_result || null
                : null,
            last_error: null,
          });
          log(
            "Managed export started for " +
              projectId +
              " (video job " +
              videoJobId +
              (audioJobId ? ", audio job " + audioJobId : "") +
              ")",
            "info",
          );
          return null;
        });
      });
        });

        return sequence;
      })
      .then(function () {
        maybeAdvanceBatchAfterUpload();
        updateGlobalStatus();
      })
      .catch(function (err) {
        if (getBatchPhase() !== getBatchRuntimeHelper().PHASES.blocked_error) {
          getBatchRuntimeHelper().markBatchBlocked(ensureBatchRuntime());
          syncTrackedBatchProjectMetadata();
        }
        handleBatchFailure("batch", "Export start failed: " + err.message);
        log("Managed batch export failed to start: " + err.message, "error");
        updateGlobalStatus();
      });
  }

  // --- Global status synthesis ---

  function updateGlobalStatus() {
    if (localServerError) {
      setStatus("error");
      return;
    }

    if (getBatchPhase() === getBatchRuntimeHelper().PHASES.blocked_error) {
      setStatus("error");
      return;
    }

    if (
      getBatchPhase() === getBatchRuntimeHelper().PHASES.exporting ||
      getBatchPhase() === getBatchRuntimeHelper().PHASES.cleaning ||
      getBatchPhase() === getBatchRuntimeHelper().PHASES.awaiting_final_ack
    ) {
      setStatus("running");
      return;
    }

    if (jobStore.active) {
      setStatus("running");
      return;
    }

    if (watcher && localServerStarted) {
      setStatus("watching");
      return;
    }

    setStatus("idle");
  }

  // --- UI handlers ---

  function browseAndRun() {
    if (window.cep && window.cep.fs && window.cep.fs.showOpenDialog) {
      var result = window.cep.fs.showOpenDialog(
        false,
        false,
        "Select JSX Script",
        "",
        ["jsx"],
      );
      if (result && result.data && result.data.length > 0) {
        runScript(result.data[0]).catch(function (err) {
          log("Browse run failed: " + err.message, "error");
        });
      }
    } else {
      log("Browse dialog not available", "error");
    }
  }

  function browsePreset() {
    if (window.cep && window.cep.fs && window.cep.fs.showOpenDialog) {
      var result = window.cep.fs.showOpenDialog(
        false,
        false,
        "Select AME Preset (.epr)",
        "",
        ["epr"],
      );
      if (result && result.data && result.data.length > 0) {
        settingPresetEpr.value = result.data[0];
      }
    } else {
      log("Preset browse dialog not available", "error");
    }
  }

  function browseAudioPreset() {
    if (window.cep && window.cep.fs && window.cep.fs.showOpenDialog) {
      var result = window.cep.fs.showOpenDialog(
        false,
        false,
        "Select AME Audio Preset (.epr)",
        "",
        ["epr"],
      );
      if (result && result.data && result.data.length > 0) {
        settingAudioPresetEpr.value = result.data[0];
      }
    } else {
      log("Audio preset browse dialog not available", "error");
    }
  }

  function saveSettingsFromUi() {
    var next = readSettingsForm();
    saveSettings(next);
    renderSettingsForm();
    setSettingsStatus("Settings saved", false);
    log("Settings saved", "success");

    startLocalServer().then(function () {
      updateGlobalStatus();
    });
  }

  function testDriveConnectionFromUi() {
    if (!isDriveConfigured()) {
      setSettingsStatus("Drive settings are incomplete", true);
      log("Cannot test Drive: settings incomplete", "error");
      return;
    }

    setSettingsStatus("Testing Drive...", false);
    setStatus("running");

    var payload = buildDrivePayloadBase();

    runDriveTask("testConnection", payload, null)
      .then(function (result) {
        var label = "Drive OK: " + (result.folder_name || result.folder_id);
        setSettingsStatus(label, false);
        log(label, "success");
        updateGlobalStatus();
      })
      .catch(function (err) {
        setSettingsStatus("Drive test failed: " + err.message, true);
        log("Drive test failed: " + err.message, "error");
        updateGlobalStatus();
      });
  }

  function cleanupBeforeUnload() {
    try {
      if (watcher) {
        watcher.close();
        watcher = null;
      }
    } catch (e) {}

    Object.keys(exportMonitors).forEach(function (projectId) {
      disarmExportMonitor(projectId);
    });

    if (encoderPollTimer) {
      clearInterval(encoderPollTimer);
      encoderPollTimer = null;
    }

    Object.keys(cleanupRetryTimers).forEach(function (projectId) {
      clearCleanupRetry(projectId);
    });

    stopLocalServer();
  }

  // --- Bootstrap ---

  function init() {
    migrateLegacyBaseDir();
    ensureDir(INBOX_DIR);
    ensureDir(STATE_DIR);
    ensureDir(PROJECTS_STATE_DIR);
    ensureDir(UPLOAD_SESSIONS_DIR);

    settings = loadSettings();
    jobStore = loadJobs();
    batchRuntime = getBatchRuntimeHelper().createBatchRuntime();
    projectStates = loadNormalizedProjectStates();

    setSettingsSectionCollapsed(true);
    setLatestProjectsSectionCollapsed(true);
    renderSettingsForm();
    renderQueue();
    renderProjectSelect();
    renderProjectStates();

    btnBrowse.addEventListener("click", browseAndRun);
    btnExportProject.addEventListener(
      "click",
      startManagedExportForSelectedProject,
    );
    btnBrowsePreset.addEventListener("click", browsePreset);
    if (btnBrowseAudioPreset) {
      btnBrowseAudioPreset.addEventListener("click", browseAudioPreset);
    }
    btnSaveSettings.addEventListener("click", saveSettingsFromUi);
    btnTestDrive.addEventListener("click", testDriveConnectionFromUi);
    projectStatusList.addEventListener("click", handleProjectStatusClick);
    if (settingsToggle) {
      settingsToggle.addEventListener("click", toggleSettingsSection);
    }
    if (latestProjectsToggle) {
      latestProjectsToggle.addEventListener(
        "click",
        toggleLatestProjectsSection,
      );
    }

    deleteAfterUploadCheckbox.addEventListener("change", function () {
      settings.delete_after_upload_default =
        !!deleteAfterUploadCheckbox.checked;
      saveSettings(settings);
      setSettingsStatus("Default cleanup behavior updated", false);
    });

    if (exportAudioNoMusicCheckbox) {
      exportAudioNoMusicCheckbox.addEventListener("change", function () {
        settings.export_audio_no_music_default =
          !!exportAudioNoMusicCheckbox.checked;
        saveSettings(settings);
        setSettingsStatus("Default audio export behavior updated", false);
      });
    }

    if (autoProxyNonH264Checkbox) {
      autoProxyNonH264Checkbox.addEventListener("change", function () {
        settings.auto_proxy_non_h264_default =
          !!autoProxyNonH264Checkbox.checked;
        saveSettings(settings);
        setSettingsStatus("Default proxy behavior updated", false);
      });
    }

    projectSelect.addEventListener("change", function () {
      var selectedId = String(projectSelect.value || "");
      var selectedState = getProjectState(selectedId);
      if (selectedState) {
        log("Selected project: " + selectedId, "info");
      }
    });

    evalHost("setPanelPersistent()")
      .then(function (result) {
        if (result && result.indexOf("ERROR:") === 0) {
          log("Panel persistence warning: " + result, "warn");
        }
      })
      .catch(function () {
        // ignore
      });

    evalHost("cleanupOrphanTempAudioSequences()")
      .then(function (result) {
        var normalized = String(result || "").trim();
        if (!normalized) {
          return;
        }
        if (normalized.indexOf("ERROR:") === 0) {
          log("Temp audio sequence cleanup warning: " + normalized, "warn");
          return;
        }
        var removedCount = Number(normalized);
        if (!isNaN(removedCount) && removedCount > 0) {
          log(
            "Removed " + removedCount + " orphan audio temp sequence(s)",
            "info",
          );
        }
      })
      .catch(function () {
        // ignore
      });

    startTriggerWatcher();
    startEncoderPolling();

    startLocalServer().then(function () {
      updateGlobalStatus();
    });

    processJobQueue();

    log(
      "Tiktok Reproducer automation initialized (" + PANEL_BUILD_ID + ")",
      "info",
    );
    updateGlobalStatus();
  }

  window.addEventListener("beforeunload", cleanupBeforeUnload);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
