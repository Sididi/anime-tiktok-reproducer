import { expect, test } from "@playwright/test";

// Per-scene extensive zoom search: async job trigger, stacked completion
// alerts, card glow, and the three dismissal paths (alert click, Play Both,
// committed timing change). The jobs stream mock is finite — the bridge
// reconnects every 3s, and each reconnect advances queued jobs to complete,
// emulating the server-side background job.
function installZoomMocks(
  {
    projectId,
    libraryType,
    playbackReady,
  }: { projectId: string; libraryType: string; playbackReady: boolean },
) {
  const testWindow = window as typeof window & {
    __zoomStarted?: number[];
    __zoomAcks?: string[];
    __zoomStreamConnects?: number;
    __zoomJobs?: Record<string, Record<string, unknown>>;
  };
  testWindow.__zoomStarted = [];
  testWindow.__zoomAcks = [];
  testWindow.__zoomStreamConnects = 0;
  const jobs: Record<
    string,
    {
      id: string;
      project_id: string;
      scene_index: number;
      status: string;
      message: string;
      changed: boolean | null;
      applied: boolean | null;
      old_match: null;
      new_match: null;
      error: null;
      acknowledged: boolean;
      created_at: number;
    }
  > = {};
  testWindow.__zoomJobs = jobs;

  const originalFetch = window.fetch.bind(window);
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  const sse = (events: unknown[]) =>
    new Response(
      events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    );

  const scenes = [0, 1, 2].map((index) => ({
    index,
    start_time: index * 2,
    end_time: index * 2 + 2,
    duration: 2,
  }));
  const matchDefaults = {
    speed_ratio: 1,
    confirmed: true,
    alternatives: [],
    start_candidates: [],
    middle_candidates: [],
    end_candidates: [],
  };
  const matches = [
    {
      ...matchDefaults,
      scene_index: 0,
      episode: "Episode-01.mkv",
      start_time: 10,
      end_time: 12,
      confidence: 0.95,
    },
    {
      ...matchDefaults,
      scene_index: 1,
      episode: "Episode-02.mkv",
      start_time: 40,
      end_time: 42,
      confidence: 0.9,
    },
    {
      ...matchDefaults,
      scene_index: 2,
      episode: "",
      start_time: 0,
      end_time: 0,
      confidence: 0,
      confirmed: false,
      was_no_match: true,
    },
  ];

  const clip = (sceneIndex: number, track: "tiktok" | "source") => ({
    scene_index: sceneIndex,
    track,
    url: `/media/fake-${track}-${sceneIndex}.mp4`,
    duration: 2,
    ready: true,
    clip_id: `clip-${track}-${sceneIndex}`,
    status: "ready",
    profile: track === "tiktok" ? "tiktok_fast" : "source_fast",
  });
  const manifest = playbackReady
    ? {
        ready: true,
        fingerprint: "fp",
        generated_at: "2026-08-14T00:00:00Z",
        scenes: matches.map((match) => ({
          scene_index: match.scene_index,
          has_match: match.confidence > 0,
          status: "ready",
          tiktok: clip(match.scene_index, "tiktok"),
          source: match.confidence > 0 ? clip(match.scene_index, "source") : null,
        })),
      }
    : { ready: false, clips: [], scenes: [] };

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);
    const prefix = `/api/projects/${projectId}`;

    if (url.pathname === prefix) {
      return json({
        id: projectId,
        phase: "match_validation",
        source_paths: [],
        video_path: "/tmp/test.mp4",
        video_duration: 6,
        video_fps: 24,
        anime_name: "Test Anime",
        series_id: "series-test",
        library_type: libraryType,
        output_language: "fr",
      });
    }
    if (url.pathname === `${prefix}/scenes`) {
      return json({ scenes });
    }
    if (url.pathname === `${prefix}/matches` && init?.method !== "POST") {
      return json({ matches });
    }
    if (url.pathname === `${prefix}/sources/episodes`) {
      return json({ episodes: ["Episode-01", "Episode-02", "Episode-03"] });
    }
    if (url.pathname === `${prefix}/scenes/config`) {
      return json({ skip_ui_enabled: false });
    }
    if (url.pathname === `${prefix}/matches/config`) {
      return json({ full_auto_enabled: false });
    }
    if (url.pathname === `${prefix}/transcription`) {
      return json({ transcription: null });
    }
    if (url.pathname === `${prefix}/transcription/config`) {
      return json({ full_auto_enabled: false });
    }
    if (url.pathname === `${prefix}/matches/playback/manifest`) {
      return json(manifest);
    }
    if (url.pathname === `${prefix}/matches/find` && init?.method === "POST") {
      return sse([
        {
          status: "complete",
          progress: 1,
          message: "Matched",
          matches: { matches },
        },
      ]);
    }
    if (url.pathname === `${prefix}/matches/deferred-download`) {
      return sse([{ status: "complete", progress: 1, message: "Done" }]);
    }
    if (url.pathname === `${prefix}/matches/playback/prepare`) {
      return sse([
        { status: "complete", progress: 1, message: "Ready", manifest },
      ]);
    }
    if (
      url.pathname.startsWith(`${prefix}/matches/playback/prepare-scene/`)
    ) {
      const sceneIndex = Number(url.pathname.split("/").pop());
      const sceneAsset = (manifest as { scenes?: unknown[] }).scenes?.find(
        (asset) =>
          (asset as { scene_index: number }).scene_index === sceneIndex,
      );
      return sse([
        {
          status: "complete",
          progress: 1,
          message: "Ready",
          scene_asset: sceneAsset ?? null,
        },
      ]);
    }

    // --- zoom search endpoints ---
    const zoomBase = `${prefix}/matches/zoom-search`;
    const zoomStartMatch = url.pathname.match(
      new RegExp(`^${zoomBase}/(\\d+)$`),
    );
    if (zoomStartMatch && init?.method === "POST") {
      const sceneIndex = Number(zoomStartMatch[1]);
      testWindow.__zoomStarted?.push(sceneIndex);
      const job = {
        id: `job-${sceneIndex}`,
        project_id: projectId,
        scene_index: sceneIndex,
        status: "queued",
        message: "",
        changed: null,
        applied: null,
        old_match: null,
        new_match: null,
        error: null,
        acknowledged: false,
        created_at: 0,
      };
      jobs[job.id] = job;
      return json({ job });
    }
    if (url.pathname === `${zoomBase}/jobs`) {
      return json({ jobs: Object.values(jobs) });
    }
    if (url.pathname === `${zoomBase}/jobs/stream`) {
      testWindow.__zoomStreamConnects =
        (testWindow.__zoomStreamConnects ?? 0) + 1;
      const events: unknown[] = [];
      for (const job of Object.values(jobs)) {
        if (job.status === "queued") {
          // Each reconnect advances the fake background job to completion.
          job.status = "running";
          events.push({ kind: "zoom_job", job: { ...job } });
          job.status = "complete";
          job.changed = true;
          job.applied = true;
          job.message = "Match updated";
          events.push({ kind: "zoom_job", job: { ...job } });
        } else {
          events.push({ kind: "zoom_job", job: { ...job } });
        }
      }
      return sse(events);
    }
    const ackMatch = url.pathname.match(
      new RegExp(`^${zoomBase}/jobs/([^/]+)/ack$`),
    );
    if (ackMatch && init?.method === "POST") {
      const job = jobs[ackMatch[1]];
      if (!job) return new Response("{}", { status: 404 });
      job.acknowledged = true;
      testWindow.__zoomAcks?.push(ackMatch[1]);
      return json({ job });
    }
    if (
      url.pathname.startsWith(`${prefix}/matches/`) &&
      init?.method === "PUT"
    ) {
      const sceneIndex = Number(url.pathname.split("/").pop());
      const body = JSON.parse(String(init?.body ?? "{}"));
      const match = matches.find((item) => item.scene_index === sceneIndex);
      return json({
        status: "updated",
        match: { ...(match ?? matchDefaults), scene_index: sceneIndex, ...body },
      });
    }

    return originalFetch(input, init);
  };
}

