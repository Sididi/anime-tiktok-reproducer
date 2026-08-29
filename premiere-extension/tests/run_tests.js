"use strict";

var assert = require("assert");
var childProcess = require("child_process");
var fs = require("fs");
var os = require("os");
var path = require("path");
var vm = require("vm");

var extensionRoot = path.join(__dirname, "..", "tiktok-reproducer");
var clientRoot = path.join(extensionRoot, "client");
var hostPath = path.join(extensionRoot, "host", "host.jsx");

var tests = [];

function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

function collection(items, countProperty) {
  var result = { length: items.length };
  result[countProperty] = items.length;
  items.forEach(function (item, index) {
    result[index] = item;
  });
  return result;
}

function createSequence(name, id, parentBin) {
  var projectItem = {
    name: name,
    nodeId: "item-" + id,
    parent: parentBin,
    children: collection([], "numItems"),
  };
  return {
    name: name,
    sequenceID: id,
    projectItem: projectItem,
  };
}

function createProject(options) {
  var opts = options || {};
  var bin = {
    name: "__ATR_PROJECT__" + String(opts.projectId || ""),
    nodeId: "bin-" + String(opts.identity || "project"),
    children: collection([], "numItems"),
  };
  var sequences = (opts.sequences || []).map(function (entry, index) {
    return createSequence(
      entry.name,
      entry.id || String(opts.identity) + "-seq-" + index,
      bin,
    );
  });
  bin.children = collection(
    sequences.map(function (sequence) {
      return sequence.projectItem;
    }),
    "numItems",
  );
  var rootChildren = opts.withBin === false ? [] : [bin];
  var project = {
    documentID: String(opts.identity || ""),
    name: String(opts.name || opts.identity || "project"),
    path: String(opts.projectPath || "C:/projects/" + opts.identity + ".prproj"),
    rootItem: {
      name: "Root",
      children: collection(rootChildren, "numItems"),
    },
    sequences: collection(sequences, "numSequences"),
    activeSequence:
      opts.activeSequence === false || sequences.length === 0
        ? null
        : sequences[sequences.length - 1],
    openedSequenceIds: [],
    openSequence: function (sequenceId) {
      this.openedSequenceIds.push(String(sequenceId));
      return true;
    },
  };
  return { project: project, bin: bin, sequences: sequences };
}

function createHostHarness(projectRecords, activeProject) {
  var encodedSequences = [];
  var encoder = {
    ENCODE_ENTIRE: 1,
    bind: function () {},
    launchEncoder: function () {},
    startBatch: function () {},
    encodeSequence: function (sequence) {
      encodedSequences.push(sequence);
      return "job-" + encodedSequences.length;
    },
  };
  var projects = projectRecords.map(function (record) {
    return record.project;
  });
  var app = {
    project: activeProject || projects[0] || null,
    projects: collection(projects, "numProjects"),
    encoder: encoder,
    setExtensionPersistent: function () {},
  };

  function FakeFile(filePath) {
    this.fsName = String(filePath || "");
    this.exists = true;
    this.parent = {
      exists: true,
      fsName: path.dirname(this.fsName),
      create: function () {
        this.exists = true;
        return true;
      },
    };
    this.remove = function () {
      this.exists = false;
      return true;
    };
  }

  var context = vm.createContext({
    app: app,
    File: FakeFile,
    Folder: function () {},
    JSON: JSON,
    Date: Date,
    Math: Math,
    Number: Number,
    String: String,
    isNaN: isNaN,
    decodeURI: decodeURI,
    $: {
      global: {},
      sleep: function () {},
    },
  });
  vm.runInContext(fs.readFileSync(hostPath, "utf8"), context, {
    filename: hostPath,
  });
  return {
    app: app,
    context: context,
    encodedSequences: encodedSequences,
  };
}

function preflight(harness, entries) {
  var raw = harness.context.preflightManagedBatchExport(
    JSON.stringify(entries),
  );
  assert.ok(String(raw).indexOf("ERROR:") !== 0, raw);
  return JSON.parse(String(raw));
}

