import { expect, test } from "@playwright/test";
import { buildVideoSourceCandidates, getProjectVideoSourceCandidates } from "../src/utils/mediaSources";

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
