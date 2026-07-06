#!/usr/bin/env node
"use strict";
/**
 * Regression test for the import template's project-item cache.
 *
 * Bug being guarded (introduced 2026-07-05 by warmFullProjectMediaIndex):
 * during batch intake several `__ATR_PROJECT__*` bins coexist in one
 * Premiere project, and every project ships identically named assets
 * (tts_edited.wav, music, overlays). Warming the whole-project index BY
 * NAME let project 2's preload resolve project 1's audio instead of
 * importing its own file.
 *
 * The test extracts the real functions from premiere_import_project_v77.jsx
 * (verbatim, brace-counted) and runs them against a stubbed Premiere
 * project tree. Run: node scripts/template_item_cache_smoke.js
 */

var fs = require("fs");
var path = require("path");
var vm = require("vm");

// Default target is the template; pass a generated import_project.jsx path
// as argv[2] to validate a specific project's script instead.
var TEMPLATE_PATH =
  process.argv[2] ||
  path.join(
    __dirname,
    "..",
    "backend",
    "app",
    "services",
    "templates",
    "premiere_import_project_v77.jsx",
  );

var EXTRACT_FUNCTIONS = [
  "normalizeComparePath",
  "stripKnownExtension",
  "normalizeNameKey",
  "normalizeLooseName",
  "getProjectRootItem",
  "getProjectItemMediaPath",
  "normalizeMediaPathCacheKey",
  "cacheProjectItemByName",
  "cacheProjectItemByMediaPath",
  "cacheProjectItem",
  "getCachedProjectItem",
  "getCachedProjectItemByMediaPath",
  "isBinItem",
  "findChildBinByName",
  "ensureProjectBin",
  "getProjectSearchRoot",
  "moveItemToProjectBin",
  "walkProjectItems",
  "findProjectItemByMediaPathInContainer",
  "warmProjectItemCache",
  "warmFullProjectMediaIndex",
  "findProjectItemInContainer",
  "findProjectItem",
  "findProjectItemLooseInContainer",
  "findProjectItemLoose",
  "findProjectItemByMediaPath",
  "resolveExistingProjectItem",
  "hasProjectItemForName",
];

function extractFunction(source, name) {
  var marker = "  function " + name + "(";
  var start = source.indexOf(marker);
  if (start < 0) {
    throw new Error("Template function not found: " + name);
  }
  var brace = source.indexOf("{", start);
  var depth = 0;
  for (var i = brace; i < source.length; i += 1) {
    var ch = source.charAt(i);
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        return source.substring(start, i + 1);
      }
    }
  }
  throw new Error("Unbalanced braces extracting: " + name);
}

function extractVarBlock(source, name) {
  var re = new RegExp("var " + name + " = \\{[\\s\\S]*?\\};");
  var m = source.match(re);
  if (!m) {
    throw new Error("Template var not found: " + name);
  }
  return m[0];
}

// --- Stub Premiere object model ---

function makeChildren() {
  var arr = [];
  arr.numItems = 0;
  return arr;
}

function addTo(bin, node) {
  bin.children.push(node);
  bin.children.numItems = bin.children.length;
  node.parent = bin;
}

function detach(node) {
  if (!node.parent) return;
  var c = node.parent.children;
  var i = c.indexOf(node);
  if (i >= 0) {
    c.splice(i, 1);
    c.numItems = c.length;
  }
  node.parent = null;
}

function makeBin(name) {
  var bin = {
    name: name,
    type: 2, // ProjectItemType.BIN
    children: makeChildren(),
  };
  bin.createBin = function (childName) {
    var child = makeBin(childName);
    addTo(bin, child);
    return child;
  };
  return bin;
}

function makeItem(name, mediaPath) {
  var item = {
    name: name,
    type: 1,
    _path: mediaPath || "",
  };
  item.getMediaPath = function () {
    return item._path;
  };
  item.moveBin = function (target) {
    detach(item);
    addTo(target, item);
  };
  return item;
}

