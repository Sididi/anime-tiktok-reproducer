"use strict";

var fs = require("fs");
var path = require("path");
var os = require("os");
var https = require("https");
var zlib = require("zlib");
var querystring = require("querystring");

var DRIVE_API_HOST = "www.googleapis.com";
var OAUTH_HOST = "oauth2.googleapis.com";
var FOLDER_MIME = "application/vnd.google-apps.folder";
var OUTPUT_FILENAME = "output.mp4";
var RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024;
var MAX_RETRIES = 6;
var DOWNLOAD_CONCURRENCY_DEFAULT = 4;
var DOWNLOAD_CONCURRENCY_MAX = 8;
var SMALL_FILE_BYTES = 12 * 1024 * 1024;
var SMALL_FILE_HIGH_COUNT = 120;
var SMALL_FILE_MEDIUM_COUNT = 40;
var PROGRESS_EMIT_INTERVAL_MS = 250;
var PROGRESS_EMIT_MIN_DELTA_BYTES = 4 * 1024 * 1024;
var TREE_LIST_CONCURRENCY = 6;
var SHARED_HTTPS_AGENT = new https.Agent({
  keepAlive: true,
  maxSockets: 32,
  maxFreeSockets: 16,
});

function nowIso() {
  return new Date().toISOString();
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
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
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
  fs.renameSync(tmp, filePath);
}

function removeIfExists(filePath) {
  try {
    if (fs.existsSync(filePath)) {
      fs.rmSync(filePath, { recursive: true, force: true });
    }
  } catch (e) {
    // best effort
  }
}

