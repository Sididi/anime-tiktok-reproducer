"use strict";

/**
 * cep_link.js — Premiere Link client.
 *
 * Keeps one WebSocket open to the VPS (server/ — /api/cep/ws). The VPS holds
 * the queue of "launch project X" requests the backend posts after a Drive
 * export, replays undelivered ones when we (re)connect, and pushes new ones
 * live. For every `launch` frame the panel runs the same intake as the
 * `/p/{project_id}` localhost URL and answers with an `ack` whose result the
 * VPS writes into the project's Discord message.
 *
 * Uses the browser-native WebSocket of the CEF panel page (no `ws` package);
 * side-effect free: main.js injects settings, logging and the launch handler.
 *
 * Frames — panel -> VPS: auth (first), pong, ping, ack
 *          VPS -> panel: auth_ok, launch, ping, pong, error
 * Close codes: 4401 auth rejected/timeout, 4400 protocol, 4408 server
 * heartbeat timeout, 1012 server restart, 4000 client heartbeat timeout,
 * 1000 client stop.
 */

var DEFAULT_LINK_URL = "wss://tiktok.sididi.tv/api/cep/ws";
var HEARTBEAT_INTERVAL_MS = 25000;
var HEARTBEAT_TIMEOUT_MS = 2 * HEARTBEAT_INTERVAL_MS;
var AUTH_TIMEOUT_MS = 5000;
var RECONNECT_MIN_MS = 1000;
var RECONNECT_MAX_MS = 60000;
var TEST_TIMEOUT_MS = 8000;
var MIN_SERVER_HEARTBEAT_INTERVAL_MS = 1000;
var MAX_SERVER_HEARTBEAT_INTERVAL_MS = 300000;

var CLOSE_CODES = {
  AUTH_FAILED: 4401,
  PROTOCOL: 4400,
  SERVER_HEARTBEAT: 4408,
  SERVER_RESTART: 1012,
  CLIENT_HEARTBEAT: 4000,
  CLIENT_STOP: 1000,
};

function isEnabled(settings) {
  return !!(
    settings &&
    String(settings.link_url || "").trim() &&
    String(settings.link_token || "").trim()
  );
}

function normalizeLinkUrl(value) {
  var text = String(value == null ? "" : value).trim();
  return text || DEFAULT_LINK_URL;
}

function isValidLinkUrl(value) {
  return /^wss?:\/\/\S+$/i.test(String(value || ""));
}

function computeBackoffMs(attempt, randomFn) {
  var exponent = Math.max(0, Number(attempt) || 0);
  var base = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * Math.pow(2, exponent));
  var random = typeof randomFn === "function" ? Number(randomFn()) : Math.random();
  if (!(random >= 0 && random <= 1)) {
    random = 0.5;
  }
  return Math.min(RECONNECT_MAX_MS, Math.round(base * (0.5 + random)));
}

function describeClose(code, reason) {
  var text = "code " + code;
  if (reason) {
    text += ": " + reason;
  }
  return text;
}

function normalizeHeartbeatIntervalMs(intervalSeconds) {
  var parsedMs = Number(intervalSeconds) * 1000;
  if (!isFinite(parsedMs) || parsedMs <= 0) {
    return HEARTBEAT_INTERVAL_MS;
  }
  return Math.max(
    MIN_SERVER_HEARTBEAT_INTERVAL_MS,
    Math.min(MAX_SERVER_HEARTBEAT_INTERVAL_MS, Math.round(parsedMs)),
  );
}