function buildSandbox(fakeFs, projectId, rootDir) {
  var source = fs.readFileSync(TEMPLATE_PATH, "utf8");
  var pieces = [extractVarBlock(source, "KNOWN_MEDIA_EXTENSIONS")];
  EXTRACT_FUNCTIONS.forEach(function (name) {
    pieces.push(extractFunction(source, name));
  });

  var root = makeBin("__root__");
  var rootWalks = 0;

  function FileStub(p) {
    this._p = String(p || "").replace(/\\/g, "/");
    this.exists = !!fakeFs[this._p];
    this.fsName = this._p;
    this.name = this._p.split("/").pop();
    this.displayName = this.name;
  }

  var sandbox = {
    app: { project: { rootItem: root } },
    ProjectItemType: { BIN: 2 },
    File: FileStub,
    log: function () {},
    PROJECT_ITEM_CACHE: {},
    PROJECT_ITEM_CACHE_WARMED: false,
    FULL_MEDIA_INDEX_WARMED: false,
    PROJECT_IMPORT_BIN: null,
    PROJECT_BIN_NAME: "__ATR_PROJECT__" + projectId,
    ROOT_DIR: rootDir,
    SOURCES_DIR: rootDir + "/sources",
  };
  vm.createContext(sandbox);
  vm.runInContext(pieces.join("\n\n"), sandbox);

  // Count full-root walks to assert the perf optimization stays intact.
  var realWalk = sandbox.walkProjectItems;
  vm.runInContext(
    "walkProjectItems = function (container, visitor) { __walkHook(container); return __realWalk(container, visitor); };",
    Object.assign(sandbox, {
      __realWalk: realWalk,
      __walkHook: function (container) {
        if (container === root) rootWalks += 1;
      },
    }),
  );

  return {
    sandbox: sandbox,
    root: root,
    getRootWalks: function () {
      return rootWalks;
    },
    resetRootWalks: function () {
      rootWalks = 0;
    },
  };
}

// --- Scenario: batch intake, project 2 imports after project 1 ---

var failures = [];

function check(label, actual, expected) {
  var ok = actual === expected;
  console.log((ok ? "PASS" : "FAIL") + "  " + label + (ok ? "" : "  (got " + String(actual) + ", want " + String(expected) + ")"));
  if (!ok) failures.push(label);
}

var P1_ROOT = "/dl/spm_proj_1";
var P2_ROOT = "/dl/spm_proj_2";

var fakeFs = {};
fakeFs[P2_ROOT + "/tts_edited.wav"] = true;
fakeFs[P2_ROOT + "/sources/ep_unique_02.mkv"] = true;

var env = buildSandbox(fakeFs, "p2", P2_ROOT);
var sb = env.sandbox;
var root = env.root;

// Project 1's leftovers (by design during intake): its bin with its own
// same-named audio, a unique video source, and its batch sequence.
var p1Bin = makeBin("__ATR_PROJECT__p1");
addTo(root, p1Bin);
var p1Audio = makeItem("tts_edited.wav", P1_ROOT + "/tts_edited.wav");
addTo(p1Bin, p1Audio);
addTo(p1Bin, makeItem("ep_unique_01.mkv", P1_ROOT + "/sources/ep_unique_01.mkv"));
var p1Sequence = makeItem("ATR_BATCH__p1", "");
addTo(p1Bin, p1Sequence);

// Project 2 preload sequence, as in main(): warm index, warm own-bin cache.
sb.warmFullProjectMediaIndex();
sb.warmProjectItemCache();

// 1) THE BUG: project 2 must NOT resolve project 1's same-named audio.
var resolved = sb.resolveExistingProjectItem(
  "tts_edited.wav",
  "tts_edited",
  sb.normalizeComparePath(P2_ROOT + "/tts_edited.wav"),
);
check("project 2 audio does not resolve to project 1 item", resolved, null);
check(
  "hasProjectItemForName('tts_edited.wav') is false before import",
  sb.hasProjectItemForName("tts_edited.wav"),
  false,
);
check("project 1 audio stayed in project 1 bin", p1Audio.parent, p1Bin);

// 2) Path-based reuse must still work (same absolute path = same file),
//    e.g. re-running a project into a Premiere project that already has it.
var sharedPathItem = makeItem(
  "ep_unique_02.mkv",
  P2_ROOT + "/sources/ep_unique_02.mkv",
);
addTo(p1Bin, sharedPathItem); // parked under a foreign bin on purpose
sb.PROJECT_ITEM_CACHE = {};
sb.PROJECT_ITEM_CACHE_WARMED = false;
sb.FULL_MEDIA_INDEX_WARMED = false;
sb.warmFullProjectMediaIndex();
sb.warmProjectItemCache();
check(
  "exact-path lookup still reuses the existing item",
  sb.findProjectItemByMediaPath(P2_ROOT + "/sources/ep_unique_02.mkv", true),
  sharedPathItem,
);

// 3) Perf invariant: once warmed, a path MISS must not re-walk the root.
env.resetRootWalks();
var missResult = sb.findProjectItemByMediaPath(
  P2_ROOT + "/sources/not_imported_yet.mkv",
  true,
);
check("miss after warm returns null", missResult, null);
check("miss after warm does not walk whole root", env.getRootWalks(), 0);

// 4) Own-bin name lookups still work after import into the project bin.
var ownBin = sb.ensureProjectBin();
var p2Audio = makeItem("tts_edited.wav", P2_ROOT + "/tts_edited.wav");
addTo(ownBin, p2Audio);
check(
  "own-bin name lookup finds the project's own audio",
  sb.findProjectItem("tts_edited.wav"),
  p2Audio,
);

console.log(failures.length === 0 ? "\nALL OK" : "\n" + failures.length + " FAILURE(S)");
process.exit(failures.length === 0 ? 0 : 1);