function sleep(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

function isRetryableStatus(statusCode) {
  return statusCode === 429 || (statusCode >= 500 && statusCode < 600);
}

function escapeQueryValue(value) {
  return String(value || "")
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'");
}

function sanitizeWindowsSegment(name) {
  var cleaned = String(name || "").replace(/[<>:\"/\\|?*]/g, "_");
  cleaned = cleaned.replace(/[.\s]+$/g, "");
  if (!cleaned) {
    cleaned = "unnamed";
  }
  var reserved = {
    CON: true,
    PRN: true,
    AUX: true,
    NUL: true,
    COM1: true,
    COM2: true,
    COM3: true,
    COM4: true,
    COM5: true,
    COM6: true,
    COM7: true,
    COM8: true,
    COM9: true,
    LPT1: true,
    LPT2: true,
    LPT3: true,
    LPT4: true,
    LPT5: true,
    LPT6: true,
    LPT7: true,
    LPT8: true,
    LPT9: true,
  };
  if (reserved[cleaned.toUpperCase()]) {
    cleaned = "_" + cleaned;
  }
  return cleaned;
}

function decodeBody(buffer, contentType) {
  if (!buffer || buffer.length === 0) {
    return null;
  }
  var mime = String(contentType || "").toLowerCase();
  if (mime.indexOf("application/json") !== -1) {
    try {
      return JSON.parse(buffer.toString("utf8"));
    } catch (e) {
      return buffer.toString("utf8");
    }
  }
  return buffer.toString("utf8");
}

function normalizeHeaderValue(value) {
  if (Array.isArray(value)) {
    return String(value[0] || "");
  }
  return String(value || "");
}

function decompressIfNeeded(buffer, contentEncoding) {
  var encoding = normalizeHeaderValue(contentEncoding).toLowerCase();
  if (!buffer || buffer.length === 0 || !encoding || encoding === "identity") {
    return buffer;
  }
  try {
    if (encoding.indexOf("gzip") !== -1) {
      return zlib.gunzipSync(buffer);
    }
    if (encoding.indexOf("deflate") !== -1) {
      return zlib.inflateSync(buffer);
    }
    if (
      encoding.indexOf("br") !== -1 &&
      typeof zlib.brotliDecompressSync === "function"
    ) {
      return zlib.brotliDecompressSync(buffer);
    }
  } catch (e) {
    throw new Error(
      "Failed to decode compressed HTTP response (" +
        encoding +
        "): " +
        e.message,
    );
  }
  return buffer;
}

function requestRaw(options, bodyBuffer, timeoutMs) {
  options = options || {};
  if (typeof options.agent === "undefined") {
    options.agent = SHARED_HTTPS_AGENT;
  }
  return new Promise(function (resolve, reject) {
    var done = false;
    var req = https.request(options, function (res) {
      var chunks = [];
      res.on("data", function (chunk) {
        chunks.push(chunk);
      });
      res.on("end", function () {
        if (done) {
          return;
        }
        done = true;
        var rawBody = Buffer.concat(chunks);
        var decodedBody = rawBody;
        try {
          decodedBody = decompressIfNeeded(
            rawBody,
            res.headers["content-encoding"],
          );
        } catch (decodeErr) {
          reject(decodeErr);
          return;
        }
        resolve({
          statusCode: res.statusCode || 0,
          headers: res.headers || {},
          body: decodedBody,
        });
      });
    });

    req.on("error", function (err) {
      if (done) {
        return;
      }
      done = true;
      reject(err);
    });

    req.setTimeout(timeoutMs || 120000, function () {
      req.destroy(new Error("Request timeout"));
    });

    if (bodyBuffer && bodyBuffer.length) {
      req.write(bodyBuffer);
    }
    req.end();
  });
}

function requestJson(options, bodyObj, timeoutMs) {
  var payload = null;
  if (bodyObj !== undefined && bodyObj !== null) {
    payload = Buffer.from(JSON.stringify(bodyObj), "utf8");
    options.headers = options.headers || {};
    if (!options.headers["Content-Type"]) {
      options.headers["Content-Type"] = "application/json; charset=utf-8";
    }
    options.headers["Content-Length"] = payload.length;
  }
  return requestRaw(options, payload, timeoutMs).then(function (result) {
    result.parsedBody = decodeBody(result.body, result.headers["content-type"]);
    return result;
  });
}

function createDriveAuth(settings) {
  var tokenCache = {
    accessToken: null,
    expiryMs: 0,
  };

  function refreshToken() {
    var payload = querystring.stringify({
      client_id: settings.client_id,
      client_secret: settings.client_secret,
      refresh_token: settings.refresh_token,
      grant_type: "refresh_token",
    });

    return requestRaw(
      {
        method: "POST",
        host: OAUTH_HOST,
        path: "/token",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "Content-Length": Buffer.byteLength(payload),
          "Accept-Encoding": "gzip",
        },
      },
      Buffer.from(payload, "utf8"),
      60000,
    ).then(function (res) {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        throw new Error(
          "OAuth refresh failed (" +
            res.statusCode +
            "): " +
            res.body.toString("utf8"),
        );
      }
      var data = JSON.parse(res.body.toString("utf8"));
      var expiresIn = Number(data.expires_in || 3600);
      tokenCache.accessToken = data.access_token;
      tokenCache.expiryMs = Date.now() + Math.max(60, expiresIn - 300) * 1000;
      return tokenCache.accessToken;
    });
  }

  function getAccessToken(forceRefresh) {
    if (
      !forceRefresh &&
      tokenCache.accessToken &&
      Date.now() < tokenCache.expiryMs
    ) {
      return Promise.resolve(tokenCache.accessToken);
    }
    return refreshToken();
  }

  return {
    getAccessToken: getAccessToken,
  };
}

function driveApiRequest(
  auth,
  method,
  apiPath,
  query,
  body,
  extraHeaders,
  allowRetry,
) {
  var qs = query ? querystring.stringify(query) : "";
  var fullPath = apiPath + (qs ? "?" + qs : "");
  var attempt = 0;

  function run(forceRefreshToken) {
    return auth.getAccessToken(!!forceRefreshToken).then(function (token) {
      var headers = {
        Authorization: "Bearer " + token,
        "Accept-Encoding": "gzip",
      };
      if (extraHeaders) {
        Object.keys(extraHeaders).forEach(function (key) {
          headers[key] = extraHeaders[key];
        });
      }
      return requestJson(
        {
          method: method,
          host: DRIVE_API_HOST,
          path: fullPath,
          headers: headers,
        },
        body,
        120000,
      ).then(function (res) {
        if (res.statusCode === 401 && !forceRefreshToken) {
          return run(true);
        }
        if (
          allowRetry &&
          isRetryableStatus(res.statusCode) &&
          attempt < MAX_RETRIES
        ) {
          attempt += 1;
          return sleep(Math.pow(2, attempt) * 250).then(function () {
            return run(false);
          });
        }
        if (res.statusCode < 200 || res.statusCode >= 300) {
          throw new Error(
            "Drive API error " +
              res.statusCode +
              " on " +
              apiPath +
              ": " +
              String(res.parsedBody || ""),
          );
        }
        return res.parsedBody;
      });
    });
  }

  return run(false);
}

function driveUploadRequest(
  auth,
  method,
  uploadUrl,
  headers,
  bodyBuffer,
  allowRetry,
) {
  var urlMatch = uploadUrl.match(/^https:\/\/([^/]+)(\/.*)$/i);
  if (!urlMatch) {
    return Promise.reject(new Error("Invalid upload URL"));
  }
  var host = urlMatch[1];
  var reqPath = urlMatch[2];

  var attempt = 0;

  function run(forceRefreshToken) {
    return auth.getAccessToken(!!forceRefreshToken).then(function (token) {
      var allHeaders = {
        Authorization: "Bearer " + token,
      };
      Object.keys(headers || {}).forEach(function (key) {
        allHeaders[key] = headers[key];
      });

      return requestRaw(
        {
          method: method,
          host: host,
          path: reqPath,
          headers: allHeaders,
        },
        bodyBuffer,
        240000,
      ).then(function (res) {
        if (res.statusCode === 401 && !forceRefreshToken) {
          return run(true);
        }
        if (
          allowRetry &&
          isRetryableStatus(res.statusCode) &&
          attempt < MAX_RETRIES
        ) {
          attempt += 1;
          return sleep(Math.pow(2, attempt) * 250).then(function () {
            return run(false);
          });
        }
        return res;
      });
    });
  }

  return run(false);
}

function listDriveFiles(auth, query, fields) {
  var requestedFields = String(
    fields || "nextPageToken,files(id,name,mimeType,size)",
  );
  var all = [];

  function fetchPage(pageToken) {
    return driveApiRequest(
      auth,
      "GET",
      "/drive/v3/files",
      {
        q: query,
        fields: requestedFields,
        pageSize: 1000,
        pageToken: pageToken || undefined,
        supportsAllDrives: true,
        includeItemsFromAllDrives: true,
      },
      null,
      null,
      true,
    ).then(function (payload) {
      var files = payload.files || [];
      all = all.concat(files);
      if (payload.nextPageToken) {
        return fetchPage(payload.nextPageToken);
      }
      return all;
    });
  }

  return fetchPage(null);
}

function resolveProjectFolder(auth, parentFolderId, projectId) {
  var q = [
    "mimeType='" + FOLDER_MIME + "'",
    "trashed=false",
    "'" + escapeQueryValue(parentFolderId) + "' in parents",
    "name contains '" + escapeQueryValue(projectId) + "'",
  ].join(" and ");

  var expectedRe = new RegExp(
    "^SPM_.+_" + projectId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$",
    "i",
  );

  return listDriveFiles(auth, q, "nextPageToken,files(id,name,mimeType)").then(
    function (folders) {
      var candidates = folders.filter(function (folder) {
        var name = String(folder.name || "");
        return expectedRe.test(name);
      });

      if (candidates.length === 0) {
        throw new Error(
          "No Drive folder matching SPM_*_" +
            projectId +
            " under configured parent",
        );
      }
      if (candidates.length > 1) {
        throw new Error(
          "Multiple Drive folders match project " +
            projectId +
            ": " +
            candidates
              .map(function (item) {
                return item.name;
              })
              .join(", "),
        );
      }
      return candidates[0];
    },
  );
}

function walkDriveTree(auth, folderId, relativeDir, outFiles) {
  var q = [
    "trashed=false",
    "'" + escapeQueryValue(folderId) + "' in parents",
  ].join(" and ");

  return listDriveFiles(
    auth,
    q,
    "nextPageToken,files(id,name,mimeType,size)",
  ).then(function (items) {
    var childFolders = [];
    items.forEach(function (item) {
      var safeName = sanitizeWindowsSegment(item.name || "");
      var relPath = relativeDir ? path.join(relativeDir, safeName) : safeName;
      if (item.mimeType === FOLDER_MIME) {
        childFolders.push({
          id: item.id,
          relativePath: relPath,
        });
        return;
      }
      outFiles.push({
        id: item.id,
        name: item.name,
        relativePath: relPath,
        size: Number(item.size || 0),
        mimeType: item.mimeType,
      });
    });

    return runWithConcurrency(
      childFolders,
      TREE_LIST_CONCURRENCY,
      function (child) {
        return walkDriveTree(auth, child.id, child.relativePath, outFiles);
      },
    );
  });
}

function downloadFileWithResume(
  auth,
  fileId,
  destinationPath,
  expectedSize,
  onProgress,
) {
  ensureDir(path.dirname(destinationPath));
  var partPath = destinationPath + ".part";
  var size = Number(expectedSize || 0);

  if (fs.existsSync(destinationPath)) {
    if (!size || fs.statSync(destinationPath).size === size) {
      return Promise.resolve({
        bytes: fs.statSync(destinationPath).size,
        reused: true,
      });
    }
    fs.unlinkSync(destinationPath);
  }

  if (fs.existsSync(partPath) && size && fs.statSync(partPath).size > size) {
    fs.unlinkSync(partPath);
  }

  function attemptDownload(startOffset, attempt) {
    return auth.getAccessToken(false).then(function (token) {
      return new Promise(function (resolve, reject) {
        var reqHeaders = {
          Authorization: "Bearer " + token,
        };
        if (startOffset > 0) {
          reqHeaders.Range = "bytes=" + startOffset + "-";
        }

        var req = https.request(
          {
            method: "GET",
            host: DRIVE_API_HOST,
            path:
              "/drive/v3/files/" +
              encodeURIComponent(fileId) +
              "?alt=media&supportsAllDrives=true",
            headers: reqHeaders,
            agent: SHARED_HTTPS_AGENT,
          },
          function (res) {
            if (res.statusCode === 401) {
              res.resume();
              auth
                .getAccessToken(true)
                .then(function () {
                  resolve(attemptDownload(startOffset, attempt + 1));
                })
                .catch(reject);
              return;
            }

            if (res.statusCode === 416) {
              res.resume();
              try {
                if (fs.existsSync(partPath)) {
                  fs.unlinkSync(partPath);
                }
              } catch (e) {
                // ignore
              }
              resolve(attemptDownload(0, attempt + 1));
              return;
            }

            if (!(res.statusCode === 200 || res.statusCode === 206)) {
              var chunks = [];
              res.on("data", function (chunk) {
                chunks.push(chunk);
              });
              res.on("end", function () {
                var body = Buffer.concat(chunks).toString("utf8");
                if (
                  attempt < MAX_RETRIES &&
                  isRetryableStatus(res.statusCode)
                ) {
                  resolve(
                    sleep(Math.pow(2, attempt) * 250).then(function () {
                      return attemptDownload(startOffset, attempt + 1);
                    }),
                  );
                  return;
                }
                reject(
                  new Error(
                    "Download failed (" + res.statusCode + "): " + body,
                  ),
                );
              });
              return;
            }

            var wrote = startOffset;
            var stream = fs.createWriteStream(partPath, {
              flags: startOffset > 0 ? "a" : "w",
            });

            var streamErrored = false;

            stream.on("error", function (err) {
              streamErrored = true;
              req.destroy(err);
            });

            res.on("data", function (chunk) {
              wrote += chunk.length;
              if (onProgress) {
                onProgress({
                  kind: "file",
                  file_id: fileId,
                  destination: destinationPath,
                  downloaded_bytes: wrote,
                  total_bytes: size,
                });
              }
            });

            res.on("error", function (err) {
              stream.destroy(err);
            });

            stream.on("finish", function () {
              if (streamErrored) {
                return;
              }
              if (size > 0 && wrote < size) {
                resolve(attemptDownload(wrote, attempt + 1));
                return;
              }
              try {
                fs.renameSync(partPath, destinationPath);
              } catch (e) {
                reject(e);
                return;
              }
              resolve({ bytes: wrote, reused: false });
            });

            res.pipe(stream);
          },
        );

        req.on("error", function (err) {
          if (attempt < MAX_RETRIES) {
            resolve(
              sleep(Math.pow(2, attempt) * 250).then(function () {
                return attemptDownload(startOffset, attempt + 1);
              }),
            );
            return;
          }
          reject(err);
        });

        req.setTimeout(180000, function () {
          req.destroy(new Error("Download timeout"));
        });

        req.end();
      });
    });
  }

  var existing = 0;
  if (fs.existsSync(partPath)) {
    existing = fs.statSync(partPath).size;
  }
  return attemptDownload(existing, 0);
}

function pickTargetBasePaths(folderName, appDataPath) {
  var home = process.env.USERPROFILE || os.homedir();
  var desktopParent = path.join(home, "Desktop");
  var fallbackParent = path.join(
    appDataPath || path.join(home, "AppData", "Roaming"),
    "Adobe",
    "TiktokReproducer",
    "downloads",
  );

  try {
    ensureDir(desktopParent);
    fs.accessSync(desktopParent, fs.constants.W_OK);
    return {
      parent: desktopParent,
      folderName: folderName,
      isFallback: false,
    };
  } catch (e) {
    ensureDir(fallbackParent);
    return {
      parent: fallbackParent,
      folderName: folderName,
      isFallback: true,
    };
  }
}

function runWithConcurrency(items, concurrency, workerFn) {
  var list = Array.isArray(items) ? items : [];
  var limit = Math.max(1, Number(concurrency || 1));
  if (list.length === 0) {
    return Promise.resolve();
  }

  return new Promise(function (resolve, reject) {
    var nextIndex = 0;
    var active = 0;
    var completed = 0;
    var failed = false;

    function launch() {
      if (failed) {
        return;
      }

      while (active < limit && nextIndex < list.length) {
        (function (index) {
          active += 1;
          Promise.resolve()
            .then(function () {
              return workerFn(list[index], index);
            })
            .then(function () {
              active -= 1;
              completed += 1;
              if (completed >= list.length) {
                resolve();
                return;
              }
              launch();
            })
            .catch(function (err) {
              if (failed) {
                return;
              }
              failed = true;
              reject(err);
            });
        })(nextIndex);
        nextIndex += 1;
      }
    }

    launch();
  });
}

function parseDownloadConcurrencyOverride(rawValue) {
  if (rawValue === undefined || rawValue === null || rawValue === "") {
    return 0;
  }
  var parsed = Number(rawValue);
  if (!parsed || !isFinite(parsed)) {
    return 0;
  }
  return Math.max(1, Math.min(DOWNLOAD_CONCURRENCY_MAX, Math.floor(parsed)));
}

function pickDownloadConcurrency(settings, files) {
  var override =
    parseDownloadConcurrencyOverride(
      settings && settings.download_concurrency,
    ) ||
    parseDownloadConcurrencyOverride(
      process.env.JSXRUNNER_DOWNLOAD_CONCURRENCY,
    );
  if (override > 0) {
    return override;
  }

  var list = Array.isArray(files) ? files : [];
  if (list.length === 0) {
    return DOWNLOAD_CONCURRENCY_DEFAULT;
  }

  var smallFiles = 0;
  for (var i = 0; i < list.length; i += 1) {
    if (Number(list[i].size || 0) <= SMALL_FILE_BYTES) {
      smallFiles += 1;
    }
  }

  var ratioSmall = smallFiles / list.length;
  var selected = DOWNLOAD_CONCURRENCY_DEFAULT;

  if (list.length >= SMALL_FILE_HIGH_COUNT && ratioSmall >= 0.8) {
    selected = 8;
  } else if (list.length >= SMALL_FILE_MEDIUM_COUNT && ratioSmall >= 0.6) {
    selected = 6;
  }

  return Math.max(1, Math.min(DOWNLOAD_CONCURRENCY_MAX, selected));
}

function performDownloadProject(payload, emitProgress) {
  var settings = payload.settings || {};
  var projectId = payload.project_id;
  var auth = createDriveAuth(settings);

  if (!projectId) {
    return Promise.reject(new Error("Missing project_id"));
  }
  if (!settings.parent_folder_id) {
    return Promise.reject(new Error("Missing parent_folder_id in settings"));
  }

  emitProgress({ stage: "resolve_folder", project_id: projectId });

  return resolveProjectFolder(auth, settings.parent_folder_id, projectId).then(
    function (folder) {
      var files = [];
      emitProgress({
        stage: "list_tree",
        project_id: projectId,
        folder_id: folder.id,
        folder_name: folder.name,
      });

      return walkDriveTree(auth, folder.id, "", files).then(function () {
        var target = pickTargetBasePaths(
          sanitizeWindowsSegment(folder.name || "project_" + projectId),
          payload.app_data_path,
        );
        var targetRoot = path.join(target.parent, target.folderName);
        var partialRoot = targetRoot + ".partial";

        ensureDir(partialRoot);

        var totalBytes = 0;
        files.forEach(function (file) {
          totalBytes += Number(file.size || 0);
        });

        var downloadConcurrency = pickDownloadConcurrency(settings, files);
        var globalDownloaded = 0;
        var activeProgressByFileId = {};
        var activeProgressTotal = 0;
        var lastProgressEmitAt = 0;
        var lastProgressBytes = 0;
        var downloadStartedAt = Date.now();
        emitProgress({
          stage: "download_tuning",
          project_id: projectId,
          file_count: files.length,
          selected_concurrency: downloadConcurrency,
        });
        emitProgress({
          stage: "download_start",
          project_id: projectId,
          file_count: files.length,
          total_bytes: totalBytes,
          target_root: targetRoot,
        });

        return runWithConcurrency(
          files,
          downloadConcurrency,
          function (file, index) {
            var destination = path.join(partialRoot, file.relativePath);
            return downloadFileWithResume(
              auth,
              file.id,
              destination,
              file.size,
              function (fileProgress) {
                var nextBytes = Number(fileProgress.downloaded_bytes || 0);
                var previousBytes = Number(
                  activeProgressByFileId[file.id] || 0,
                );
                if (nextBytes < previousBytes) {
                  nextBytes = previousBytes;
                }
                activeProgressByFileId[file.id] = nextBytes;
                activeProgressTotal += nextBytes - previousBytes;

                var estimateBytes = globalDownloaded + activeProgressTotal;
                var nowMs = Date.now();
                if (
                  nowMs - lastProgressEmitAt < PROGRESS_EMIT_INTERVAL_MS &&
                  Math.abs(estimateBytes - lastProgressBytes) <
                    PROGRESS_EMIT_MIN_DELTA_BYTES
                ) {
                  return;
                }
                lastProgressEmitAt = nowMs;
                lastProgressBytes = estimateBytes;

                emitProgress({
                  stage: "download_file_progress",
                  project_id: projectId,
                  file_index: index + 1,
                  file_count: files.length,
                  relative_path: file.relativePath,
                  downloaded_bytes: fileProgress.downloaded_bytes,
                  total_bytes: fileProgress.total_bytes,
                  global_downloaded_estimate: estimateBytes,
                  global_total_bytes: totalBytes,
                });
              },
            ).then(function (result) {
              var lastTrackedBytes = Number(
                activeProgressByFileId[file.id] || 0,
              );
              if (lastTrackedBytes > 0) {
                activeProgressTotal = Math.max(
                  0,
                  activeProgressTotal - lastTrackedBytes,
                );
              }
              delete activeProgressByFileId[file.id];
              globalDownloaded += Number(result.bytes || 0);
              emitProgress({
                stage: "download_file_complete",
                project_id: projectId,
                file_index: index + 1,
                file_count: files.length,
                relative_path: file.relativePath,
                global_downloaded_bytes: globalDownloaded,
                global_total_bytes: totalBytes,
              });
            });
          },
        ).then(function () {
          removeIfExists(targetRoot);
          fs.renameSync(partialRoot, targetRoot);

          var outputPath = path.join(targetRoot, OUTPUT_FILENAME);
          var contextPath = path.join(targetRoot, ".atr_project_context.json");
          var elapsedMs = Math.max(1, Date.now() - downloadStartedAt);
          var avgMbPerSec =
            totalBytes > 0
              ? totalBytes / (1024 * 1024) / (elapsedMs / 1000)
              : 0;
          writeJsonAtomic(contextPath, {
            project_id: projectId,
            drive_folder_id: folder.id,
            local_root: targetRoot,
            output_path: outputPath,
            downloaded_at: nowIso(),
            download_elapsed_ms: elapsedMs,
            download_avg_mb_per_sec: avgMbPerSec,
            download_file_count: files.length,
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
          });

          return {
            project_id: projectId,
            drive_folder_id: folder.id,
            drive_folder_name: folder.name,
            local_root: targetRoot,
            output_path: outputPath,
            used_fallback_root: !!target.isFallback,
            download_elapsed_ms: elapsedMs,
            download_avg_mb_per_sec: avgMbPerSec,
            download_file_count: files.length,
            download_total_bytes: totalBytes,
          };
        });
      });
    },
  );
}

function getExistingOutputFile(auth, folderId, outputFileName) {
  var targetOutputName = String(outputFileName || OUTPUT_FILENAME);
  var q = [
    "trashed=false",
    "name='" + escapeQueryValue(targetOutputName) + "'",
    "'" + escapeQueryValue(folderId) + "' in parents",
  ].join(" and ");

  return listDriveFiles(auth, q, "nextPageToken,files(id,name,mimeType)").then(
    function (files) {
      if (!files || files.length === 0) {
        return null;
      }
      return files[0];
    },
  );
}

function queryResumableOffset(auth, sessionUrl, fileSize) {
  return driveUploadRequest(
    auth,
    "PUT",
    sessionUrl,
    {
      "Content-Length": 0,
      "Content-Range": "bytes */" + fileSize,
    },
    null,
    true,
  ).then(function (res) {
    if (res.statusCode === 200 || res.statusCode === 201) {
      return {
        complete: true,
        offset: fileSize,
        body: decodeBody(res.body, res.headers["content-type"]),
      };
    }
    if (res.statusCode !== 308) {
      throw new Error(
        "Unexpected resumable status query response: " +
          res.statusCode +
          " " +
          res.body.toString("utf8"),
      );
    }

    var range = String(res.headers.range || "");
    if (!range) {
      return { complete: false, offset: 0 };
    }

    var m = /bytes=0-(\d+)/i.exec(range);
    if (!m) {
      return { complete: false, offset: 0 };
    }
    return { complete: false, offset: Number(m[1]) + 1 };
  });
}

function readChunkFromFile(filePath, start, endInclusive) {
  var length = endInclusive - start + 1;
  return new Promise(function (resolve, reject) {
    fs.open(filePath, "r", function (openErr, fd) {
      if (openErr) {
        reject(openErr);
        return;
      }
      var buffer = Buffer.alloc(length);
      fs.read(fd, buffer, 0, length, start, function (readErr, bytesRead) {
        fs.close(fd, function () {
          if (readErr) {
            reject(readErr);
            return;
          }
          resolve(buffer.slice(0, bytesRead));
        });
      });
    });
  });
}

function startResumableSession(
  auth,
  folderId,
  existingFileId,
  fileSize,
  outputFileName,
  uploadContentType,
) {
  var targetOutputName = String(outputFileName || OUTPUT_FILENAME);
  var targetContentType = String(
    uploadContentType || "application/octet-stream",
  );
  var method;
  var apiPath;
  var body;

  if (existingFileId) {
    method = "PATCH";
    apiPath = "/upload/drive/v3/files/" + encodeURIComponent(existingFileId);
    body = { name: targetOutputName };
  } else {
    method = "POST";
    apiPath = "/upload/drive/v3/files";
    body = {
      name: targetOutputName,
      parents: [folderId],
    };
  }

  return auth.getAccessToken(false).then(function (token) {
    var qs = querystring.stringify({
      uploadType: "resumable",
      supportsAllDrives: true,
      fields: "id,name,webViewLink",
    });

    return requestJson(
      {
        method: method,
        host: DRIVE_API_HOST,
        path: apiPath + "?" + qs,
        headers: {
          Authorization: "Bearer " + token,
          "X-Upload-Content-Type": targetContentType,
          "X-Upload-Content-Length": String(fileSize),
        },
      },
      body,
      120000,
    ).then(function (res) {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        throw new Error(
          "Failed to start resumable upload: " +
            res.statusCode +
            " " +
            res.body.toString("utf8"),
        );
      }
      var location = res.headers.location;
      if (!location) {
        throw new Error("Resumable upload session missing Location header");
      }
      return {
        upload_url: location,
        file_id:
          existingFileId || (res.parsedBody && res.parsedBody.id) || null,
      };
    });
  });
}

