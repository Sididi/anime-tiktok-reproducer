import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { installEventHubMock, type HubMockHandle } from "./helpers/eventHubMock";

const PROJECT_ID = "cleanup-recovery-test";
const PROJECT_PATH = `/api/projects/${PROJECT_ID}`;
const CLEANUP = {
  zones: [],
  clean_feed_rect: { x: 0, y: 0.2, w: 1, h: 0.5 },
  status: "idle",
  progress: 0,
};
const FAILED_JOB = {
  job_id: "cleanup-startup-test",
  project_id: PROJECT_ID,
  status: "error",
  progress: 0.05,
  phase: "download",
  error: "This post may not be comfortable for some audiences. Log in for access.",
  message: null,
  updated_at: "2026-09-24T14:48:29Z",
};

let video: Buffer;
let audioOnly: Buffer;

test.beforeAll(() => {
  // Tiny generated fixtures exercise real browser decoding without downloading
  // TikTok media or changing any local project. FFmpeg is an app prerequisite.
  video = execFileSync("ffmpeg", [
    "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=48x80:rate=30",
    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
  ]);
  audioOnly = execFileSync("ffmpeg", [
    "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440",
    "-t", "1", "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov",
    "-f", "mp4", "pipe:1",
  ]);
});

async function mockProject(page: Page, available: boolean) {
  const state = { available, previewStatus: 200, media: video, retryCalls: 0, rawRequests: 0 };
  await page.addInitScript(installEventHubMock, {
    topics: { startup_jobs: available ? [] : [{ key: PROJECT_ID, project_id: PROJECT_ID, data: FAILED_JOB }] },
  });
  await page.route(`**${PROJECT_PATH}{,/**}`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === PROJECT_PATH) {
      return route.fulfill({ json: {
        id: PROJECT_ID, library_type: "pure", phase: state.available ? "cleanup" : "setup",
        tiktok_url: "https://www.tiktok.com/@demo/video/123",
        video_path: state.available ? "/source/hevc.mp4" : null,
        video_duration: state.available ? 1 : null, video_fps: 30,
        cleanup: CLEANUP,
      } });
    }
    if (path === `${PROJECT_PATH}/cleanup`) return route.fulfill({ json: CLEANUP });
    if (path === `${PROJECT_PATH}/video`) {
      state.rawRequests++;
      return route.abort();
    }
    if (path === `${PROJECT_PATH}/video/preview`) {
      return route.fulfill({
        status: state.previewStatus, contentType: "video/mp4", body: state.media,
        headers: { "Access-Control-Allow-Origin": "*" },
      });
    }
    if (path === `${PROJECT_PATH}/startup/retry`) {
      state.retryCalls++;
      return route.fulfill({ json: {
        ...FAILED_JOB, status: "queued", error: null, message: "Startup queued",
        updated_at: "2026-09-30T08:38:52Z",
      } });
    }
    return route.abort();
  });
  return state;
}

test("missing download shows its login error and recovers after retry", async ({ page }) => {
  const state = await mockProject(page, false);
  await page.goto(`/project/${PROJECT_ID}/cleanup`);
  await expect(page.getByRole("alert")).toHaveText(FAILED_JOB.error);
  await expect(page.locator("video")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Skip cleanup" })).toHaveCount(0);
  await page.getByRole("button", { name: "Retry download" }).click();
  await expect(page.getByRole("button", { name: "Downloading…" })).toBeDisabled();
  expect(state.retryCalls).toBe(1);

  state.available = true;
  await page.evaluate(({ id, job }) => {
    (window as unknown as { __atrHub: HubMockHandle }).__atrHub.pushItem("startup_jobs", {
      key: id, project_id: id,
      data: { ...job, status: "complete", error: null, updated_at: "2026-09-30T08:39:06Z" },
    });
  }, { id: PROJECT_ID, job: FAILED_JOB });
  await expect(page.getByRole("button", { name: "Skip cleanup" })).toBeEnabled();
  await expect(page.getByText("clean feed", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("cleanup decodes the compatible preview instead of requesting the HEVC original", async ({ page }) => {
  const state = await mockProject(page, true);
  await page.goto(`/project/${PROJECT_ID}/cleanup`);
  await expect(page.getByRole("button", { name: "Subtitle zone" })).toBeEnabled();
  const media = await page.locator("video").evaluate((video: HTMLVideoElement) => ({
    src: video.currentSrc, width: video.videoWidth, height: video.videoHeight,
  }));
  expect(media.src).toContain(`${PROJECT_PATH}/video/preview`);
  expect([media.width, media.height]).toEqual([48, 80]);
  expect(state.rawRequests).toBe(0);
});

test("failed media requests show a recoverable error instead of a blank editor", async ({ page }) => {
  const state = await mockProject(page, true);
  state.previewStatus = 404;
  await page.goto(`/project/${PROJECT_ID}/cleanup`);
  await expect(page.getByText("The video could not be loaded.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Skip cleanup" })).toHaveCount(0);
  state.previewStatus = 200;
  await page.getByRole("button", { name: "Reload video" }).click();
  await expect(page.getByRole("button", { name: "Skip cleanup" })).toBeEnabled();
  expect(state.retryCalls).toBe(0);
});

test("audio-only playback without a media error does not leave a black player", async ({ page }) => {
  const state = await mockProject(page, true);
  state.media = audioOnly;
  await page.goto(`/project/${PROJECT_ID}/cleanup`);
  await expect(page.getByText("The browser could not decode the video track.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Skip cleanup" })).toHaveCount(0);
  state.media = video;
  await page.getByRole("button", { name: "Reload video" }).click();
  await expect(page.getByRole("button", { name: "Skip cleanup" })).toBeEnabled();
});
