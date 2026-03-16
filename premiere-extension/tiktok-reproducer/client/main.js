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

    var APPDATA = process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming");
    var LEGACY_BASE_DIR = path.join(APPDATA, "Adobe", "JSXRunner");
    var BASE_DIR = path.join(APPDATA, "Adobe", "TiktokReproducer");
    var INBOX_DIR = path.join(BASE_DIR, "inbox");
    var STATE_DIR = path.join(BASE_DIR, "state");
    var PROJECTS_STATE_DIR = path.join(STATE_DIR, "projects");
    var UPLOAD_SESSIONS_DIR = path.join(STATE_DIR, "upload_sessions");
    var SETTINGS_PATH = path.join(STATE_DIR, "settings.json");
    var JOBS_PATH = path.join(STATE_DIR, "jobs.json");

    var DEFAULT_PORT = 48653;
    var OUTPUT_FILENAME = "output.mp4";
    var AUDIO_NO_MUSIC_OUTPUT_FILENAME = "output_no_music.wav";
    var ATR_OUTPUT_PATTERN = /^ATR_.*\.mp4$/i;
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
    var CLEANUP_REMAINING_PREVIEW_LIMIT = 6;

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
    };

    var statusIndicator = document.getElementById("status-indicator");
    var btnBrowse = document.getElementById("btn-browse");
    var btnExportProject = document.getElementById("btn-export-project");
    var btnBrowsePreset = document.getElementById("btn-browse-preset");
    var btnBrowseAudioPreset = document.getElementById("btn-browse-audio-preset");
    var btnSaveSettings = document.getElementById("btn-save-settings");
    var btnTestDrive = document.getElementById("btn-test-drive");

    var projectSelect = document.getElementById("project-select");
    var exportAudioNoMusicCheckbox = document.getElementById("chk-export-audio-no-music");
    var deleteAfterUploadCheckbox = document.getElementById("chk-delete-after-upload");

    var settingClientId = document.getElementById("setting-client-id");
    var settingClientSecret = document.getElementById("setting-client-secret");
    var settingRefreshToken = document.getElementById("setting-refresh-token");
    var settingParentFolderId = document.getElementById("setting-parent-folder-id");
    var settingPort = document.getElementById("setting-port");
    var settingPresetEpr = document.getElementById("setting-preset-epr");
    var settingAudioPresetEpr = document.getElementById("setting-audio-preset-epr");
    var settingsStatus = document.getElementById("settings-status");
    var settingsSection = document.getElementById("settings-section");
    var settingsToggle = document.getElementById("settings-toggle");
    var latestProjectsSection = document.getElementById("latest-projects-section");
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
    var encoderJobMap = {}; // job_id -> project_id
    var encoderPollTimer = null;
    var driveTasksFallback = null;
    var cleanupRetryTimers = {}; // project_id -> timeoutId

    var settings = null;
    var projectStates = {}; // project_id -> state
    var jobStore = {
        queue: [],
        active: null,
    };

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
        var entry = document.createElement("div");
        entry.className = "entry " + level;

        var ts = document.createElement("span");
        ts.className = "timestamp";
        ts.textContent = new Date().toLocaleTimeString();

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
        var maxCount = Math.max(1, Number(limit || CLEANUP_REMAINING_PREVIEW_LIMIT));
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

    function removePathOnce(targetPath) {
        var normalized = String(targetPath || "").trim();
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
            if (fs.rmSync) {
                fs.rmSync(normalized, {
                    recursive: true,
                    force: true,
                    maxRetries: 0,
                    retryDelay: 0,
                });
            } else {
                fs.rmdirSync(normalized, { recursive: true });
            }
        } catch (e) {
            return {
                ok: false,
                error: e,
            };
        }

        if (fs.existsSync(normalized)) {
            return {
                ok: false,
                error: new Error("Path still exists after cleanup: " + normalized),
            };
        }

        return {
            ok: true,
            removed: true,
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
                    CLEANUP_BACKOFF_BASE_MS * Math.pow(2, attempt - 1)
                );
                return sleep(waitMs).then(tryRemove);
            }

            result.attempts = attempt;
            result.retryable_lock = retryable;
            result.error = result.error || new Error("Unknown cleanup failure");
            result.error.message = formatCleanupError(result.error);
            result.remaining_entries = collectCleanupRemainingEntries(normalized, CLEANUP_REMAINING_PREVIEW_LIMIT);
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
        var listed = normalizeOutputPathList(state && state.expected_outputs ? state.expected_outputs : []);
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

    function listWatchedOutputPaths(dirPath) {
        try {
            var names = fs.readdirSync(dirPath);
            return names.filter(function (name) {
                return isWatchedOutputFileName(name);
            }).sort().map(function (name) {
                return path.join(dirPath, name);
            });
        } catch (e) {
            return [];
        }
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
        merged.export_audio_no_music_default = !!merged.export_audio_no_music_default;

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
        exportAudioNoMusicCheckbox.checked = !!settings.export_audio_no_music_default;
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
        };
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
        setSectionCollapsed(sectionEl, toggleEl, !sectionEl.classList.contains("is-collapsed"));
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

        if (!fs.existsSync(jsxPath)) {
            return Promise.reject(new Error("Script not found: " + jsxPath));
        }

        setStatus("running");
        log("Running: " + path.basename(jsxPath), "info");

        return evalHost('runScript("' + escapeForEval(normalized) + '")').then(function (result) {
            if (result && result.indexOf("ERROR:") === 0) {
                setStatus("error");
                throw new Error(result);
            }
            log("Completed: " + path.basename(jsxPath), "success");
            updateGlobalStatus();
            return result;
        }).catch(function (err) {
            setStatus("error");
            throw err;
        });
    }

    function cleanupImportedProjectInHost(projectId, localRootPath, suppressLogs) {
        var normalizedRoot = String(localRootPath || "").trim();
        var quiet = !!suppressLogs;
        if (!normalizedRoot) {
            return Promise.resolve({
                ok: false,
                skipped: true,
                reason: "missing_local_root",
            });
        }

        var hostCall = [
            'cleanupImportedProjectMedia(',
            '"', escapeForEval(String(normalizedRoot).replace(/\\/g, "/")), '"',
            ')',
        ].join("");

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
                var timelineRemoved = Number(summary.timeline_removed || 0);
                var remainingItems = Number(summary.project_items_remaining || 0);
                var deletedLeaves = Number(summary.leaf_items_deleted || 0);
                var deletedBins = Number(summary.bins_deleted || 0);
                log(
                    "Premiere cleanup for " + projectId
                    + ": timeline=" + timelineRemoved
                    + ", bins=" + deletedBins
                    + ", leafItems=" + deletedLeaves
                    + ", remaining=" + remainingItems,
                    "info"
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

    function getProjectState(projectId) {
        return projectStates[projectId] || null;
    }

    function upsertProjectState(projectId, patch) {
        var previous = projectStates[projectId] || {
            project_id: projectId,
            created_at: nowIso(),
        };

        var merged = {};
        Object.keys(previous).forEach(function (key) {
            merged[key] = previous[key];
        });
        Object.keys(patch || {}).forEach(function (key) {
            merged[key] = patch[key];
        });

        merged.updated_at = nowIso();
        projectStates[projectId] = merged;
        writeJsonAtomic(projectStatePath(projectId), merged);
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

        if (jobStore.active && jobStore.active.payload && jobStore.active.payload.project_id === id) {
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
        });

        log("Project reset: " + id + (removedJobs > 0 ? " (removed " + removedJobs + " queued job(s))" : ""), "success");
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
        var remaining = Array.isArray(cleanupResult.remaining_entries) ? cleanupResult.remaining_entries : [];
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

        parts.push("Remaining Premiere items: " + Number(hostSummary.project_items_remaining || 0));
        parts.push("Deleted bins: " + Number(hostSummary.bins_deleted || 0));
        parts.push("Deleted leaf items: " + Number(hostSummary.leaf_items_deleted || 0));

        var timelineRemoved = Number(hostSummary.timeline_removed || 0);
        if (timelineRemoved > 0) {
            parts.push("Timeline removed: " + timelineRemoved);
        }

        return parts.join(" | ");
    }

    function maybeNotifyProjectCompletion(state) {
        if (!state || !state.project_id) {
            return state;
        }

        var status = String(state.status || "");
        if (status !== "uploaded" && status !== "uploaded_cleaned") {
            return state;
        }
        if (state.completion_notified_status === status) {
            return state;
        }

        var message;
        if (status === "uploaded_cleaned") {
            message = "Tiktok Reproducer finished project " + state.project_id + ": generation, upload, and cleanup are complete.";
        } else {
            message = "Tiktok Reproducer finished project " + state.project_id + ": generation and upload are complete.";
        }

        try {
            window.alert(message);
        } catch (e) {
            log("Completion alert failed for " + state.project_id + ": " + e.message, "warn");
        }

        return upsertProjectState(state.project_id, {
            completion_notified_status: status,
            completion_notified_at: nowIso(),
        });
    }

    function scheduleCleanupRetry(projectId, delayMs) {
        var id = String(projectId || "").trim();
        if (!id) {
            return;
        }
        clearCleanupRetry(id);
        cleanupRetryTimers[id] = setTimeout(function () {
            delete cleanupRetryTimers[id];
            retryPendingCleanup(id, "scheduled");
        }, Math.max(500, Number(delayMs) || CLEANUP_RETRY_DELAY_MS));
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
        if (retryCount >= CLEANUP_RETRYABLE_MAX_PASSES) {
            clearCleanupRetry(id);
            return Promise.resolve(false);
        }

        disarmExportMonitor(id);

        return cleanupImportedProjectInHost(id, state.local_root, true).then(function (hostSummary) {
            upsertProjectState(id, {
                host_cleanup_result: hostSummary || null,
                host_cleanup_error: null,
            });
            if (hostSummary && hostSummary.ok === false) {
                var hostDetail = buildHostCleanupErrorDetail(hostSummary);
                var nextRetryCount = retryCount + 1;
                var canRetryHost = nextRetryCount < CLEANUP_RETRYABLE_MAX_PASSES;
                if (canRetryHost) {
                    var nextRetryAt = new Date(Date.now() + CLEANUP_RETRY_DELAY_MS).toISOString();
                    upsertProjectState(id, {
                        status: "cleanup_pending",
                        cleanup_error: hostDetail,
                        cleanup_retryable: true,
                        cleanup_retry_count: nextRetryCount,
                        cleanup_next_retry_at: nextRetryAt,
                    });
                    log(
                        "Premiere cleanup incomplete for " + id
                        + " (attempt " + nextRetryCount + "/" + CLEANUP_RETRYABLE_MAX_PASSES + "), retrying in "
                        + Math.round(CLEANUP_RETRY_DELAY_MS / 1000) + "s",
                        "warn"
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
                return false;
            }
            return removePathSafe(state.local_root, {
                maxAttempts: CLEANUP_BACKGROUND_MAX_ATTEMPTS,
            });
        }).catch(function (hostErr) {
            clearCleanupRetry(id);
            upsertProjectState(id, {
                host_cleanup_error: hostErr.message,
                status: "cleanup_failed",
                cleanup_error: hostErr.message,
                cleanup_retryable: false,
                cleanup_next_retry_at: null,
            });
            log("Premiere cleanup warning for " + id + " during retry: " + hostErr.message, "warn");
            return false;
        }).then(function (cleanupResult) {
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
                log("Cleanup succeeded for " + id + " after retry (" + source + ")", "success");
                maybeNotifyProjectCompletion(cleanedState);
                return true;
            }

            var detail = buildCleanupErrorDetail(cleanupResult);
            var nextRetryCount = retryCount + 1;
            var retryable = !!cleanupResult.retryable_lock && nextRetryCount < CLEANUP_RETRYABLE_MAX_PASSES;
            if (retryable) {
                var nextRetryAt = new Date(Date.now() + CLEANUP_RETRY_DELAY_MS).toISOString();
                upsertProjectState(id, {
                    status: "cleanup_pending",
                    cleanup_error: detail,
                    cleanup_retryable: true,
                    cleanup_retry_count: nextRetryCount,
                    cleanup_next_retry_at: nextRetryAt,
                });
                log(
                    "Cleanup still locked for " + id + " (attempt " + nextRetryCount + "/" + CLEANUP_RETRYABLE_MAX_PASSES + "), retrying in "
                    + Math.round(CLEANUP_RETRY_DELAY_MS / 1000) + "s",
                    "warn"
                );
                scheduleCleanupRetry(id, CLEANUP_RETRY_DELAY_MS);
                return false;
            }

            clearCleanupRetry(id);
            upsertProjectState(id, {
                status: "cleanup_failed",
                cleanup_error: detail,
                cleanup_retryable: !!cleanupResult.retryable_lock,
                cleanup_retry_count: nextRetryCount,
                cleanup_next_retry_at: null,
            });
            log("Cleanup failed for " + id + ": " + detail, "warn");
            return false;
        }).catch(function (err) {
            clearCleanupRetry(id);
            upsertProjectState(id, {
                status: "cleanup_failed",
                cleanup_error: formatCleanupError(err),
                cleanup_retryable: false,
                cleanup_next_retry_at: null,
            });
            log("Cleanup retry crashed for " + id + ": " + err.message, "error");
            return false;
        });
    }

    // --- Queue persistence ---

    function loadJobs() {
        var loaded = readJson(JOBS_PATH, { queue: [], active: null });
        var queue = Array.isArray(loaded.queue) ? loaded.queue.slice(0) : [];

        if (loaded.active && loaded.active.type && loaded.active.payload) {
            queue.unshift(loaded.active);
        }

        queue = queue.filter(function (job) {
            return job && typeof job.type === "string" && job.payload;
        });

        return {
            queue: queue,
            active: null,
        };
    }

    function persistJobs() {
        writeJsonAtomic(JOBS_PATH, jobStore);
    }

    function generateJobId(type) {
        return type + "_" + Date.now() + "_" + Math.floor(Math.random() * 100000);
    }

    function isJobQueued(type, projectId) {
        var active = jobStore.active;
        if (active && active.type === type && active.payload && active.payload.project_id === projectId) {
            return true;
        }
        return jobStore.queue.some(function (job) {
            return job.type === type && job.payload && job.payload.project_id === projectId;
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
            var projectPart = row.job.payload && row.job.payload.project_id ? row.job.payload.project_id : "-";
            li.textContent = (row.active ? "[ACTIVE] " : "[PENDING] ") + row.job.type + " :: " + projectPart;
            queueList.appendChild(li);
        });
    }

    function processJobQueue() {
        if (jobStore.active || jobStore.queue.length === 0) {
            updateGlobalStatus();
            return;
        }

        var job = jobStore.queue.shift();
        job.status = "running";
        job.updated_at = nowIso();
        jobStore.active = job;
        persistJobs();
        renderQueue();
        setStatus("running");

        executeJob(job).then(function () {
            log("Job completed: " + job.type + " (" + (job.payload.project_id || "-") + ")", "success");
        }).catch(function (err) {
            log("Job failed: " + job.type + " -> " + err.message, "error");
        }).finally(function () {
            jobStore.active = null;
            persistJobs();
            renderQueue();
            processJobQueue();
        });
    }

    function executeJob(job) {
        if (job.type === "download_import") {
            return executeDownloadImport(job.payload.project_id);
        }
        if (job.type === "upload_output") {
            var outputPath = job.payload && job.payload.output_path ? String(job.payload.output_path) : "";
            return executeUploadOutput(
                job.payload.project_id,
                !!job.payload.cleanup_after_upload,
                String(job.payload.reason || "watch"),
                outputPath
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

    function runDriveTask(taskName, payload, onProgress) {
        return new Promise(function (resolve, reject) {
            var workerPath = getClientFilePath("drive_worker.js");
            var child;
            var exitingWithFallback = false;
            try {
                child = childProcess.fork(workerPath, [], {
                    stdio: ["ignore", "ignore", "ignore", "ipc"],
                });
            } catch (forkErr) {
                log("Worker unavailable, using in-process fallback", "warn");
                runDriveTaskFallback(taskName, payload, onProgress).then(resolve).catch(reject);
                return;
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
                reject(err);
            }

            child.on("message", function (msg) {
                if (!msg || settled) {
                    return;
                }
                if (msg.type === "progress") {
                    if (typeof onProgress === "function") {
                        onProgress(msg.progress || {});
                    }
                    return;
                }
                if (msg.type === "result") {
                    settled = true;
                    resolve(msg.result);
                    try {
                        child.kill();
                    } catch (eKill) {}
                    return;
                }
                if (msg.type === "error") {
                    completeWithError(new Error((msg.error && msg.error.message) || "Worker error"));
                }
            });

            child.on("error", function (err) {
                completeWithError(err);
            });

            child.on("exit", function (code) {
                if (settled) {
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
                log("Worker exited with code " + code + ", retrying in-process fallback", "warn");
                runDriveTaskFallback(taskName, payload, onProgress).then(resolve).catch(reject);
            });

            child.send({
                type: "run",
                task: taskName,
                payload: payload,
            });
        });
    }

    // --- Drive automation jobs ---

    function executeDownloadImport(projectId) {
        if (!validateProjectId(projectId)) {
            return Promise.reject(new Error("Invalid project ID: " + projectId));
        }
        if (!isDriveConfigured()) {
            return Promise.reject(new Error("Drive settings are incomplete"));
        }

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
        });

        var downloadPayload = buildDrivePayloadBase();
        downloadPayload.project_id = projectId;

        return runDriveTask("downloadProject", downloadPayload, function (progress) {
            if (progress.stage === "download_tuning") {
                log(
                    "Download tuning for " + projectId
                    + ": concurrency=" + Number(progress.selected_concurrency || 0)
                    + " (" + Number(progress.file_count || 0) + " files)",
                    "info"
                );
            } else if (progress.stage === "download_start") {
                log("Download started for " + projectId + " (" + progress.file_count + " files)", "info");
            } else if (progress.stage === "download_file_complete") {
                log("Downloaded file " + progress.file_index + "/" + progress.file_count + " for " + projectId, "info");
            } else if (progress.stage === "download_complete") {
                var elapsedSec = Math.max(0.001, Number(progress.elapsed_ms || 0) / 1000);
                var totalMb = Number(progress.total_bytes || 0) / (1024 * 1024);
                var speed = totalMb / elapsedSec;
                log(
                    "Download completed for " + projectId
                    + " (" + totalMb.toFixed(1) + " MB in " + elapsedSec.toFixed(1) + "s, "
                    + speed.toFixed(2) + " MB/s)",
                    "success"
                );
            }
        }).then(function (result) {
            var importPath = path.join(result.local_root, "import_project.jsx");
            if (!fs.existsSync(importPath)) {
                throw new Error("import_project.jsx not found in downloaded folder: " + result.local_root);
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
            });

            return runScript(importPath).then(function () {
                var enableAudioNoMusic = !!(exportAudioNoMusicCheckbox && exportAudioNoMusicCheckbox.checked);
                var audioOutputPath = path.join(path.dirname(String(result.output_path || "")), AUDIO_NO_MUSIC_OUTPUT_FILENAME);
                var expectedOutputs = normalizeOutputPathList(
                    enableAudioNoMusic
                        ? [String(result.output_path || ""), String(audioOutputPath || "")]
                        : [String(result.output_path || "")]
                );
                var nextState = upsertProjectState(projectId, {
                    status: "ready_for_export",
                    imported_at: nowIso(),
                    upload_pending: false,
                    pending_cleanup_choice: !!deleteAfterUploadCheckbox.checked,
                    audio_export_enabled: enableAudioNoMusic,
                    audio_output_path: enableAudioNoMusic ? audioOutputPath : "",
                    expected_outputs: expectedOutputs,
                    uploaded_outputs: {},
                    upload_results_by_output: {},
                });
                armExportMonitor(nextState.project_id);
                projectSelect.value = projectId;
            });
        }).catch(function (err) {
            upsertProjectState(projectId, {
                status: "error",
                last_error: err.message,
            });
            throw err;
        });
    }

    function executeUploadOutput(projectId, cleanupAfterUpload, reason, outputPathOverride) {
        var state = getProjectState(projectId);
        var selectedOutputPath = String(outputPathOverride || (state && state.output_path) || "").trim();
        if (!state) {
            return Promise.reject(new Error("Unknown project state: " + projectId));
        }
        if (!state.drive_folder_id) {
            return Promise.reject(new Error("Project has no resolved Drive folder id"));
        }
        if (!selectedOutputPath || !fs.existsSync(selectedOutputPath)) {
            return Promise.reject(new Error("Missing output file for project " + projectId + ": " + selectedOutputPath));
        }
        if (!isDriveConfigured()) {
            return Promise.reject(new Error("Drive settings are incomplete"));
        }

        var expectedOutputs = getExpectedOutputPaths(state);
        if (expectedOutputs.length <= 0 && selectedOutputPath) {
            expectedOutputs = [selectedOutputPath];
        }

        upsertProjectState(projectId, {
            status: "uploading",
            upload_pending: false,
            pending_cleanup_choice: !!cleanupAfterUpload,
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
        uploadPayload.session_state_path = path.join(UPLOAD_SESSIONS_DIR, projectId + ".json");

        var lastProgressPct = -1;

        return runDriveTask("uploadOutput", uploadPayload, function (progress) {
            if (progress.stage === "upload_progress") {
                var pct = Math.round((Number(progress.uploaded_bytes || 0) / Math.max(1, Number(progress.total_bytes || 1))) * 100);
                if (pct !== lastProgressPct && (pct % 5 === 0 || pct === 100)) {
                    lastProgressPct = pct;
                    log("Upload " + projectId + ": " + pct + "%", "info");
                }
            }
        }).then(function (result) {
            var stat = fs.statSync(selectedOutputPath);
            var freshState = getProjectState(projectId) || state;
            var uploadedOutputs = clonePlainObject(freshState.uploaded_outputs || {});
            var uploadResultsByOutput = clonePlainObject(freshState.upload_results_by_output || {});
            uploadedOutputs[selectedOutputPath] = nowIso();
            uploadResultsByOutput[selectedOutputPath] = result;

            var expectedForCompletion = getExpectedOutputPaths(freshState);
            if (expectedForCompletion.length <= 0) {
                expectedForCompletion = expectedOutputs;
            }
            var allUploaded = expectedForCompletion.length > 0 && expectedForCompletion.every(function (outputPath) {
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
                "Drive upload complete for " + projectId
                + " [" + path.basename(selectedOutputPath) + "]"
                + " (" + (result.drive_file_id || "unknown") + ")",
                "success"
            );
            resetMonitorCandidateSelection(projectId);

            if (!allUploaded) {
                armExportMonitor(projectId);
                return null;
            }

            if (cleanupAfterUpload) {
                disarmExportMonitor(projectId);
                clearCleanupRetry(projectId);
                return cleanupImportedProjectInHost(projectId, newState.local_root, false).then(function (hostSummary) {
                    upsertProjectState(projectId, {
                        host_cleanup_result: hostSummary || null,
                        host_cleanup_error: null,
                    });
                    if (hostSummary && hostSummary.ok === false) {
                        var hostDetail = buildHostCleanupErrorDetail(hostSummary);
                        upsertProjectState(projectId, {
                            status: "cleanup_pending",
                            cleanup_error: hostDetail,
                            cleanup_retryable: true,
                            cleanup_retry_count: 1,
                            cleanup_next_retry_at: new Date(Date.now() + CLEANUP_RETRY_DELAY_MS).toISOString(),
                        });
                        log(
                            "Upload succeeded but Premiere cleanup is pending for " + projectId
                            + ". Retrying automatically.",
                            "warn"
                        );
                        scheduleCleanupRetry(projectId, CLEANUP_RETRY_DELAY_MS);
                        return null;
                    }
                    return removePathSafe(newState.local_root, {
                        maxAttempts: CLEANUP_IMMEDIATE_MAX_ATTEMPTS,
                    });
                }).catch(function (hostErr) {
                    upsertProjectState(projectId, {
                        host_cleanup_error: hostErr.message,
                        status: "cleanup_failed",
                        cleanup_error: hostErr.message,
                        cleanup_retryable: false,
                        cleanup_retry_count: 1,
                        cleanup_next_retry_at: null,
                    });
                    log("Premiere cleanup warning for " + projectId + ": " + hostErr.message, "warn");
                    return null;
                }).then(function (cleanupResult) {
                    if (!cleanupResult) {
                        return;
                    }
                    if (cleanupResult.ok) {
                        var cleanedState = upsertProjectState(projectId, {
                            status: "uploaded_cleaned",
                            cleanup_deleted: true,
                            cleanup_error: null,
                            cleanup_retryable: false,
                            cleanup_retry_count: 0,
                            cleanup_next_retry_at: null,
                        });
                        log("Local folder removed for " + projectId, "info");
                        maybeNotifyProjectCompletion(cleanedState);
                        return;
                    }

                    var detail = buildCleanupErrorDetail(cleanupResult);
                    if (cleanupResult.retryable_lock) {
                        upsertProjectState(projectId, {
                            status: "cleanup_pending",
                            cleanup_error: detail,
                            cleanup_retryable: true,
                            cleanup_retry_count: 1,
                            cleanup_next_retry_at: new Date(Date.now() + CLEANUP_RETRY_DELAY_MS).toISOString(),
                        });
                        log(
                            "Upload succeeded but cleanup is pending for " + projectId
                            + " (locked by another process). Retrying automatically.",
                            "warn"
                        );
                        scheduleCleanupRetry(projectId, CLEANUP_RETRY_DELAY_MS);
                        return;
                    }

                    upsertProjectState(projectId, {
                        status: "cleanup_failed",
                        cleanup_error: detail,
                        cleanup_retryable: false,
                        cleanup_retry_count: Number(cleanupResult.attempts || 1),
                        cleanup_next_retry_at: null,
                    });
                    log("Upload succeeded but cleanup failed for " + projectId + ": " + detail, "warn");
                });
            }
            armExportMonitor(projectId);
            maybeNotifyProjectCompletion(newState);
            return null;
        }).catch(function (err) {
            upsertProjectState(projectId, {
                status: "upload_failed",
                upload_pending: false,
                last_error: err.message,
            });
            throw err;
        });
    }

    function queueDownloadImport(projectId, source) {
        if (!validateProjectId(projectId)) {
            throw new Error("Invalid project id: " + projectId);
        }
        if (isJobQueued("download_import", projectId)) {
            log("Download/import already queued for " + projectId, "warn");
            return false;
        }

        enqueueJob("download_import", {
            project_id: projectId,
            source: source || "manual",
        });

        upsertProjectState(projectId, {
            status: "queued_download",
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
        });

        return true;
    }

    function queueUpload(projectId, reason, outputPathOverride) {
        var state = getProjectState(projectId);
        if (!state || !state.drive_folder_id) {
            return false;
        }

        var selectedOutputPath = String(outputPathOverride || state.output_path || "").trim();
        if (!selectedOutputPath || !fs.existsSync(selectedOutputPath)) {
            return false;
        }

        if (hasOutputUploadFinished(state, selectedOutputPath)) {
            return false;
        }

        if (isUploadJobQueuedForOutput(projectId, selectedOutputPath)) {
            return false;
        }

        var cleanupChoice = !!deleteAfterUploadCheckbox.checked;
        enqueueJob("upload_output", {
            project_id: projectId,
            output_path: selectedOutputPath,
            cleanup_after_upload: cleanupChoice,
            reason: reason || "watch",
        });

        upsertProjectState(projectId, {
            upload_pending: true,
            output_path: selectedOutputPath,
            pending_cleanup_choice: cleanupChoice,
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
        var monitor = exportMonitors[projectId];
        var state = getProjectState(projectId);
        if (!monitor || !state) {
            return;
        }

        if (state.status === "uploading") {
            return;
        }

        var candidatePaths = listExpectedCandidatesForMonitor(projectId, monitor, state);
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
            if (hasOutputUploadFinished(state, candidatePath) || isUploadJobQueuedForOutput(projectId, candidatePath)) {
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

            var stableSince = Number(monitor.output_last_changed_at[candidatePath] || Date.now());
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
                    "Detected stable " + path.basename(candidatePath) + " for " + projectId + " -> upload queued",
                    "info"
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
            var msg = watchErr && watchErr.message ? String(watchErr.message) : String(watchErr);
            var errKey = code + ":" + msg;
            if (monitor.watch_last_error_key !== errKey) {
                monitor.watch_last_error_key = errKey;
                log("fs.watch unavailable for " + projectId + " (" + code + "), polling only", "warn");
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

    function armMonitorsForRecoveredStates() {
        Object.keys(projectStates).forEach(function (projectId) {
            var state = projectStates[projectId];
            if (!state) {
                return;
            }

            if (state.status === "downloading" || state.status === "importing") {
                if (!isJobQueued("download_import", projectId)) {
                    enqueueJob("download_import", { project_id: projectId, source: "recovery" });
                }
                return;
            }

            if (state.status === "uploading") {
                if (!isJobQueued("upload_output", projectId)) {
                    enqueueJob("upload_output", {
                        project_id: projectId,
                        output_path: state.output_path || "",
                        cleanup_after_upload: !!state.pending_cleanup_choice,
                        reason: "recovery_upload",
                    });
                }
                return;
            }

            if (
                state.status === "cleanup_pending" ||
                (state.status === "cleanup_failed" && !!state.cleanup_retryable)
            ) {
                scheduleCleanupRetry(projectId, 1000);
                return;
            }

            if (
                state.status === "ready_for_export" ||
                state.status === "upload_failed" ||
                state.status === "uploaded_partial" ||
                state.status === "uploaded" ||
                state.status === "exporting"
            ) {
                armExportMonitor(projectId);
            }
        });
    }

    // --- Render project list ---

    function projectStateSort(a, b) {
        var ta = Date.parse(a.updated_at || a.created_at || "1970-01-01T00:00:00Z") || 0;
        var tb = Date.parse(b.updated_at || b.created_at || "1970-01-01T00:00:00Z") || 0;
        return tb - ta;
    }

    function renderProjectSelect() {
        var previous = projectSelect.value;
        clearChildren(projectSelect);

        var states = Object.keys(projectStates).map(function (id) { return projectStates[id]; }).sort(projectStateSort);
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
            option.textContent = state.project_id + "  [" + (state.status || "unknown") + "]";
            projectSelect.appendChild(option);
        });

        if (previous && projectStates[previous]) {
            projectSelect.value = previous;
        }
    }

    function renderProjectStates() {
        clearChildren(projectStatusList);

        var states = Object.keys(projectStates).map(function (id) { return projectStates[id]; }).sort(projectStateSort).slice(0, 3);

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
                (
                    state.status === "cleanup_failed" ||
                    state.status === "cleanup_pending"
                )
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
            if (state.drive_folder_id) {
                details.push("Drive: " + state.drive_folder_id);
            }
            if (state.output_path) {
                details.push("Output: " + state.output_path);
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
            if (state.last_upload_result && state.last_upload_result.drive_file_web_view_link) {
                details.push("Uploaded: " + state.last_upload_result.drive_file_web_view_link);
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

    function buildTriggerAcceptedHtml(projectId, queued) {
        var queuedText = queued ? "true" : "false";
        return [
            "<!doctype html><html><head><meta charset='utf-8'><title>Tiktok Reproducer Trigger</title></head><body>",
            "<h3>Job recu</h3>",
            "<p>Projet: <code>", projectId, "</code></p>",
            "<p>Queued: ", queuedText, "</p>",
            "<p>Cette page va se fermer automatiquement.</p>",
            "<script>",
            "(function(){",
            "function closeNow(){",
            "try { window.close(); } catch (e) {}",
            "setTimeout(function(){",
            "if (!window.closed) {",
            "document.body.innerHTML='<p>Operation prise en compte. Cette page va etre fermee.</p>';",
            "setTimeout(function(){ location.replace(\"about:blank\"); }, 250);",
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
            try {
                var queued = queueDownloadImport(projectId, "http");
                respondHtml(
                    res,
                    202,
                    buildTriggerAcceptedHtml(projectId, queued),
                );
                log("HTTP trigger received for project " + projectId, "info");
            } catch (err) {
                respondHtml(res, 400, "<html><body><h3>Error</h3><pre>" + String(err.message) + "</pre></body></html>");
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
                        log("Local server failed: port " + settings.port + " is already in use", "error");
                    } else {
                        log("Local server error: " + err.message, "error");
                    }
                    updateGlobalStatus();
                    settleOnce();
                });

                server.listen(settings.port, "127.0.0.1", function () {
                    localServerStarted = true;
                    localServerError = null;
                    log("Local server listening on http://127.0.0.1:" + settings.port, "info");
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
            evalHost("pullEncoderEvents()").then(function (result) {
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
            }).catch(function () {
                // ignore poll failures
            });
        }, ENCODER_POLL_INTERVAL_MS);
    }

    function handleEncoderEvent(eventItem) {
        if (!eventItem || !eventItem.type) {
            return;
        }

        var jobId = String(eventItem.job_id || "");
        var projectId = String(eventItem.project_id || "") || encoderJobMap[jobId] || "";
        var renderKind = String(eventItem.detail && eventItem.detail.render_kind || "video");
        var outputPath = String(eventItem.detail && eventItem.detail.output_path || "").trim();

        if (projectId && jobId) {
            encoderJobMap[jobId] = projectId;
        }

        if (!projectId) {
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
            var progressVal = eventItem.detail && typeof eventItem.detail.progress !== "undefined"
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

            var videoPending = completeState && completeState.video_export_job_id && String(completeState.video_export_job_id) !== String(jobId);
            var audioPending = completeState && completeState.audio_export_job_id && String(completeState.audio_export_job_id) !== String(jobId);
            completePatch.export_job_id = videoPending || audioPending ? (completeState.export_job_id || jobId) : null;
            completePatch.status = videoPending || audioPending ? "exporting" : "ready_for_export";

            upsertProjectState(projectId, completePatch);
            log("Encoder completed for " + projectId + " (job " + jobId + ")", "success");

            if (outputPath && !queueUpload(projectId, "encoder_complete_" + renderKind, outputPath)) {
                armExportMonitor(projectId);
            } else if (!outputPath && !queueUpload(projectId, "encoder_complete")) {
                // fallback to monitor flow
                armExportMonitor(projectId);
            }
            return;
        }

        if (eventItem.type === "error") {
            delete encoderJobMap[jobId];
            var errorText = (eventItem.detail && eventItem.detail.error) ? String(eventItem.detail.error) : "Encoder error";
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
            log("Encoder error for " + projectId + " (job " + jobId + "): " + errorText, "error");
        }
    }

    function startManagedExportForSelectedProject() {
        var projectId = String(projectSelect.value || "").trim();
        if (!projectId) {
            log("No project selected for managed export", "warn");
            return;
        }

        var state = getProjectState(projectId);
        if (!state || !state.output_path) {
            log("Selected project has no output path yet", "error");
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

        var enableAudioNoMusic = !!(exportAudioNoMusicCheckbox && exportAudioNoMusicCheckbox.checked);
        var audioOutputPath = path.join(path.dirname(String(state.output_path)), AUDIO_NO_MUSIC_OUTPUT_FILENAME);
        var audioPresetPath = String(settings.audio_preset_epr_path || "").trim() || presetPath;
        if (enableAudioNoMusic && !audioPresetPath) {
            log("Missing audio preset path for no-music export", "error");
            setSettingsStatus("Audio preset .epr path is required for audio export", true);
            return;
        }
        if (enableAudioNoMusic && !fs.existsSync(audioPresetPath)) {
            log("Audio preset file not found: " + audioPresetPath, "error");
            setSettingsStatus("Audio preset path is invalid", true);
            return;
        }

        var expectedOutputs = [String(state.output_path)];
        if (enableAudioNoMusic) {
            expectedOutputs.push(String(audioOutputPath));
        }
        expectedOutputs = normalizeOutputPathList(expectedOutputs);

        var hostCall = [
            'startManagedExport(',
            '"', escapeForEval(projectId), '",',
            '"', escapeForEval(String(state.output_path).replace(/\\/g, "/")), '",',
            '"', escapeForEval(String(presetPath).replace(/\\/g, "/")), '",',
            enableAudioNoMusic ? '1,' : '0,',
            '"', escapeForEval(String(audioOutputPath).replace(/\\/g, "/")), '",',
            '"', escapeForEval(String(audioPresetPath).replace(/\\/g, "/")), '"',
            ')',
        ].join("");

        setStatus("running");

        evalHost(hostCall).then(function (result) {
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

            encoderJobMap[videoJobId] = projectId;
            if (audioJobId) {
                encoderJobMap[audioJobId] = projectId;
            }
            upsertProjectState(projectId, {
                status: "exporting",
                export_job_id: videoJobId,
                video_export_job_id: videoJobId,
                audio_export_job_id: audioJobId || null,
                encoder_progress: 0,
                pending_cleanup_choice: !!deleteAfterUploadCheckbox.checked,
                audio_export_enabled: enableAudioNoMusic,
                audio_output_path: enableAudioNoMusic ? audioOutputPath : "",
                expected_outputs: expectedOutputs,
                uploaded_outputs: {},
                upload_results_by_output: {},
                last_error: null,
            });
            log("Managed export started for " + projectId + " (video job " + videoJobId + (audioJobId ? ", audio job " + audioJobId : "") + ")", "info");
            updateGlobalStatus();
        }).catch(function (err) {
            upsertProjectState(projectId, {
                status: "error",
                last_error: err.message,
            });
            log("Managed export failed to start: " + err.message, "error");
            updateGlobalStatus();
        });
    }

    // --- Global status synthesis ---

    function updateGlobalStatus() {
        if (localServerError) {
            setStatus("error");
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
            var result = window.cep.fs.showOpenDialog(false, false, "Select JSX Script", "", ["jsx"]);
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
            var result = window.cep.fs.showOpenDialog(false, false, "Select AME Preset (.epr)", "", ["epr"]);
            if (result && result.data && result.data.length > 0) {
                settingPresetEpr.value = result.data[0];
            }
        } else {
            log("Preset browse dialog not available", "error");
        }
    }

    function browseAudioPreset() {
        if (window.cep && window.cep.fs && window.cep.fs.showOpenDialog) {
            var result = window.cep.fs.showOpenDialog(false, false, "Select AME Audio Preset (.epr)", "", ["epr"]);
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

        runDriveTask("testConnection", payload, null).then(function (result) {
            var label = "Drive OK: " + (result.folder_name || result.folder_id);
            setSettingsStatus(label, false);
            log(label, "success");
            updateGlobalStatus();
        }).catch(function (err) {
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
        projectStates = loadProjectStates();

        setSettingsSectionCollapsed(true);
        setLatestProjectsSectionCollapsed(true);
        renderSettingsForm();
        renderQueue();
        renderProjectSelect();
        renderProjectStates();

        btnBrowse.addEventListener("click", browseAndRun);
        btnExportProject.addEventListener("click", startManagedExportForSelectedProject);
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
            latestProjectsToggle.addEventListener("click", toggleLatestProjectsSection);
        }

        deleteAfterUploadCheckbox.addEventListener("change", function () {
            settings.delete_after_upload_default = !!deleteAfterUploadCheckbox.checked;
            saveSettings(settings);
            setSettingsStatus("Default cleanup behavior updated", false);
        });

        if (exportAudioNoMusicCheckbox) {
            exportAudioNoMusicCheckbox.addEventListener("change", function () {
                settings.export_audio_no_music_default = !!exportAudioNoMusicCheckbox.checked;
                saveSettings(settings);
                setSettingsStatus("Default audio export behavior updated", false);
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

        startTriggerWatcher();
        startEncoderPolling();

        startLocalServer().then(function () {
            updateGlobalStatus();
        });

        armMonitorsForRecoveredStates();
        processJobQueue();

        log("Tiktok Reproducer automation initialized", "info");
        updateGlobalStatus();
    }

    window.addEventListener("beforeunload", cleanupBeforeUnload);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
