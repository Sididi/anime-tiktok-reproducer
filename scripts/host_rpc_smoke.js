#!/usr/bin/env node
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");
var hostRpcModule = require("../premiere-extension/tiktok-reproducer/client/host_rpc.js");
var constants = require("../premiere-extension/tiktok-reproducer/client/constants.js");

function flushMicrotasks() {
  return Promise.resolve().then(function () {
    return Promise.resolve();
  });
}

async function main() {
  var hostSource = fs.readFileSync(
    path.join(
      __dirname,
      "..",
      "premiere-extension",
      "tiktok-reproducer",
      "host",
      "host.jsx",
    ),
    "utf8",
  );
  var hostBuildMatch = hostSource.match(
    /var ATR_HOST_BUILD_ID = "([^"]+)";/,
  );
  assert.ok(hostBuildMatch, "host build identifier is missing");
  assert.strictEqual(hostBuildMatch[1], constants.ATR_BUILD_ID);
  assert.match(hostSource, /function ATR_getHostBuildId\(\)/);

  var dispatched = [];
  var callbacks = [];
  var rpc = hostRpcModule.createHostRpc(function (script, callback) {
    dispatched.push(script);
    callbacks.push(callback);
  });

  var first = rpc.call("first()", { label: "first" });
  var low = rpc.call("poll()", { priority: -100, label: "poll" });
  var high = rpc.call("import()", { priority: 100, label: "import" });
  assert.deepStrictEqual(dispatched, ["first()"]);

  callbacks.shift()("OK-first");
  assert.strictEqual(await first, "OK-first");
  assert.deepStrictEqual(dispatched, ["first()", "import()"]);

  callbacks.shift()("OK-import");
  assert.strictEqual(await high, "OK-import");
  assert.deepStrictEqual(dispatched, ["first()", "import()", "poll()"]);

  callbacks.shift()("[]");
  assert.strictEqual(await low, "[]");

  var coalescedA = rpc.call("pollAgain()", {
    coalesceKey: "encoder_poll",
    priority: -100,
  });
  var coalescedB = rpc.call("pollAgain()", {
    coalesceKey: "encoder_poll",
    priority: -100,
  });
  assert.strictEqual(coalescedA, coalescedB);
  callbacks.shift()("[]");
  await Promise.all([coalescedA, coalescedB]);
  assert.strictEqual(
    dispatched.filter(function (script) {
      return script === "pollAgain()";
    }).length,
    1,
  );

  var emptyResult = rpc.call("empty()");
  callbacks.shift()("");
  await assert.rejects(emptyResult, /empty response/i);

  var cepError = rpc.call("missingFunction()");
  callbacks.shift()("EvalScript error.");
  await assert.rejects(cepError, /EvalScript error/i);

  var missingResult = rpc.call("missingResult()");
  callbacks.shift()(undefined);
  await assert.rejects(missingResult, /no response/i);

  await flushMicrotasks();
  assert.deepStrictEqual(rpc.getState(), {
    active: false,
    active_label: null,
    queued: 0,
  });
  process.stdout.write("host_rpc_smoke: OK\n");
}

main().catch(function (err) {
  process.stderr.write((err && err.stack ? err.stack : String(err)) + "\n");
  process.exitCode = 1;
});