test("concurrent download progress is monotonic and 100% is exact", function () {
  var progress = require(path.join(clientRoot, "download_progress.js"));
  var state = progress.createProgressState();
  var values = [0, 30, 22, 40, 99.9, 100];
  var emitted = [];
  values.forEach(function (value) {
    var event = progress.buildSummaryEvent(state, {
      project_id: "project",
      file_count: 4,
      downloaded_bytes: value,
      total_bytes: 100,
    });
    if (event) {
      emitted.push(event);
    }
  });
  assert.deepStrictEqual(
    emitted.map(function (event) {
      return event.progress_pct;
    }),
    [0, 30, 40, 99, 100],
  );
  emitted.forEach(function (event, index) {
    if (index > 0) {
      assert.ok(
        event.downloaded_bytes >= emitted[index - 1].downloaded_bytes,
      );
    }
  });
  assert.strictEqual(progress.computePercent(999, 1000), 99);
  assert.strictEqual(progress.computePercent(1000, 1000), 100);
  assert.strictEqual(progress.computeConcurrentDownloadedBytes(22, 8), 30);
});

function createHeartbeatHarness() {
  var currentTime = 0;
  var intervals = [];
  var sockets = [];

  function FakeWebSocket(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closeCalls = [];
    sockets.push(this);
  }
  FakeWebSocket.prototype.open = function () {
    this.readyState = 1;
    this.onopen();
  };
  FakeWebSocket.prototype.send = function (payload) {
    this.sent.push(JSON.parse(payload));
  };
  FakeWebSocket.prototype.receive = function (frame) {
    this.onmessage({ data: JSON.stringify(frame) });
  };
  FakeWebSocket.prototype.close = function (code, reason) {
    this.closeCalls.push({ code: code, reason: reason });
    this.readyState = 3;
    if (this.onclose) {
      this.onclose({ code: code, reason: reason });
    }
  };

  var link = require(path.join(clientRoot, "cep_link.js"));
  var client = link.createLinkClient({
    WebSocketImpl: FakeWebSocket,
    getSettings: function () {
      return { link_url: "ws://test", link_token: "token" };
    },
    now: function () {
      return new Date(currentTime);
    },
    setTimeout: function () {
      return { timeout: true };
    },
    clearTimeout: function () {},
    setInterval: function (callback, delay) {
      var timer = { callback: callback, delay: delay, active: true };
      intervals.push(timer);
      return timer;
    },
    clearInterval: function (timer) {
      timer.active = false;
    },
  });

  return {
    client: client,
    connect: function () {
      client.start();
      var socket = sockets[sockets.length - 1];
      socket.open();
      socket.receive({
        type: "auth_ok",
        heartbeat_interval_s: 10,
        pending_count: 0,
      });
      return socket;
    },
    tick: function (atMs) {
      currentTime = atMs;
      var timer = intervals[intervals.length - 1];
      assert.ok(timer && timer.active, "heartbeat interval is not active");
      timer.callback();
    },
  };
}

test("heartbeat closes after a genuinely unanswered probe", function () {
  var harness = createHeartbeatHarness();
  var socket = harness.connect();
  assert.strictEqual(harness.client.getState().heartbeat_interval_ms, 10000);
  harness.tick(10000);
  harness.tick(20000);
  assert.strictEqual(socket.closeCalls.length, 0);
  harness.tick(30000);
  assert.strictEqual(socket.closeCalls.length, 1);
  assert.strictEqual(socket.closeCalls[0].code, 4000);
});

test("heartbeat grants a fresh probe after local timer starvation", function () {
  var harness = createHeartbeatHarness();
  var socket = harness.connect();
  harness.tick(10000);
  harness.tick(35000);
  assert.strictEqual(socket.closeCalls.length, 0);
  assert.strictEqual(
    socket.sent.filter(function (frame) {
      return frame.type === "ping";
    }).length,
    2,
  );
  harness.tick(45000);
  assert.strictEqual(socket.closeCalls.length, 0);
  harness.tick(55000);
  assert.strictEqual(socket.closeCalls.length, 1);
});