function finalizeUploadMetadata(auth, fileId) {
  return driveApiRequest(
    auth,
    "GET",
    "/drive/v3/files/" + encodeURIComponent(fileId),
    {
      fields: "id,name,webViewLink",
      supportsAllDrives: true,
    },
    null,
    null,
    true,
  );
}

function performResumableUpload(payload, emitProgress) {
  var settings = payload.settings || {};
  var projectId = payload.project_id;
  var outputPath = payload.output_path;
  var folderId = payload.drive_folder_id;
  var sessionFile = payload.session_state_path;

  if (!outputPath || !fs.existsSync(outputPath)) {
    return Promise.reject(new Error("Output file not found at " + outputPath));
  }
  if (!folderId) {
    return Promise.reject(new Error("Missing drive_folder_id"));
  }
  if (!sessionFile) {
    return Promise.reject(new Error("Missing session_state_path"));
  }

  var stat = fs.statSync(outputPath);
  var fileSize = stat.size;
  var fileMtimeMs = stat.mtimeMs;
  var outputFileName = String(
    payload.output_file_name || path.basename(outputPath) || OUTPUT_FILENAME,
  );
  var outputLower = outputFileName.toLowerCase();
  var outputContentType = "application/octet-stream";
  if (outputLower.slice(-4) === ".mp4") {
    outputContentType = "video/mp4";
  } else if (outputLower.slice(-4) === ".wav") {
    outputContentType = "audio/wav";
  } else if (outputLower.slice(-4) === ".m4a") {
    outputContentType = "audio/mp4";
  } else if (outputLower.slice(-4) === ".mp3") {
    outputContentType = "audio/mpeg";
  }
  var auth = createDriveAuth(settings);

  emitProgress({
    stage: "upload_prepare",
    project_id: projectId,
    output_path: outputPath,
    file_size: fileSize,
  });

  return getExistingOutputFile(auth, folderId, outputFileName)
    .then(function (existingFile) {
      var existingFileId = existingFile ? existingFile.id : null;
      var sessionState = readJson(sessionFile, null);

      if (
        !sessionState ||
        sessionState.file_size !== fileSize ||
        sessionState.file_mtime_ms !== fileMtimeMs ||
        sessionState.output_file_name !== outputFileName ||
        sessionState.drive_folder_id !== folderId ||
        sessionState.drive_file_id !== (existingFileId || null) ||
        !sessionState.upload_url
      ) {
        sessionState = null;
      }

      var sessionPromise;
      if (sessionState) {
        sessionPromise = Promise.resolve(sessionState);
        emitProgress({
          stage: "upload_resume_session",
          project_id: projectId,
          drive_file_id: sessionState.drive_file_id || null,
        });
      } else {
        sessionPromise = startResumableSession(
          auth,
          folderId,
          existingFileId,
          fileSize,
          outputFileName,
          outputContentType,
        ).then(function (created) {
          var nextState = {
            upload_url: created.upload_url,
            drive_file_id: created.file_id || existingFileId || null,
            drive_folder_id: folderId,
            output_file_name: outputFileName,
            output_content_type: outputContentType,
            file_size: fileSize,
            file_mtime_ms: fileMtimeMs,
            updated_at: nowIso(),
          };
          writeJsonAtomic(sessionFile, nextState);
          emitProgress({
            stage: "upload_new_session",
            project_id: projectId,
            drive_file_id: nextState.drive_file_id || null,
          });
          return nextState;
        });
      }

      return sessionPromise.then(function (activeSession) {
        return queryResumableOffset(
          auth,
          activeSession.upload_url,
          fileSize,
        ).then(function (offsetResult) {
          if (offsetResult.complete) {
            removeIfExists(sessionFile);
            var body = offsetResult.body || {};
            if (body.id) {
              return body;
            }
            if (!activeSession.drive_file_id) {
              throw new Error(
                "Upload already complete but Drive file ID unavailable",
              );
            }
            return finalizeUploadMetadata(auth, activeSession.drive_file_id);
          }

          var offset = Number(offsetResult.offset || 0);
          if (offset < 0) {
            offset = 0;
          }

          function uploadFrom(startOffset) {
            if (startOffset >= fileSize) {
              var fallbackId = activeSession.drive_file_id;
              if (!fallbackId) {
                throw new Error("Upload reached EOF but file ID is unknown");
              }
              return finalizeUploadMetadata(auth, fallbackId);
            }

            var chunkStart = startOffset;
            var chunkEnd =
              Math.min(chunkStart + RESUMABLE_CHUNK_SIZE, fileSize) - 1;

            return readChunkFromFile(outputPath, chunkStart, chunkEnd).then(
              function (chunkBuffer) {
                var contentRange =
                  "bytes " +
                  chunkStart +
                  "-" +
                  (chunkStart + chunkBuffer.length - 1) +
                  "/" +
                  fileSize;

                var attempts = 0;

                function sendChunk() {
                  return driveUploadRequest(
                    auth,
                    "PUT",
                    activeSession.upload_url,
                    {
                      "Content-Length": String(chunkBuffer.length),
                      "Content-Range": contentRange,
                    },
                    chunkBuffer,
                    false,
                  )
                    .then(function (res) {
                      if (res.statusCode === 308) {
                        var range = String(res.headers.range || "");
                        var nextOffset = chunkStart + chunkBuffer.length;
                        if (range) {
                          var m = /bytes=0-(\d+)/i.exec(range);
                          if (m) {
                            nextOffset = Number(m[1]) + 1;
                          }
                        }
                        activeSession.updated_at = nowIso();
                        writeJsonAtomic(sessionFile, activeSession);
                        emitProgress({
                          stage: "upload_progress",
                          project_id: projectId,
                          uploaded_bytes: nextOffset,
                          total_bytes: fileSize,
                        });
                        return uploadFrom(nextOffset);
                      }

                      if (res.statusCode === 200 || res.statusCode === 201) {
                        var body =
                          decodeBody(res.body, res.headers["content-type"]) ||
                          {};
                        if (body.id && !activeSession.drive_file_id) {
                          activeSession.drive_file_id = body.id;
                        }
                        removeIfExists(sessionFile);
                        emitProgress({
                          stage: "upload_progress",
                          project_id: projectId,
                          uploaded_bytes: fileSize,
                          total_bytes: fileSize,
                        });
                        return body;
                      }

                      if (
                        isRetryableStatus(res.statusCode) &&
                        attempts < MAX_RETRIES
                      ) {
                        attempts += 1;
                        return sleep(Math.pow(2, attempts) * 250).then(
                          sendChunk,
                        );
                      }

                      throw new Error(
                        "Upload chunk failed (" +
                          res.statusCode +
                          "): " +
                          res.body.toString("utf8"),
                      );
                    })
                    .catch(function (err) {
                      if (attempts < MAX_RETRIES) {
                        attempts += 1;
                        return sleep(Math.pow(2, attempts) * 250).then(
                          sendChunk,
                        );
                      }
                      throw err;
                    });
                }

                return sendChunk();
              },
            );
          }

          return uploadFrom(offset).then(function (finalMeta) {
            var fileId = finalMeta.id || activeSession.drive_file_id;
            if (!fileId) {
              throw new Error("Upload succeeded but no Drive file id returned");
            }
            if (!finalMeta.webViewLink) {
              return finalizeUploadMetadata(auth, fileId);
            }
            return finalMeta;
          });
        });
      });
    })
    .then(function (uploaded) {
      return {
        project_id: projectId,
        drive_file_id: uploaded.id,
        drive_file_name: uploaded.name,
        drive_file_web_view_link: uploaded.webViewLink || null,
        uploaded_at: nowIso(),
        file_size: fileSize,
        output_path: outputPath,
      };
    });
}

