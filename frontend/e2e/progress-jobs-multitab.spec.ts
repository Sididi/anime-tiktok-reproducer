import { test, expect, type Page } from "@playwright/test";

// Run against backend/tests/progress_browser_server.py, never real projects.
test.skip(process.env.ATR_PROGRESS_BROWSER_TEST !== "1", "Requires the isolated progress backend");

async function openObserver(page: Page, id: string, polling = false) {
  await page.route("**/progress-test", (route) => route.fulfill({
    contentType: "text/html", body: "<!doctype html><title>Progress test</title>",
  }));
  if (polling) await page.addInitScript(() => { Object.defineProperty(window, "SharedWorker", { value: undefined }); });
  await page.goto("/progress-test");
  await page.evaluate(async (projectId) => {
    // @ts-expect-error Vite serves this source module at runtime.
    const { startProgressJob } = await import("/src/utils/progressJobs.ts");
    // @ts-expect-error Vite serves this source module at runtime.
    const { readSSEStream } = await import("/src/utils/sse.ts");
    const response = await startProgressJob(`/api/projects/${projectId}/transcription/start`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ language: "auto" }),
    });
    document.body.dataset.accepted = "true";
    void readSSEStream(response, (event: { status: string; project_id?: string }) => {
      document.body.dataset.status = event.status;
      if (event.project_id) document.body.dataset.project = event.project_id;
    }).catch((error: Error) => { document.body.dataset.error = error.message; });
  }, id);
}

test("20 tabs submit without socket starvation; close/reopen keeps jobs and one shared connection", async ({ browser, request }) => {
  await request.post("/api/test/reset");
  const context = await browser.newContext();
  try {
    const pages: Page[] = [];
    for (let i = 0; i < 20; i += 1) {
      const page = await context.newPage();
      pages.push(page);
      await openObserver(page, `p${i}`);
    }
    const stats = await (await request.get("/api/test/stats")).json();
    expect(stats).toMatchObject({ jobs: 20, finished: 0, active: 2, peak: 2, subscribers: 1 });
    // A normal API read still completes while all 20 operations are pending.
    expect((await request.get("/api/events/stats", { timeout: 2000 })).ok()).toBeTruthy();
    await pages[0].close();
    const resumed = await context.newPage();
    await openObserver(resumed, "p0");
    expect((await (await request.get("/api/test/stats")).json()).jobs).toBe(20);
    await request.post("/api/test/release");
    for (const [i, page] of pages.entries()) {
      if (i === 0) continue;
      await expect(page.locator("body")).toHaveAttribute("data-status", "complete");
      await expect(page.locator("body")).toHaveAttribute("data-project", `p${i}`);
    }
    await expect(resumed.locator("body")).toHaveAttribute("data-project", "p0");
    expect((await (await request.get("/api/test/stats")).json()).finished).toBe(20);
  } finally {
    await context.close();
  }
});

test("browsers without SharedWorker use short snapshots, with no per-tab SSE sockets", async ({ browser, request }) => {
  await request.post("/api/test/reset");
  const baseline = (await (await request.get("/api/test/stats")).json()).subscribers;
  const context = await browser.newContext();
  try {
    const pages = [];
    for (let i = 0; i < 8; i += 1) {
      const page = await context.newPage();
      pages.push(page);
      await openObserver(page, `fallback${i}`, true);
    }
    expect((await (await request.get("/api/test/stats")).json()).subscribers).toBe(baseline);
    await request.post("/api/test/release");
    for (const page of pages) await expect(page.locator("body")).toHaveAttribute("data-status", "complete");
  } finally {
    await context.close();
  }
});

test("the transcription page reattaches to an active job after reload without another POST", async ({ page, request }) => {
  await request.post("/api/test/reset");
  const id = "resume-ui";
  await request.post(`/api/projects/${id}/transcription/start`, {
    headers: { Prefer: "respond-async" }, data: { language: "auto" },
  });
  await page.route(`**/api/projects/${id}`, (route) => route.fulfill({ json: {
    id, phase: "transcription", source_paths: [], video_path: "/test.mp4",
    video_duration: 2, video_fps: 24, library_type: "anime", output_language: "fr",
  } }));
  await page.route(`**/api/projects/${id}/scenes`, (route) => route.fulfill({ json: {
    scenes: [{ index: 0, start_time: 0, end_time: 2, duration: 2 }],
  } }));
  await page.route(`**/api/projects/${id}/transcription`, (route) => route.fulfill({ json: { transcription: null } }));
  await page.route(`**/api/projects/${id}/transcription/config`, (route) => route.fulfill({ json: { full_auto_enabled: false } }));
  const posts: string[] = [];
  page.on("request", (r) => { if (r.url().endsWith("/transcription/start")) posts.push(r.url()); });
  await page.goto(`/project/${id}/transcription`);
  await expect(page.getByText("Transcribing test audio…")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Transcribing test audio…")).toBeVisible();
  expect(posts).toEqual([]);
  await request.post("/api/test/release");
  await expect(page.getByText("1 scenes transcribed in en")).toBeVisible();
  expect((await (await request.get("/api/test/stats")).json()).started).toBe(1);
});
