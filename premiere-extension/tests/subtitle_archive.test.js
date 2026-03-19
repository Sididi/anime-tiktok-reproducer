"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const subtitleArchive = require("../tiktok-reproducer/client/subtitle_archive");

function makeTempProject() {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "atr-subtitle-archive-test-"),
  );
  const subtitlesDir = path.join(root, "subtitles");
  fs.mkdirSync(subtitlesDir, { recursive: true });
  const archivePath = path.join(subtitlesDir, "atr_subtitles.zip");
  fs.writeFileSync(archivePath, "fake zip payload", "utf8");
  return {
    root,
    subtitlesDir,
    archivePath,
  };
}

test("buildZipExtractScript uses System.IO.Compression.ZipFile", () => {
  const script = subtitleArchive.buildZipExtractScript(
    "C:\\project\\subtitles\\atr_subtitles.zip",
    "C:\\project\\__atr_extract",
  );

  assert.match(script, /System\.IO\.Compression\.ZipFile/);
  assert.doesNotMatch(script, /Expand-Archive/);
  assert.doesNotMatch(script, /CreateDirectory/);
});

test("expandSubtitleArchiveAsync extracts into sibling temp dir and merges files", async () => {
  const project = makeTempProject();
  const extractedPaths = [];

  const result = await subtitleArchive.expandSubtitleArchiveAsync({
    localRootPath: project.root,
    extractArchiveAsync: async (archivePath, tempDir) => {
      extractedPaths.push({ archivePath, tempDir });
      fs.mkdirSync(tempDir, { recursive: true });
      fs.writeFileSync(
        path.join(tempDir, "subtitle_timings.srt"),
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        "utf8",
      );
      fs.writeFileSync(path.join(tempDir, "captions.ass"), "[Script Info]", "utf8");
    },
  });

  assert.equal(result.extracted, true);
  assert.equal(result.extracted_file_count, 2);
  assert.equal(extractedPaths.length, 1);
  assert.equal(extractedPaths[0].archivePath, project.archivePath);
  assert.equal(path.dirname(extractedPaths[0].tempDir), project.root);
  assert.match(path.basename(extractedPaths[0].tempDir), /^__atr_subtitles_extract__/);
  assert.equal(fs.existsSync(project.archivePath), false);
  assert.equal(
    fs.existsSync(path.join(project.subtitlesDir, "subtitle_timings.srt")),
    true,
  );
  assert.equal(
    fs.existsSync(path.join(project.subtitlesDir, "captions.ass")),
    true,
  );
  assert.equal(fs.existsSync(extractedPaths[0].tempDir), false);
});

test("expandSubtitleArchiveSync fails when subtitle_timings.srt is missing", () => {
  const project = makeTempProject();
  let tempDirPath = "";

  assert.throws(
    () =>
      subtitleArchive.expandSubtitleArchiveSync({
        localRootPath: project.root,
        extractArchiveSync: (_archivePath, tempDir) => {
          tempDirPath = tempDir;
          fs.mkdirSync(tempDir, { recursive: true });
          fs.writeFileSync(path.join(tempDir, "captions.ass"), "[Script Info]", "utf8");
        },
      }),
    /subtitle_timings\.srt/,
  );

  assert.equal(fs.existsSync(project.archivePath), true);
  assert.equal(tempDirPath.length > 0, true);
  assert.equal(fs.existsSync(tempDirPath), false);
});