test("cleanup phase selection survives restart and manual retry", function () {
  var cleanup = require(path.join(clientRoot, "cleanup_runtime.js"));
  var runtimeState = require(path.join(clientRoot, "runtime_state.js"));
  var legacyState = {
    status: "cleanup_pending",
    local_root: "C:/locked",
    host_cleanup_result: {
      ok: true,
      release_verification: { ok: true },
    },
  };
  assert.strictEqual(cleanup.selectCleanupPhase(legacyState), "disk_only");
  var normalized = runtimeState.normalizeLoadedProjectStates(
    { project: legacyState },
    "2026-08-27T00:00:00.000Z",
  ).states.project;
  assert.strictEqual(normalized.status, "cleanup_failed");
  assert.strictEqual(normalized.host_cleanup_complete, true);
  assert.strictEqual(cleanup.canRetryCleanup(normalized, "scheduled"), false);
  assert.strictEqual(cleanup.canRetryCleanup(normalized, "manual"), true);
  assert.strictEqual(cleanup.selectCleanupPhase(normalized), "disk_only");

  var interruptedCleaning = runtimeState.normalizeLoadedProjectStates(
    {
      project: {
        status: "cleaning",
        local_root: "C:/locked",
        host_cleanup_complete: true,
      },
    },
    "2026-08-27T00:00:00.000Z",
  ).states.project;
  assert.strictEqual(interruptedCleaning.status, "cleanup_failed");
  assert.strictEqual(
    cleanup.canRetryCleanup(interruptedCleaning, "manual"),
    true,
  );
});

test("local recursive deletion uses the asynchronous task path", async function () {
  var driveTasksSource = fs.readFileSync(
    path.join(clientRoot, "drive_tasks.js"),
    "utf8",
  );
  var cleanupSource = driveTasksSource.slice(
    driveTasksSource.indexOf("// --- Local path removal"),
    driveTasksSource.indexOf("function validateSettings"),
  );
  assert.ok(cleanupSource.indexOf("fs.promises.rm") !== -1);
  assert.strictEqual(cleanupSource.indexOf("rmSync"), -1);
  assert.strictEqual(cleanupSource.indexOf("execFileSync"), -1);
  assert.strictEqual(cleanupSource.indexOf("spawnSync"), -1);

  var tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "atr-cleanup-test-"));
  fs.mkdirSync(path.join(tempRoot, "nested"));
  fs.writeFileSync(path.join(tempRoot, "nested", "file.txt"), "test");
  var driveTasks = require(path.join(clientRoot, "drive_tasks.js"));
  var removal = driveTasks.runTask("removeLocalPath", {
    target_path: tempRoot,
    max_attempts: 1,
  });
  assert.ok(removal && typeof removal.then === "function");
  var result = await removal;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(fs.existsSync(tempRoot), false);
});

test("host preflight resolves an exact sequence in a non-active project", function () {
  var scratch = createProject({
    projectId: "scratch",
    identity: "scratch",
    sequences: [],
    withBin: false,
  });
  var owner = createProject({
    projectId: "abc",
    identity: "owner",
    name: "Owner Project",
    sequences: [{ name: "ATR_BATCH__abc", id: "sequence-abc" }],
  });
  var harness = createHostHarness([scratch, owner], scratch.project);
  var result = preflight(harness, [
    {
      project_id: "abc",
      sequence_name: "ATR_BATCH__abc",
      local_root: "C:/downloads/abc",
    },
  ]);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.resolved[0].project_identity, "owner");
  assert.strictEqual(result.resolved[0].sequence_id, "sequence-abc");
  assert.strictEqual(result.resolved[0].repaired_name, false);
});

test("host preflight prefers the populated live wrapper over stale same-identity scratch", function () {
  var staleScratch = createProject({
    projectId: "stale",
    identity: "shared-scratch-document",
    name: "ATR_Automation_Scratch.prproj",
    projectPath: "C:/state/ATR_Automation_Scratch.prproj",
    sequences: [],
    withBin: false,
  });
  var liveScratch = createProject({
    projectId: "live",
    identity: "shared-scratch-document",
    name: "ATR_Automation_Scratch.prproj",
    projectPath: "C:/state/ATR_Automation_Scratch.prproj",
    sequences: [
      { name: "ATR_BATCH__live", id: "sequence-live" },
    ],
  });
  // app.projects returns the stale wrapper first; app.project is the populated
  // live wrapper. The old identity-only dedupe discarded app.project here.
  var harness = createHostHarness(
    [staleScratch, liveScratch],
    liveScratch.project,
  );
  var result = preflight(harness, [
    {
      project_id: "live",
      sequence_name: "ATR_BATCH__live",
      local_root: "C:/downloads/live",
    },
  ]);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.resolved[0].sequence_id, "sequence-live");
});

