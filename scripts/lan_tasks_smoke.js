// scripts/lan_tasks_smoke.js — dev-only: exercise lan_tasks against a running backend.
// Usage: ATR_LAN_TOKEN=... node scripts/lan_tasks_smoke.js <base_url> <project_id> [<file_to_upload>]
var lanTasks = require("../premiere-extension/tiktok-reproducer/client/lan_tasks.js");

var baseUrl = process.argv[2];
var projectId = process.argv[3];
var uploadFile = process.argv[4];
var settings = {
  lan_base_url: baseUrl,
  lan_token: process.env.ATR_LAN_TOKEN || "",
  lan_probe_timeout_ms: 2500,
};

lanTasks
  .probe(settings)
  .then(function (ping) {
    console.log("probe OK:", JSON.stringify(ping));
    if (!projectId) {
      return null;
    }
    if (uploadFile) {
      return lanTasks.runTask(
        "uploadOutput",
        { settings: settings, project_id: projectId, output_path: uploadFile },
        function (p) {
          if (p.stage !== "upload_progress") {
            console.log(p.stage);
          }
        },
      );
    }
    return lanTasks.runTask(
      "downloadProject",
      { settings: settings, project_id: projectId, app_data_path: "/tmp/atr-lan-smoke" },
      function (p) {
        console.log(p.stage || "progress", p.downloaded_bytes || "");
      },
    );
  })
  .then(function (result) {
    console.log("RESULT:", JSON.stringify(result, null, 2));
  })
  .catch(function (err) {
    console.error("FAILED:", err.message);
    process.exit(1);
  });
