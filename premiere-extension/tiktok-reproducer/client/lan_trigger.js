"use strict";

/**
 * Small, side-effect-free helpers for the inbound LAN trigger server.
 *
 * LAN transfers and LAN triggers intentionally share the existing
 * X-ATR-LAN-Token secret. The HTTP server is exposed beyond loopback only
 * when the LAN transfer settings are complete.
 */

var crypto = require("crypto");

var API_VERSION = 1;
var TOKEN_HEADER = "x-atr-lan-token";

function isEnabled(settings) {
  return !!(
    settings &&
    String(settings.lan_base_url || "").trim() &&
    String(settings.lan_token || "").trim()
  );
}

function getListenHost(settings) {
  return isEnabled(settings) ? "0.0.0.0" : "127.0.0.1";
}

function isLoopbackAddress(address) {
  var normalized = String(address || "").trim().toLowerCase();
  return (
    normalized === "::1" ||
    normalized.indexOf("127.") === 0 ||
    normalized.indexOf("::ffff:127.") === 0
  );
}

function readToken(req) {
  var headers = (req && req.headers) || {};
  var value = headers[TOKEN_HEADER];
  if (Array.isArray(value)) {
    value = value[0];
  }
  return String(value || "");
}

function tokensMatch(actual, expected) {
  var actualBuffer = Buffer.from(String(actual || ""), "utf8");
  var expectedBuffer = Buffer.from(String(expected || ""), "utf8");
  if (expectedBuffer.length === 0 || actualBuffer.length !== expectedBuffer.length) {
    return false;
  }
  return crypto.timingSafeEqual(actualBuffer, expectedBuffer);
}

function isAuthorized(req, settings) {
  return (
    isEnabled(settings) &&
    tokensMatch(readToken(req), String(settings.lan_token || ""))
  );
}

function matchRoute(method, pathname) {
  var normalizedMethod = String(method || "GET").toUpperCase();
  var normalizedPath = String(pathname || "");

  if (normalizedMethod === "GET" && normalizedPath === "/api/lan/ping") {
    return { type: "ping" };
  }

  if (normalizedMethod === "POST") {
    var startMatch = normalizedPath.match(
      /^\/api\/lan\/projects\/([A-Za-z0-9_-]+)\/start$/,
    );
    if (startMatch) {
      return {
        type: "start_project",
        project_id: startMatch[1],
      };
    }
  }

  return null;
}

module.exports = {
  API_VERSION: API_VERSION,
  TOKEN_HEADER: TOKEN_HEADER,
  getListenHost: getListenHost,
  isAuthorized: isAuthorized,
  isEnabled: isEnabled,
  isLoopbackAddress: isLoopbackAddress,
  matchRoute: matchRoute,
  tokensMatch: tokensMatch,
};
