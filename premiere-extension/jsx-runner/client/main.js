/**
 * JSX Runner - CEP Panel for Premiere Pro 2025
 *
 * Hot folder watcher + Browse & Run for executing .jsx scripts.
 * Uses Node.js (enabled via CEP manifest) for file system watching.
 */

(function () {
    "use strict";

    var cs = new CSInterface();
    var fs = require("fs");
    var path = require("path");
    var os = require("os");

    // Hot folder path: %APPDATA%/Adobe/JSXRunner/inbox/
    var APPDATA = process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming");
    var INBOX_DIR = path.join(APPDATA, "Adobe", "JSXRunner", "inbox");

    // DOM references
    var statusIndicator = document.getElementById("status-indicator");
    var btnBrowse = document.getElementById("btn-browse");
    var recentList = document.getElementById("recent-list");
    var logEl = document.getElementById("log");

    var watcher = null;
    var MAX_RECENT = 8;
    var RECENT_KEY = "jsxrunner_recent";
    var processedTriggers = {}; // Track processed triggers to prevent duplicates

    // --- Utility: clear all children from a DOM element ---

    function clearChildren(el) {
        while (el.firstChild) {
            el.removeChild(el.firstChild);
        }
    }

    // --- Logging ---

    function log(message, level) {
        level = level || "info";
        var entry = document.createElement("div");
        entry.className = "entry " + level;

        var ts = document.createElement("span");
        ts.className = "timestamp";
        var now = new Date();
        ts.textContent = now.toLocaleTimeString();

        entry.appendChild(ts);
        entry.appendChild(document.createTextNode(message));
        logEl.appendChild(entry);
        logEl.scrollTop = logEl.scrollHeight;
    }

    // --- Status indicator ---

    function setStatus(state) {
        statusIndicator.className = state;
        var titles = {
            idle: "Not watching",
            watching: "Watching for scripts",
            running: "Executing script...",
            error: "Error occurred"
        };
        statusIndicator.title = titles[state] || state;
    }

    // --- Recent scripts ---

    function getRecent() {
        try {
            var data = localStorage.getItem(RECENT_KEY);
            return data ? JSON.parse(data) : [];
        } catch (e) {
            return [];
        }
    }

    function saveRecent(list) {
        localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    }

    function addRecent(filePath) {
        var list = getRecent();
        // Remove if already present
        list = list.filter(function (p) { return p !== filePath; });
        // Add to front
        list.unshift(filePath);
        // Trim
        if (list.length > MAX_RECENT) {
            list = list.slice(0, MAX_RECENT);
        }
        saveRecent(list);
        renderRecent();
    }

    function renderRecent() {
        var list = getRecent();
        clearChildren(recentList);

        if (list.length === 0) {
            var li = document.createElement("li");
            li.className = "empty-msg";
            li.textContent = "No recent scripts";
            recentList.appendChild(li);
            return;
        }

        list.forEach(function (filePath) {
            var li = document.createElement("li");
            li.textContent = path.basename(filePath);
            li.title = filePath;
            li.addEventListener("click", function () {
                runScript(filePath);
            });
            recentList.appendChild(li);
        });
    }

    // --- Script execution ---

    function runScript(jsxPath) {
        // Normalize path separators for ExtendScript (use forward slashes)
        var normalized = jsxPath.replace(/\\/g, "/");

        log("Running: " + path.basename(jsxPath), "info");
        setStatus("running");

        // Use the host function which has proper error handling
        var hostScript = 'runScript("' + normalized + '")';
        cs.evalScript(hostScript, function (result) {
            if (result && result.indexOf("ERROR:") === 0) {
                log(result, "error");
                setStatus("error");
            } else {
                log("Completed: " + path.basename(jsxPath), "success");
                setStatus("watching");
            }
        });

        addRecent(jsxPath);
    }

    // --- Hot folder watcher ---

    function ensureInboxDir() {
        try {
            // Create inbox dir recursively
            var parts = INBOX_DIR.split(path.sep);
            var current = "";
            for (var i = 0; i < parts.length; i++) {
                current = current ? path.join(current, parts[i]) : parts[i];
                // On Windows, first part is drive letter like "C:"
                if (current.length <= 3 && current.indexOf(":") !== -1) continue;
                if (!fs.existsSync(current)) {
                    fs.mkdirSync(current);
                }
            }
            return true;
        } catch (e) {
            log("Failed to create inbox: " + e.message, "error");
            return false;
        }
    }

    function processTriggerFile(triggerPath) {
        try {
            var content = fs.readFileSync(triggerPath, "utf8").trim();
            // Delete trigger file immediately
            fs.unlinkSync(triggerPath);

            if (!content) {
                log("Empty trigger file ignored", "error");
                return;
            }

            // The trigger file contains the absolute path to the .jsx
            var jsxPath = content;

            // Verify the .jsx file exists
            if (!fs.existsSync(jsxPath)) {
                log("Script not found: " + jsxPath, "error");
                setStatus("error");
                return;
            }

            runScript(jsxPath);
        } catch (e) {
            log("Trigger error: " + e.message, "error");
        }
    }

    function startWatcher() {
        if (!ensureInboxDir()) {
            setStatus("error");
            return;
        }

        // Clean up any stale trigger files from previous sessions
        // (do NOT execute them — only triggers created while watching should run)
        try {
            var existing = fs.readdirSync(INBOX_DIR);
            var cleaned = 0;
            existing.forEach(function (filename) {
                if (filename.endsWith(".trigger")) {
                    try {
                        fs.unlinkSync(path.join(INBOX_DIR, filename));
                        cleaned++;
                    } catch (e) { /* ignore */ }
                }
            });
            if (cleaned > 0) {
                log("Cleaned " + cleaned + " stale trigger(s)", "info");
            }
        } catch (e) {
            // Ignore read errors on startup
        }

        // Watch for new trigger files
        try {
            watcher = fs.watch(INBOX_DIR, function (eventType, filename) {
                if (!filename || !filename.endsWith(".trigger")) return;

                // Deduplicate: fs.watch on Windows fires multiple events per file
                if (processedTriggers[filename]) return;
                processedTriggers[filename] = true;

                var triggerPath = path.join(INBOX_DIR, filename);

                // Small delay to ensure the file is fully written
                setTimeout(function () {
                    // Clear dedup entry after processing
                    delete processedTriggers[filename];

                    if (fs.existsSync(triggerPath)) {
                        processTriggerFile(triggerPath);
                    }
                }, 200);
            });

            setStatus("watching");
            log("Watching: " + INBOX_DIR, "info");
        } catch (e) {
            log("Watch error: " + e.message, "error");
            setStatus("error");
        }
    }

    // --- Browse & Run ---

    function browseAndRun() {
        if (window.cep && window.cep.fs && window.cep.fs.showOpenDialog) {
            var result = window.cep.fs.showOpenDialog(
                false,  // allowMultipleSelection
                false,  // chooseDirectory
                "Select JSX Script",
                "",     // initialPath
                ["jsx"] // fileTypes
            );

            if (result && result.data && result.data.length > 0) {
                runScript(result.data[0]);
            }
        } else {
            log("Browse dialog not available", "error");
        }
    }

    // --- Initialize ---

    function init() {
        log("JSX Runner v1.0.0", "info");

        btnBrowse.addEventListener("click", browseAndRun);
        renderRecent();
        startWatcher();
    }

    // Wait for DOM
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