// The mainCard div always carries data-zoom-search-alert ("true"/"false"),
// which disambiguates it from the outer wrapper sharing data-scene-index.
const mainCard = (page: import("@playwright/test").Page, sceneIndex: number) =>
  page.locator(
    `[data-scene-index="${sceneIndex}"][data-zoom-search-alert]`,
  );

const glowCard = (page: import("@playwright/test").Page, sceneIndex: number) =>
  page.locator(
    `[data-zoom-search-alert="true"][data-scene-index="${sceneIndex}"]`,
  );

test("trigger shows job state then a clickable alert with card glow", async ({
  page,
}) => {
  const projectId = "zoom-search-basic";
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: false,
  });
  await page.goto(`/project/${projectId}/matches`);

  const button = page.locator('[data-zoom-search-scene-index="1"]');
  await expect(button).toBeVisible();
  await button.click();

  await expect
    .poll(async () => page.evaluate(() => window.__zoomStarted ?? []))
    .toEqual([1]);
  // Optimistic queued state disables the button immediately.
  await expect(button).toBeDisabled();

  // The next stream reconnect (≤3s) completes the job: alert + glow.
  const alert = page.locator("[data-zoom-search-alert-card]");
  await expect(alert).toBeVisible({ timeout: 15000 });
  await expect(alert).toContainText("Scene 2");
  await expect(alert).toContainText("Match updated");
  await expect(glowCard(page, 1)).toHaveCount(1);
  await expect(button).toBeEnabled();
});