test("host preflight can recover a populated project through its Project view", function () {
  var staleScratch = createProject({
    projectId: "view",
    identity: "view-scratch-document",
    name: "ATR_Automation_Scratch.prproj",
    projectPath: "C:/state/ATR_Automation_Scratch.prproj",
    sequences: [],
    withBin: false,
  });
  var liveScratch = createProject({
    projectId: "view",
    identity: "view-scratch-document",
    name: "ATR_Automation_Scratch.prproj",
    projectPath: "C:/state/ATR_Automation_Scratch.prproj",
    sequences: [
      { name: "ATR_BATCH__view", id: "sequence-view" },
    ],
  });
  var harness = createHostHarness([staleScratch], staleScratch.project);
  harness.app.getProjectViewIDs = function () {
    return ["live-view"];
  };
  harness.app.getProjectFromViewID = function () {
    return liveScratch.project;
  };
  var result = preflight(harness, [
    {
      project_id: "view",
      sequence_name: "ATR_BATCH__view",
      local_root: "C:/downloads/view",
    },
  ]);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.resolved[0].sequence_id, "sequence-view");
});

test("remembered import references are not reported as globally open projects", function () {
  var liveScratch = createProject({
    projectId: "live-open",
    identity: "live-open-owner",
    sequences: [],
    withBin: false,
  });
  var closedImport = createProject({
    projectId: "closed-import",
    identity: "closed-import-owner",
    sequences: [
      { name: "ATR_BATCH__closed-import", id: "sequence-closed" },
    ],
  });
  var harness = createHostHarness([liveScratch], liveScratch.project);
  harness.context.__atrImportedProjectRefs = [closedImport.project];
  var openProjects = harness.context.__atrGetOpenProjects();
  assert.strictEqual(openProjects.length, 1);
  assert.strictEqual(openProjects[0], liveScratch.project);
});

test("managed runScript validates and captures its exact export target", function () {
  var owner = createProject({
    projectId: "captured",
    identity: "captured-owner",
    sequences: [
      { name: "ATR_BATCH__captured", id: "sequence-captured" },
    ],
  });
  var harness = createHostHarness([owner], owner.project);
  harness.context.$.evalFile = function () {};
  var result = harness.context.runScript(
    "C:/downloads/captured/import_project.jsx",
    "captured",
    "ATR_BATCH__captured",
    "C:/downloads/captured",
  );
  assert.strictEqual(result, "OK");
  assert.strictEqual(
    harness.context.__atrManagedExportTargetsByProjectId.captured.sequence,
    owner.sequences[0],
  );

  var missing = createProject({
    projectId: "missing-capture",
    identity: "missing-capture-owner",
    sequences: [],
  });
  var missingHarness = createHostHarness([missing], missing.project);
  missingHarness.context.$.evalFile = function () {};
  var missingResult = missingHarness.context.runScript(
    "C:/downloads/missing/import_project.jsx",
    "missing-capture",
    "ATR_BATCH__missing-capture",
    "C:/downloads/missing",
  );
  assert.ok(
    String(missingResult).indexOf(
      "ERROR: Managed import created no resolvable sequence",
    ) === 0,
    missingResult,
  );
});

test("host preflight repairs exactly one automation-bin sequence", function () {
  var owner = createProject({
    projectId: "repair",
    identity: "repair-owner",
    sequences: [{ name: "Wrong Name", id: "sequence-repair" }],
  });
  var harness = createHostHarness([owner], owner.project);
  var result = preflight(harness, [
    {
      project_id: "repair",
      sequence_name: "ATR_BATCH__repair",
      local_root: "C:/downloads/repair",
    },
  ]);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.resolved[0].repaired_name, true);
  assert.strictEqual(owner.sequences[0].name, "ATR_BATCH__repair");
});