function testDriveConnection(payload) {
  var settings = payload.settings || {};
  if (!settings.parent_folder_id) {
    return Promise.reject(new Error("Missing parent_folder_id in settings"));
  }
  var auth = createDriveAuth(settings);
  return driveApiRequest(
    auth,
    "GET",
    "/drive/v3/files/" + encodeURIComponent(settings.parent_folder_id),
    {
      fields: "id,name,mimeType",
      supportsAllDrives: true,
    },
    null,
    null,
    true,
  ).then(function (folder) {
    if (folder.mimeType !== FOLDER_MIME) {
      throw new Error(
        "Configured parent ID is not a folder (mimeType=" +
          folder.mimeType +
          ")",
      );
    }
    return {
      ok: true,
      folder_id: folder.id,
      folder_name: folder.name,
    };
  });
}

function validateSettings(settings) {
  var missing = [];
  if (!settings) {
    throw new Error("Missing settings payload");
  }
  if (!settings.client_id) {
    missing.push("client_id");
  }
  if (!settings.client_secret) {
    missing.push("client_secret");
  }
  if (!settings.refresh_token) {
    missing.push("refresh_token");
  }
  if (!settings.parent_folder_id) {
    missing.push("parent_folder_id");
  }
  if (missing.length) {
    throw new Error("Missing Drive settings: " + missing.join(", "));
  }
}

function runTask(task, payload, emitProgress) {
  var reporter =
    typeof emitProgress === "function" ? emitProgress : function () {};
  var safePayload = payload || {};

  if (task === "testConnection") {
    validateSettings((safePayload || {}).settings || {});
    return testDriveConnection(safePayload);
  }

  if (task === "downloadProject") {
    validateSettings((safePayload || {}).settings || {});
    return performDownloadProject(safePayload, reporter);
  }

  if (task === "uploadOutput") {
    validateSettings((safePayload || {}).settings || {});
    return performResumableUpload(safePayload, reporter);
  }

  return Promise.reject(new Error("Unknown task: " + task));
}

module.exports = {
  runTask: runTask,
  RESUMABLE_CHUNK_SIZE: RESUMABLE_CHUNK_SIZE,
  OUTPUT_FILENAME: OUTPUT_FILENAME,
};
