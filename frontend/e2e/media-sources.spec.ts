import { expect, test } from "@playwright/test";
import {
  computeSourceChunkWindowStart,
  isTimeInsideSourceChunkWindow,
  resolveSourcePlaybackMode,
} from "../src/hooks/useSourcePlaybackStrategy";
import { buildVideoSourceCandidates, getProjectVideoSourceCandidates } from "../src/utils/mediaSources";
import type { SourceStreamDescriptor } from "../src/types";

function makeDescriptor(
  overrides: Partial<SourceStreamDescriptor>,
): SourceStreamDescriptor {
  return {
    mode: overrides.mode ?? "passthrough",
    duration: overrides.duration ?? 140,
    codec: overrides.codec ?? "h264",
    pix_fmt: overrides.pix_fmt ?? "yuv420p",
    chunk_duration: overrides.chunk_duration ?? 30,
    chunk_step: overrides.chunk_step ?? 20,
    seek_guard_seconds: overrides.seek_guard_seconds ?? 5,
  };
}

test("native-first project video candidates keep direct video before preview fallback", () => {
  expect(
    buildVideoSourceCandidates(
      "http://127.0.0.1:8000/api/projects/demo/video",
      "http://127.0.0.1:8000/api/projects/demo/video/preview",
    ),
  ).toEqual([
    "http://127.0.0.1:8000/api/projects/demo/video",
    "http://127.0.0.1:8000/api/projects/demo/video/preview",
  ]);

  expect(
    buildVideoSourceCandidates(
      "http://127.0.0.1:8000/api/projects/demo/video",
      "http://127.0.0.1:8000/api/projects/demo/video",
    ),
  ).toEqual(["http://127.0.0.1:8000/api/projects/demo/video"]);

  expect(getProjectVideoSourceCandidates("demo-project")).toEqual([
    "http://127.0.0.1:8000/api/projects/demo-project/video",
    "http://127.0.0.1:8000/api/projects/demo-project/video/preview",
  ]);
});

test("manual source playback keeps native passthrough for normal-speed HEVC", () => {
  const hevcDescriptor = makeDescriptor({
    mode: "passthrough",
    codec: "hevc",
    pix_fmt: "yuv420p10le",
  });

  expect(
    resolveSourcePlaybackMode(hevcDescriptor, {
      playbackRate: 1,
      preferChunkedHighRateHevc: true,
    }),
  ).toBe("passthrough");

  expect(
    resolveSourcePlaybackMode(hevcDescriptor, {
      playbackRate: 16,
      preferChunkedHighRateHevc: true,
    }),
  ).toBe("chunked");

  expect(
    resolveSourcePlaybackMode(
      makeDescriptor({ mode: "chunked", codec: "vp9", pix_fmt: "yuv444p" }),
      {
        playbackRate: 1,
        preferChunkedHighRateHevc: true,
      },
    ),
  ).toBe("chunked");
});

test("chunk window helpers keep the preview seek target inside the safe window", () => {
  const descriptor = makeDescriptor({
    mode: "chunked",
    duration: 300,
    chunk_duration: 40,
    chunk_step: 10,
    seek_guard_seconds: 4,
  });

  const centeredStart = computeSourceChunkWindowStart(152, descriptor, {
    alignment: "center",
    windowDuration: 60,
    maxDuration: 120,
  });
  expect(centeredStart).toBe(120);
  expect(
    isTimeInsideSourceChunkWindow(152, centeredStart, descriptor, 60),
  ).toBe(true);
  expect(
    isTimeInsideSourceChunkWindow(184, centeredStart, descriptor, 60),
  ).toBe(false);
});