test("host preflight rejects duplicate and missing automation sequences", function () {
  var duplicate = createProject({
    projectId: "duplicate",
    identity: "duplicate-owner",
    sequences: [
      { name: "First", id: "sequence-one" },
      { name: "Second", id: "sequence-two" },
    ],
  });
  var missing = createProject({
    projectId: "missing",
    identity: "missing-owner",
    sequences: [],
  });
  var harness = createHostHarness([duplicate, missing], duplicate.project);
  var result = preflight(harness, [
    {
      project_id: "duplicate",
      sequence_name: "ATR_BATCH__duplicate",
      local_root: "C:/downloads/duplicate",
    },
    {
      project_id: "missing",
      sequence_name: "ATR_BATCH__missing",
      local_root: "C:/downloads/missing",
    },
  ]);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.ambiguous.length, 1);
  assert.strictEqual(result.ambiguous[0].candidates.length, 2);
  assert.strictEqual(result.missing.length, 1);
  assert.strictEqual(
    result.missing[0].projects.some(function (project) {
      return project.project_name === "missing-owner";
    }),
    true,
  );
});

test("managed export activates and queues through the owning project", function () {
  var scratch = createProject({
    projectId: "scratch",
    identity: "scratch",
    sequences: [],
    withBin: false,
  });
  var owner = createProject({
    projectId: "export",
    identity: "export-owner",
    sequences: [{ name: "ATR_BATCH__export", id: "sequence-export" }],
  });
  var harness = createHostHarness([scratch, owner], scratch.project);
  var result = preflight(harness, [
    {
      project_id: "export",
      sequence_name: "ATR_BATCH__export",
      local_root: "C:/downloads/export",
    },
  ]);
  assert.strictEqual(result.ok, true);
  var raw = harness.context.startManagedExport(
    JSON.stringify({
      resolved_sequence: result.resolved[0],
      output_path: "C:/downloads/export/output.mp4",
      preset_path: "C:/presets/video.epr",
      audio_export: { enabled: false },
    }),
  );
  assert.ok(String(raw).indexOf("ERROR:") !== 0, raw);
  assert.deepStrictEqual(owner.project.openedSequenceIds, ["sequence-export"]);
  assert.strictEqual(harness.encodedSequences[0], owner.sequences[0]);
  assert.strictEqual(harness.app.project, scratch.project);
});

test("managed export uses the captured sequence when SequenceCollection is stale", function () {
  var owner = createProject({
    projectId: "cached",
    identity: "cached-owner",
    sequences: [],
    activeSequence: false,
  });
  var capturedSequence = createSequence(
    "ATR_BATCH__cached",
    "sequence-cached",
    owner.bin,
  );
  var harness = createHostHarness([owner], owner.project);
  harness.context.__atrManagedExportTargetsByProjectId.cached = {
    project: owner.project,
    sequence: capturedSequence,
  };
  var result = preflight(harness, [
    {
      project_id: "cached",
      sequence_name: "ATR_BATCH__cached",
      local_root: "C:/downloads/cached",
    },
  ]);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.resolved[0].registered_target, true);

  var raw = harness.context.startManagedExport(
    JSON.stringify({
      resolved_sequence: result.resolved[0],
      output_path: "C:/downloads/cached/output.mp4",
      preset_path: "C:/presets/video.epr",
      audio_export: { enabled: false },
    }),
  );
  assert.ok(String(raw).indexOf("ERROR:") !== 0, raw);
  assert.deepStrictEqual(owner.project.openedSequenceIds, ["sequence-cached"]);
  assert.strictEqual(harness.encodedSequences[0], capturedSequence);
});

