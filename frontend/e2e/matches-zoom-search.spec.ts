import { expect, test } from "@playwright/test";
import { installEventHubMock } from "./helpers/eventHubMock";

// Per-scene extensive zoom search: async job trigger, stacked completion
// alerts, card glow, and the three dismissal paths (alert click, Play Both,
// committed timing change). Job state arrives through the shared event
// stream (mocked by installEventHubMock): a started job is pushed as queued,
// then advanced to running and complete shortly after, emulating the
// server-side background job.
function installZoomMocks(
  {
    projectId,
    libraryType,
    playbackReady,
    completionFingerprint = "match",
    refetchEpisode,
  }: {
    projectId: string;
    libraryType: string;
    playbackReady: boolean;
    // Scene layout stamped on the fake completion: the real one, or one
    // that no longer describes the page's scenes.
    completionFingerprint?: "match" | "mismatch";
    // Once `window.__refetchArmed` is set, GET /matches reports scene 1 on
    // this episode — emulates matches.json having moved on.
    refetchEpisode?: string;
  },
) {
  const testWindow = window as typeof window & {
    __zoomStarted?: number[];
    __zoomAcks?: string[];
    __zoomJobs?: Record<string, Record<string, unknown>>;
    __refetchArmed?: boolean;
    __atrHub?: {
      push(frame: unknown): void;
      pushItem(topic: string, item: unknown): void;
    };
  };
  testWindow.__zoomStarted = [];
  testWindow.__zoomAcks = [];
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
      result_match: Record<string, unknown> | null;
      candidates_added: number;
      scene_fingerprint: { count: number; start: number; end: number } | null;
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
      if (refetchEpisode && testWindow.__refetchArmed) {
        return json({
          matches: matches.map((match) =>
            match.scene_index === 1
              ? { ...match, episode: refetchEpisode }
              : match,
          ),
        });
      }
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
      // Like the backend's invalidate_project: a recompute cancels the
      // project's unseen completed jobs and broadcasts them.
      for (const job of Object.values(jobs)) {
        if (job.status === "complete" && !job.acknowledged) {
          job.status = "cancelled";
          job.acknowledged = true;
          job.message = "Cancelled: recompute";
          testWindow.__atrHub?.pushItem("zoom_jobs", {
            key: job.id,
            project_id: job.project_id,
            data: { ...job },
          });
        }
      }
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
    const publishJob = (job: (typeof jobs)[string]) => {
      testWindow.__atrHub?.pushItem("zoom_jobs", {
        key: job.id,
        project_id: job.project_id,
        data: { ...job },
      });
    };
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
        result_match: null,
        candidates_added: 0,
        scene_fingerprint: null,
        error: null,
        acknowledged: false,
        created_at: 0,
      };
      jobs[job.id] = job;
      publishJob(job);
      // The fake background job runs and completes shortly after.
      window.setTimeout(() => {
        job.status = "running";
        job.scene_fingerprint =
          completionFingerprint === "match"
            ? { count: 3, start: sceneIndex * 2, end: sceneIndex * 2 + 2 }
            : { count: 5, start: 0, end: 1 };
        publishJob(job);
        job.status = "complete";
        job.changed = true;
        job.applied = true;
        const prior = matches.find(
          (match) => match.scene_index === job.scene_index,
        );
        job.result_match = prior
          ? {
              ...prior,
              alternatives: [
                {
                  episode: "Episode-03.mkv",
                  start_time: 100,
                  end_time: 102,
                  confidence: 0.95,
                  speed_ratio: 1,
                  vote_count: 4,
                  algorithm: "zoom_search_registered",
                },
              ],
            }
          : null;
        job.candidates_added = 1;
        job.message = "Match updated";
        publishJob(job);
      }, 150);
      return json({ job });
    }
    if (url.pathname === `${zoomBase}/jobs`) {
      return json({ jobs: Object.values(jobs) });
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
  await page.addInitScript(installEventHubMock, {});
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

  // The pushed completion event produces the alert + glow.
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
  await page.addInitScript(installEventHubMock, {});
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

test("completed zoom candidates appear in the manual modal without a reload", async ({
  page,
}) => {
  const projectId = "zoom-search-candidates";
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: true,
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.locator('[data-zoom-search-scene-index="1"]').click();
  await expect(page.locator("[data-zoom-search-alert-card]")).toBeVisible({
    timeout: 15000,
  });
  await mainCard(page, 1)
    .getByRole("button", { name: "Edit match timing" })
    .click();

  await expect(page.getByText("Episode-03.mkv")).toBeVisible();
  await expect(page.getByText("[zoom_search_registered]")).toBeVisible();
});

test("Play Both dismisses the scene's alert", async ({ page }) => {
  const projectId = "zoom-search-play-both";
  await page.addInitScript(installEventHubMock, {});
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
  await page.addInitScript(installEventHubMock, {});
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

test("recompute clears alerts and the job subscription stays alive", async ({
  page,
}) => {
  const projectId = "zoom-search-recompute";
  await page.addInitScript(installEventHubMock, {});
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

  await page.getByRole("button", { name: "Recompute", exact: true }).click();
  await expect(page.locator("[data-zoom-search-alert-card]")).toHaveCount(0);
  await expect(page.locator('[data-zoom-search-alert="true"]')).toHaveCount(0);

  // The server cancelled the unseen completion; a later snapshot replay
  // (reconnect) carries it as cancelled and must not resurrect the alert.
  await expect
    .poll(async () =>
      page.evaluate(() => window.__zoomJobs?.["job-1"]?.status ?? null),
    )
    .toBe("cancelled");
  await page.evaluate(() => {
    const job = window.__zoomJobs?.["job-1"];
    window.__atrHub?.push({
      kind: "snapshot",
      topic: "zoom_jobs",
      items: [{ key: "job-1", project_id: job?.project_id, data: job }],
    });
  });
  await expect(page.locator("[data-zoom-search-alert-card]")).toHaveCount(0);

  // The bridge's subscription is not registered in the page's abortable
  // stream set: it survives the recompute and keeps delivering job events.
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          window.__atrEventHub?.debugSnapshot().subscriptions.zoom_jobs ?? 0,
      ),
    )
    .toBe(1);
  await page.locator('[data-zoom-search-scene-index="0"]').click();
  const alert = page.locator("[data-zoom-search-alert-card]");
  await expect(alert).toBeVisible({ timeout: 15000 });
  await expect(alert).toContainText("Scene 1");
});

test("pure projects show no zoom search button", async ({ page }) => {
  const projectId = "zoom-search-pure";
  await page.addInitScript(installEventHubMock, {});
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

// A completed, unacknowledged job as the hub would replay it after a
// refresh or reconnect: its frozen result points at episodes the page's
// persisted matches never mention.
const seededCompletion = (projectId: string) => ({
  key: "job-seeded",
  project_id: projectId,
  data: {
    id: "job-seeded",
    project_id: projectId,
    scene_index: 1,
    status: "complete",
    message: "Existing match confirmed — 1 new AI candidate",
    changed: false,
    applied: false,
    old_match: null,
    new_match: null,
    result_match: {
      scene_index: 1,
      episode: "Episode-99.mkv",
      start_time: 500,
      end_time: 502,
      confidence: 0.9,
      speed_ratio: 1,
      confirmed: false,
      alternatives: [
        {
          episode: "Episode-98.mkv",
          start_time: 700,
          end_time: 702,
          confidence: 0.8,
          speed_ratio: 1,
          vote_count: 3,
          algorithm: "zoom_search_motion",
        },
      ],
      start_candidates: [],
      middle_candidates: [],
      end_candidates: [],
    },
    candidates_added: 1,
    scene_fingerprint: { count: 3, start: 2, end: 4 },
    error: null,
    acknowledged: false,
    created_at: 0,
  },
});

test("snapshot replay restores the alert but does not rewrite the match", async ({
  page,
}) => {
  const projectId = "zoom-search-snapshot-replay";
  await page.addInitScript(installEventHubMock, {
    topics: { zoom_jobs: [seededCompletion(projectId)] },
  });
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: true,
  });
  await page.goto(`/project/${projectId}/matches`);

  // The alert survives the refresh, with the backend's own summary text.
  const alert = page.locator("[data-zoom-search-alert-card]");
  await expect(alert).toBeVisible({ timeout: 15000 });
  await expect(alert).toContainText("Scene 2");
  await expect(alert).toContainText("1 new AI candidate");

  // Persisted matches stay the source of truth: the replayed frozen result
  // is not spliced into the page.
  await expect(mainCard(page, 1)).toContainText("Episode-02.mkv");
  await expect(mainCard(page, 1)).not.toContainText("Episode-99.mkv");
  await mainCard(page, 1)
    .getByRole("button", { name: "Edit match timing" })
    .click();
  await expect(page.getByText("Manual Match Selection - Scene 2")).toBeVisible();
  await expect(page.getByText("Episode-98.mkv")).toHaveCount(0);

  // A reconnect replays the same snapshot: still nothing applied.
  await page.evaluate(
    (job) =>
      window.__atrHub?.push({ kind: "snapshot", topic: "zoom_jobs", items: [job] }),
    seededCompletion(projectId),
  );
  await expect(mainCard(page, 1)).toContainText("Episode-02.mkv");
  await expect(page.getByText("Episode-98.mkv")).toHaveCount(0);

  // A LIVE completion for the same job is applied (the gate is
  // snapshot-vs-event, not a broken pipe).
  await page.evaluate(
    (job) => window.__atrHub?.pushItem("zoom_jobs", job),
    seededCompletion(projectId),
  );
  await expect(page.getByText("Episode-98.mkv")).toBeVisible();
});

test("snapshot with a completed job refetches persisted matches", async ({
  page,
}) => {
  const projectId = "zoom-search-snapshot-refetch";
  await page.addInitScript(installEventHubMock, {
    topics: { zoom_jobs: [seededCompletion(projectId)] },
  });
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: true,
    refetchEpisode: "Episode-07.mkv",
  });
  await page.goto(`/project/${projectId}/matches`);
  await expect(page.locator("[data-zoom-search-alert-card]")).toBeVisible({
    timeout: 15000,
  });
  await expect(mainCard(page, 1)).toContainText("Episode-02.mkv");

  // matches.json moved on (e.g. a completion landed while this tab was
  // disconnected); the replayed snapshot triggers a re-read of it instead
  // of applying the job's frozen result.
  await page.evaluate((job) => {
    window.__refetchArmed = true;
    window.__atrHub?.push({ kind: "snapshot", topic: "zoom_jobs", items: [job] });
  }, seededCompletion(projectId));
  await expect(mainCard(page, 1)).toContainText("Episode-07.mkv");
  await expect(mainCard(page, 1)).not.toContainText("Episode-99.mkv");
});

test("a completion whose scene fingerprint no longer matches is ignored", async ({
  page,
}) => {
  const projectId = "zoom-search-fingerprint";
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installZoomMocks, {
    projectId,
    libraryType: "anime",
    playbackReady: true,
    completionFingerprint: "mismatch",
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.locator('[data-zoom-search-scene-index="1"]').click();
  await expect(page.locator("[data-zoom-search-alert-card]")).toBeVisible({
    timeout: 15000,
  });
  await mainCard(page, 1)
    .getByRole("button", { name: "Edit match timing" })
    .click();
  await expect(page.getByText("Manual Match Selection - Scene 2")).toBeVisible();
  await expect(page.getByText("Episode-03.mkv")).toHaveCount(0);
});

declare global {
  interface Window {
    __zoomStarted?: number[];
    __zoomAcks?: string[];
    __zoomJobs?: Record<string, Record<string, unknown>>;
    __refetchArmed?: boolean;
    __atrHub?: {
      push(frame: unknown): void;
      pushItem(topic: string, item: unknown): void;
    };
    __atrEventHub?: {
      debugSnapshot(): { subscriptions: Record<string, number> };
    };
  }
}
