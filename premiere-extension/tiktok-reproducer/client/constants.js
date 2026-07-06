"use strict";

/**
 * Shared constants for the Tiktok Reproducer CEP panel (Node side).
 * Single source of truth for values that used to be duplicated across
 * main.js, drive_tasks.js, lan_tasks.js and subtitle_archive.js.
 *
 * ATR_BUILD_ID must stay in sync with ATR_HOST_BUILD_ID in host/host.jsx
 * (ExtendScript cannot require() this module).
 */

module.exports = {
  ATR_BUILD_ID: "2026-07-06-panel-cleanup-v9",

  OUTPUT_FILENAME: "output.mp4",
  AUDIO_NO_MUSIC_OUTPUT_FILENAME: "output_no_music.wav",
  PROJECT_CONTEXT_FILENAME: ".atr_project_context.json",

  SUBTITLES_DIRNAME: "subtitles",
  SUBTITLES_ARCHIVE_FILENAME: "atr_subtitles.zip",
  SUBTITLE_TIMING_FILENAME: "subtitle_timings.srt",

  PROXY_OUTPUT_SUFFIX: "__atr_proxy.mp4",
};