test("managed download/import passes sequence validation metadata to host", function () {
  var mainSource = fs.readFileSync(path.join(clientRoot, "main.js"), "utf8");
  assert.ok(
    /runScript\(\s*importPath,\s*projectId,\s*result\.local_root,/m.test(
      mainSource,
    ),
  );
  assert.ok(
    mainSource.indexOf(
      "buildProjectSequenceName(normalizedManagedProjectId)",
    ) !== -1,
  );
});

test("failed preflight precedes and leaves AME/proxy clearing untouched", function () {
  var owner = createProject({
    projectId: "missing",
    identity: "missing-owner",
    sequences: [],
  });
  var harness = createHostHarness([owner], owner.project);
  var result = preflight(harness, [
    {
      project_id: "missing",
      sequence_name: "ATR_BATCH__missing",
      local_root: "C:/downloads/missing",
    },
  ]);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(harness.encodedSequences.length, 0);

  var mainSource = fs.readFileSync(path.join(clientRoot, "main.js"), "utf8");
  var exportStart = mainSource.indexOf(
    "function startManagedExportForSelectedProject()",
  );
  var exportEnd = mainSource.indexOf("// --- Global status synthesis ---", exportStart);
  var exportSource = mainSource.slice(exportStart, exportEnd);
  assert.ok(
    exportSource.indexOf("preflightManagedBatchExportInHost(preflightBatchIds)") <
      exportSource.indexOf("clearLocalProxyTrackingForExport()"),
  );
  assert.ok(
    exportSource.indexOf("preflightManagedBatchExportInHost(preflightBatchIds)") <
      exportSource.indexOf("prepareMediaEncoderWithRetry(1)"),
  );
});

test("CEP build gate is synchronized and the fork worker is gone", function () {
  var constants = require(path.join(clientRoot, "constants.js"));
  var hostSource = fs.readFileSync(hostPath, "utf8");
  var manifestSource = fs.readFileSync(
    path.join(extensionRoot, "CSXS", "manifest.xml"),
    "utf8",
  );
  var mainSource = fs.readFileSync(path.join(clientRoot, "main.js"), "utf8");
  var hostBuildMatch = hostSource.match(
    /var ATR_HOST_BUILD_ID = "([^"]+)";/,
  );
  assert.ok(hostBuildMatch);
  assert.strictEqual(hostBuildMatch[1], constants.ATR_BUILD_ID);
  assert.ok(manifestSource.indexOf('ExtensionBundleVersion="1.0.5"') !== -1);
  assert.strictEqual(mainSource.indexOf("childProcess.fork"), -1);
  assert.strictEqual(
    fs.existsSync(path.join(clientRoot, "drive_worker.js")),
    false,
  );
});

test("host cleanup closes a proxy-owning project while media is still linked", function () {
  var owner = createProject({
    projectId: "cleanup",
    identity: "cleanup-owner",
    sequences: [],
  });
  var proxyMutationCalls = 0;
  var mediaItem = {
    name: "episode.mkv",
    nodeId: "cleanup-media",
    children: collection([], "numItems"),
    getMediaPath: function () {
      return "C:/downloads/cleanup/sources/episode.mkv";
    },
    hasProxy: function () {
      return true;
    },
    getProxyPath: function () {
      return "C:/downloads/cleanup/proxies/sources/episode__atr_proxy.mp4";
    },
    detachProxy: function () {
      proxyMutationCalls += 1;
      throw new Error("cleanup must not detach proxies");
    },
    setOffline: function () {
      proxyMutationCalls += 1;
      throw new Error("cleanup must not offline media");
    },
  };
  owner.bin.children = collection([mediaItem], "numItems");
  var harness = createHostHarness([owner], owner.project);
  var closeArguments = [];
  owner.project.closeDocument = function (saveFirst, promptIfDirty) {
    closeArguments.push([saveFirst, promptIfDirty]);
    harness.app.project = null;
    harness.app.projects = collection([], "numProjects");
    return 0;
  };

  var raw = harness.context.closeImportedProjectsForCleanup(
    JSON.stringify(["C:/downloads/cleanup"]),
  );
  assert.ok(String(raw).indexOf("ERROR:") !== 0, raw);
  var result = JSON.parse(String(raw));
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.target_project_count, 1);
  assert.strictEqual(result.close_requested_count, 1);
  assert.strictEqual(result.closed_project_count, 1);
  assert.deepStrictEqual(closeArguments, [[0, 0]]);
  assert.strictEqual(proxyMutationCalls, 0);
});