function createLinkClient(options) {
  var opts = options || {};
  var getSettings = opts.getSettings || function () { return {}; };
  var WebSocketImpl =
    opts.WebSocketImpl ||
    (typeof WebSocket !== "undefined" ? WebSocket : null);
  var log = opts.log || function () {};
  var onLaunch =
    opts.onLaunch ||
    function () {
      return { result: "error", detail: "no launch handler installed" };
    };
  var onStateChange = opts.onStateChange || function () {};
  var now = opts.now || function () { return new Date(); };
  var setTimeoutFn = opts.setTimeout || setTimeout;
  var clearTimeoutFn = opts.clearTimeout || clearTimeout;
  var setIntervalFn = opts.setInterval || setInterval;
  var clearIntervalFn = opts.clearInterval || clearInterval;
  var panelBuildId = opts.panelBuildId || null;
  var getPort = opts.getPort || function () { return null; };

  var state = {
    enabled: false,
    connected: false,
    authenticated: false,
    connecting: false,
    url: "",
    reconnect_attempt: 0,
    next_retry_at: null,
    last_connected_at: null,
    last_error: null,
    launches_received: 0,
    last_launch_at: null,
    server_pending_count: null,
    heartbeat_interval_ms: HEARTBEAT_INTERVAL_MS,
  };

  var socket = null;
  var stopped = true;
  var generation = 0;
  var reconnectTimer = null;
  var authTimer = null;
  var heartbeatTimer = null;
  var heartbeatIntervalMs = HEARTBEAT_INTERVAL_MS;
  var heartbeatTimeoutMs = HEARTBEAT_TIMEOUT_MS;
  var lastInboundAt = 0;
  var lastHeartbeatTickAt = 0;
  var outstandingProbeAt = 0;

  function getState() {
    var copy = {};
    Object.keys(state).forEach(function (key) {
      copy[key] = state[key];
    });
    return copy;
  }

  function emit() {
    try {
      onStateChange(getState());
    } catch (e) {
      // observers must never break the link
    }
  }

  function clearTimers() {
    if (reconnectTimer) {
      clearTimeoutFn(reconnectTimer);
      reconnectTimer = null;
    }
    if (authTimer) {
      clearTimeoutFn(authTimer);
      authTimer = null;
    }
    if (heartbeatTimer) {
      clearIntervalFn(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function safeSend(payload) {
    if (!socket || socket.readyState !== 1) {
      return false;
    }
    try {
      socket.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      state.last_error = "send failed: " + String((err && err.message) || err);
      return false;
    }
  }

  function buildAuthFrame(settings) {
    return {
      type: "auth",
      token: String(settings.link_token || "").trim(),
      panel_build_id: panelBuildId,
      port: getPort(),
    };
  }

  function scheduleReconnect(closeCode) {
    if (stopped || reconnectTimer) {
      return;
    }
    var delay =
      closeCode === CLOSE_CODES.AUTH_FAILED
        ? RECONNECT_MAX_MS
        : computeBackoffMs(state.reconnect_attempt);
    state.reconnect_attempt += 1;
    state.next_retry_at = new Date(now().getTime() + delay).toISOString();
    reconnectTimer = setTimeoutFn(function () {
      reconnectTimer = null;
      connect();
    }, delay);
    emit();
  }

  function startHeartbeat() {
    stopHeartbeat();
    var startedAt = now().getTime();
    lastInboundAt = startedAt;
    lastHeartbeatTickAt = startedAt;
    outstandingProbeAt = 0;
    heartbeatTimer = setIntervalFn(function () {
      if (!socket || socket.readyState !== 1) {
        return;
      }
      var currentTime = now().getTime();
      var tickDelay = Math.max(0, currentTime - lastHeartbeatTickAt);
      lastHeartbeatTickAt = currentTime;

      if (
        outstandingProbeAt > 0 &&
        currentTime - outstandingProbeAt >= heartbeatTimeoutMs &&
        lastInboundAt < outstandingProbeAt
      ) {
        // CEP's renderer can be delayed by a long Premiere evalScript call or
        // local filesystem pressure. When our own interval was starved, the
        // old probe's deadline is not evidence of a dead connection: grant a
        // fresh probe and require that one to fail before reconnecting.
        if (tickDelay >= heartbeatIntervalMs * 1.5) {
          outstandingProbeAt = 0;
        } else {
          log("Premiere Link heartbeat timed out, reconnecting", "warn");
          state.last_error = "heartbeat timeout";
          try {
            socket.close(CLOSE_CODES.CLIENT_HEARTBEAT, "heartbeat timeout");
          } catch (e) {
            // onclose still fires
          }
          return;
        }
      }

      if (
        outstandingProbeAt <= 0 &&
        safeSend({ type: "ping", ts: new Date(currentTime).toISOString() })
      ) {
        outstandingProbeAt = currentTime;
      }
    }, heartbeatIntervalMs);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearIntervalFn(heartbeatTimer);
      heartbeatTimer = null;
    }
    outstandingProbeAt = 0;
  }

  function markInboundActivity() {
    lastInboundAt = now().getTime();
    outstandingProbeAt = 0;
  }

  function handleLaunch(frame) {
    state.launches_received += 1;
    state.last_launch_at = now().toISOString();
    var outcome;
    try {
      outcome =
        onLaunch({
          launch_id: frame.launch_id,
          project_id: frame.project_id,
          anime_title: frame.anime_title,
          requested_at: frame.requested_at,
          replay: !!frame.replay,
        }) || {};
    } catch (err) {
      outcome = { result: "error", detail: String((err && err.message) || err) };
    }
    var result =
      outcome.result === "accepted" || outcome.result === "duplicate"
        ? outcome.result
        : "error";
    safeSend({
      type: "ack",
      launch_id: frame.launch_id,
      project_id: frame.project_id,
      result: result,
      detail: outcome.detail == null ? null : String(outcome.detail),
      status: outcome.status || null,
      queue_state: outcome.queue_state || null,
      batch_phase: outcome.batch_phase || null,
    });
    emit();
  }

  function handleMessage(raw) {
    var frame;
    try {
      frame = JSON.parse(String(raw == null ? "" : raw));
    } catch (e) {
      return;
    }
    if (!frame || typeof frame !== "object") {
      return;
    }
    // Any valid protocol frame proves the connection is alive. A launch or
    // server ping is as authoritative as a pong for heartbeat purposes.
    markInboundActivity();
    switch (frame.type) {
      case "auth_ok":
        if (authTimer) {
          clearTimeoutFn(authTimer);
          authTimer = null;
        }
        state.connected = true;
        state.authenticated = true;
        state.connecting = false;
        state.reconnect_attempt = 0;
        state.next_retry_at = null;
        state.last_error = null;
        state.last_connected_at = now().toISOString();
        state.server_pending_count = Number(frame.pending_count || 0);
        heartbeatIntervalMs = normalizeHeartbeatIntervalMs(
          frame.heartbeat_interval_s,
        );
        heartbeatTimeoutMs = heartbeatIntervalMs * 2;
        state.heartbeat_interval_ms = heartbeatIntervalMs;
        startHeartbeat();
        log(
          "Premiere Link connected" +
            (state.server_pending_count > 0
              ? " (" + state.server_pending_count + " pending launch(es))"
              : ""),
          "success",
        );
        emit();
        break;
      case "ping":
        safeSend({ type: "pong", ts: frame.ts });
        break;
      case "pong":
        break;
      case "launch":
        handleLaunch(frame);
        break;
      case "error":
        log(
          "Premiere Link server error: " +
            String(frame.code || "unknown") +
            (frame.detail ? " — " + frame.detail : ""),
          "warn",
        );
        break;
      default:
        break;
    }
  }

  function connect() {
    if (stopped) {
      return;
    }
    var settings = getSettings() || {};
    if (!isEnabled(settings)) {
      state.enabled = false;
      emit();
      return;
    }
    var url = normalizeLinkUrl(settings.link_url);
    state.enabled = true;
    state.url = url;
    if (!isValidLinkUrl(url)) {
      state.last_error = "invalid Premiere Link URL (expected ws:// or wss://): " + url;
      log(state.last_error, "error");
      emit();
      return;
    }
    if (!WebSocketImpl) {
      state.last_error = "WebSocket is not available in this panel runtime";
      log(state.last_error, "error");
      emit();
      return;
    }

    var gen = ++generation;
    state.connecting = true;
    emit();

    var ws;
    try {
      ws = new WebSocketImpl(url);
    } catch (err) {
      state.connecting = false;
      state.last_error = String((err && err.message) || err);
      log("Premiere Link connect failed: " + state.last_error, "error");
      scheduleReconnect(0);
      return;
    }
    socket = ws;

    ws.onopen = function () {
      if (gen !== generation) {
        return;
      }
      safeSend(buildAuthFrame(settings));
      authTimer = setTimeoutFn(function () {
        authTimer = null;
        if (gen !== generation || state.authenticated) {
          return;
        }
        state.last_error = "auth timeout";
        log("Premiere Link auth timed out", "warn");
        try {
          ws.close(CLOSE_CODES.CLIENT_HEARTBEAT, "auth timeout");
        } catch (e) {
          // onclose still fires
        }
      }, AUTH_TIMEOUT_MS);
    };
    ws.onmessage = function (event) {
      if (gen !== generation) {
        return;
      }
      handleMessage(event && event.data);
    };
    ws.onerror = function () {
      if (gen !== generation) {
        return;
      }
      if (!state.last_error) {
        state.last_error = "socket error";
      }
    };
    ws.onclose = function (event) {
      if (gen !== generation) {
        return;
      }
      var code = event ? Number(event.code || 0) : 0;
      var reason = event && event.reason ? String(event.reason) : "";
      var wasAuthenticated = state.authenticated;
      clearTimers();
      socket = null;
      state.connected = false;
      state.authenticated = false;
      state.connecting = false;
      if (code === CLOSE_CODES.AUTH_FAILED) {
        state.last_error = "rejected by the VPS: check the Premiere Link token";
        log("Premiere Link rejected (" + describeClose(code, reason) + ")", "error");
      } else if (!stopped) {
        if (!state.last_error) {
          state.last_error = describeClose(code, reason);
        }
        log(
          "Premiere Link disconnected (" + describeClose(code, reason) + ")",
          wasAuthenticated ? "warn" : "info",
        );
      }
      emit();
      scheduleReconnect(code);
    };
  }

  function start() {
    stopped = false;
    if (!isEnabled(getSettings())) {
      state.enabled = false;
      log("Premiere Link disabled (URL or token not set)", "info");
      emit();
      return;
    }
    if (socket || reconnectTimer) {
      return;
    }
    state.reconnect_attempt = 0;
    connect();
  }

  function stop(reason) {
    stopped = true;
    generation += 1;
    clearTimers();
    var ws = socket;
    socket = null;
    if (ws) {
      try {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close(CLOSE_CODES.CLIENT_STOP, reason || "stop");
      } catch (e) {
        // already closed
      }
    }
    state.connected = false;
    state.authenticated = false;
    state.connecting = false;
    state.next_retry_at = null;
    emit();
  }

  function restart() {
    stop("restart");
    start();
  }

  function testOnce(timeoutMs) {
    return new Promise(function (resolve, reject) {
      var settings = getSettings() || {};
      if (!isEnabled(settings)) {
        reject(new Error("Premiere Link URL or token not set"));
        return;
      }
      var url = normalizeLinkUrl(settings.link_url);
      if (!isValidLinkUrl(url)) {
        reject(new Error("invalid Premiere Link URL: " + url));
        return;
      }
      if (!WebSocketImpl) {
        reject(new Error("WebSocket is not available in this panel runtime"));
        return;
      }
      var ws;
      try {
        ws = new WebSocketImpl(url);
      } catch (err) {
        reject(err);
        return;
      }
      var done = false;
      var limit = Number(timeoutMs) > 0 ? Number(timeoutMs) : TEST_TIMEOUT_MS;
      var timer = setTimeoutFn(function () {
        finish(new Error("no answer after " + limit + "ms"));
      }, limit);

      function finish(err, ok) {
        if (done) {
          return;
        }
        done = true;
        clearTimeoutFn(timer);
        try {
          ws.onclose = null;
          ws.close(CLOSE_CODES.CLIENT_STOP, "test done");
        } catch (e) {
          // ignore
        }
        if (err) {
          reject(err);
        } else {
          resolve(ok);
        }
      }

      ws.onopen = function () {
        try {
          ws.send(JSON.stringify(buildAuthFrame(settings)));
        } catch (err) {
          finish(err);
        }
      };
      ws.onmessage = function (event) {
        var frame;
        try {
          frame = JSON.parse(String(event && event.data));
        } catch (e) {
          return;
        }
        if (frame && frame.type === "auth_ok") {
          finish(null, {
            pending_count: Number(frame.pending_count || 0),
            server_time: frame.server_time || null,
          });
        }
      };
      ws.onclose = function (event) {
        var code = event ? Number(event.code || 0) : 0;
        finish(
          new Error(
            code === CLOSE_CODES.AUTH_FAILED
              ? "rejected: check the Premiere Link token (4401)"
              : "closed before auth (" + describeClose(code, event && event.reason) + ")",
          ),
        );
      };
      ws.onerror = function () {
        // onclose follows with the code
      };
    });
  }

  return {
    start: start,
    stop: stop,
    restart: restart,
    getState: getState,
    testOnce: testOnce,
    isEnabled: function () {
      return isEnabled(getSettings());
    },
  };
}

module.exports = {
  AUTH_TIMEOUT_MS: AUTH_TIMEOUT_MS,
  CLOSE_CODES: CLOSE_CODES,
  DEFAULT_LINK_URL: DEFAULT_LINK_URL,
  HEARTBEAT_INTERVAL_MS: HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_TIMEOUT_MS: HEARTBEAT_TIMEOUT_MS,
  RECONNECT_MAX_MS: RECONNECT_MAX_MS,
  RECONNECT_MIN_MS: RECONNECT_MIN_MS,
  computeBackoffMs: computeBackoffMs,
  createLinkClient: createLinkClient,
  isEnabled: isEnabled,
  isValidLinkUrl: isValidLinkUrl,
  normalizeLinkUrl: normalizeLinkUrl,
  normalizeHeartbeatIntervalMs: normalizeHeartbeatIntervalMs,
};