test("clicking the alert acks, dismisses, unglows and activates the scene", async ({
  page,
}) => {
  const projectId = "zoom-search-alert-click";
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: false,
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.locator('[data-zoom-search-scene-index="1"]').click();
  const alert = page.locator("[data-zoom-search-alert-card]");
  await expect(alert).toBeVisible({ timeout: 15000 });

  await alert.click();
  await expect(alert).toHaveCount(0);
  await expect(glowCard(page, 1)).toHaveCount(0);
  await expect
    .poll(async () => page.evaluate(() => window.__zoomAcks ?? []))
    .toEqual(["job-1"]);
  // Teleport: the scene card is now the active (ring-highlighted) one.
  await expect(mainCard(page, 1)).toHaveClass(/ring-2/);
});

test("Play Both dismisses the scene's alert", async ({ page }) => {
  const projectId = "zoom-search-play-both";
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: true,
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.locator('[data-zoom-search-scene-index="0"]').click();
  const alert = page.locator("[data-zoom-search-alert-card]");
  await expect(alert).toBeVisible({ timeout: 15000 });

  await mainCard(page, 0).getByRole("button", { name: "Play Both" }).click();
  await expect(alert).toHaveCount(0);
  await expect(glowCard(page, 0)).toHaveCount(0);
  await expect
    .poll(async () => page.evaluate(() => window.__zoomAcks ?? []))
    .toEqual(["job-0"]);
});

test("saving a timing change in the modal dismisses the alert", async ({
  page,
}) => {
  const projectId = "zoom-search-manual-save";
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: true,
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.locator('[data-zoom-search-scene-index="0"]').click();
  const alert = page.locator("[data-zoom-search-alert-card]");
  await expect(alert).toBeVisible({ timeout: 15000 });

  // Open the timing modal (pencil button in the matched action row) and
  // save without edits — a committed timing change either way.
  await mainCard(page, 0)
    .getByRole("button", { name: "Edit match timing" })
    .click();
  const saveButton = page.getByRole("button", { name: "Save Match" });
  await expect(saveButton).toBeEnabled();
  await saveButton.click();

  await expect(alert).toHaveCount(0, { timeout: 15000 });
  await expect(glowCard(page, 0)).toHaveCount(0);
  await expect
    .poll(async () => page.evaluate(() => window.__zoomAcks ?? []))
    .toEqual(["job-0"]);
});

test("recompute clears alerts and the job stream stays alive", async ({
  page,
}) => {
  const projectId = "zoom-search-recompute";
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: false,
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.locator('[data-zoom-search-scene-index="1"]').click();
  await expect(page.locator("[data-zoom-search-alert-card]")).toBeVisible({
    timeout: 15000,
  });

  const connectsBefore = await page.evaluate(
    () => window.__zoomStreamConnects ?? 0,
  );
  await page.getByRole("button", { name: "Recompute", exact: true }).click();
  await expect(page.locator("[data-zoom-search-alert-card]")).toHaveCount(0);
  await expect(page.locator('[data-zoom-search-alert="true"]')).toHaveCount(0);

  // The bridge's subscription is not registered in the page's abortable
  // stream set: reconnects continue after the recompute.
  await expect
    .poll(
      async () => page.evaluate(() => window.__zoomStreamConnects ?? 0),
      { timeout: 15000 },
    )
    .toBeGreaterThan(connectsBefore);
});

test("pure projects show no zoom search button", async ({ page }) => {
  const projectId = "zoom-search-pure";
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "pure",
    playbackReady: false,
  });
  await page.goto(`/project/${projectId}/matches`);

  await expect(
    page.getByRole("button", { name: "Recompute", exact: true }),
  ).toBeVisible();
  await expect(page.locator("[data-zoom-search-button]")).toHaveCount(0);
});

declare global {
  interface Window {
    __zoomStarted?: number[];
    __zoomAcks?: string[];
    __zoomStreamConnects?: number;
    __zoomJobs?: Record<string, Record<string, unknown>>;
  }
}