test("cleanup scratch is recreated instead of reopening saved proxy state", function () {
  var harness = createHostHarness([], null);
  var newProjectPaths = [];
  harness.app.newProject = function (projectPath) {
    newProjectPaths.push(String(projectPath));
    var scratch = createProject({
      identity: "fresh-scratch",
      name: "ATR_Automation_Scratch.prproj",
      projectPath: String(projectPath),
      sequences: [],
      withBin: false,
    }).project;
    harness.app.project = scratch;
    harness.app.projects = collection([scratch], "numProjects");
    return true;
  };

  var raw = harness.context.openCleanupScratchProject(
    "C:/state/ATR_Automation_Scratch.prproj",
  );
  assert.ok(String(raw).indexOf("ERROR:") !== 0, raw);
  var result = JSON.parse(String(raw));
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.removed_existing_scratch, true);
  assert.strictEqual(result.created_scratch, true);
  assert.deepStrictEqual(newProjectPaths, [
    "C:/state/ATR_Automation_Scratch.prproj",
  ]);
});

test("cleanup closes the owning project without proxy-item mutation", function () {
  var mainSource = fs.readFileSync(path.join(clientRoot, "main.js"), "utf8");
  var hostSource = fs.readFileSync(hostPath, "utf8");
  var cleanupStart = mainSource.indexOf(
    "function cleanupImportedProjectInHost(",
  );
  var cleanupEnd = mainSource.indexOf(
    "// --- Persistent project states ---",
    cleanupStart,
  );
  var cleanupSource = mainSource.slice(cleanupStart, cleanupEnd);
  var scratchStart = hostSource.indexOf(
    "function openCleanupScratchProject(",
  );
  var scratchEnd = hostSource.indexOf(
    "function verifyImportedProjectsCleanup(",
    scratchStart,
  );
  var scratchSource = hostSource.slice(scratchStart, scratchEnd);

  assert.ok(cleanupStart >= 0 && cleanupEnd > cleanupStart);
  assert.ok(
    cleanupSource.indexOf("closeImportedProjectsForCleanup") !== -1,
  );
  assert.strictEqual(
    cleanupSource.indexOf("prepareImportedProjectsForCleanup"),
    -1,
  );
  assert.strictEqual(
    cleanupSource.indexOf("cleanupImportedProjectsForLocalRoots"),
    -1,
  );
  assert.strictEqual(
    cleanupSource.indexOf("prepareMediaEncoderForExportInHost"),
    -1,
  );
  assert.strictEqual(hostSource.indexOf(".detachProxy("), -1);
  assert.strictEqual(hostSource.indexOf(".setOffline("), -1);
  assert.ok(scratchSource.indexOf("scratchFile.remove()") !== -1);
  assert.ok(scratchSource.indexOf("app.newProject") !== -1);
  assert.strictEqual(scratchSource.indexOf("app.openDocument"), -1);
});

test("all client modules and host.jsx pass syntax checks", function () {
  fs.readdirSync(clientRoot)
    .filter(function (name) {
      return /\.js$/i.test(name);
    })
    .forEach(function (name) {
      var result = childProcess.spawnSync(
        process.execPath,
        ["--check", path.join(clientRoot, name)],
        { encoding: "utf8" },
      );
      assert.strictEqual(result.status, 0, result.stderr || name);
    });
  var hostResult = childProcess.spawnSync(process.execPath, ["--check"], {
    encoding: "utf8",
    input: fs.readFileSync(hostPath, "utf8"),
  });
  assert.strictEqual(hostResult.status, 0, hostResult.stderr);
});

(async function run() {
  var failed = 0;
  for (var i = 0; i < tests.length; i += 1) {
    try {
      await tests[i].fn();
      process.stdout.write("PASS " + tests[i].name + "\n");
    } catch (error) {
      failed += 1;
      process.stderr.write(
        "FAIL " + tests[i].name + "\n" +
          String((error && error.stack) || error) +
          "\n",
      );
    }
  }
  process.stdout.write(
    "\n" + (tests.length - failed) + "/" + tests.length + " tests passed\n",
  );
  if (failed > 0) {
    process.exitCode = 1;
  }
})();
