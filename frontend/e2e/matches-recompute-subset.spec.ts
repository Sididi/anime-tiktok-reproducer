import { expect, test } from "@playwright/test";

// The episode option list is extensionless while match episodes carry the
// container extension — the proposal must bridge the two via stems.
//
// Episode-03 is "seen" only through a weak alternative (not proposed);
// Episode-04 appears nowhere in the previous run — it was indexed after it —
// and must always be proposed.
function installSubsetMocks(
  { projectId, libraryType }: { projectId: string; libraryType: string },
) {
  const testWindow = window as typeof window & {
    __findBodies?: unknown[];
    __episodeFetches?: number;
  };
  testWindow.__findBodies = [];
  testWindow.__episodeFetches = 0;

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
      episode: "Episode-01.mkv",
      start_time: 100,
      end_time: 102,
      confidence: 0.85,
      alternatives: [
        {
          episode: "Episode-03.mkv",
          start_time: 300,
          end_time: 302,
          confidence: 0.2,
          speed_ratio: 1,
          vote_count: 1,
          algorithm: "timeline_cluster",
        },
      ],
    },
  ];

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
      testWindow.__episodeFetches = (testWindow.__episodeFetches ?? 0) + 1;
      return json({
        episodes: ["Episode-01", "Episode-02", "Episode-03", "Episode-04"],
      });
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
      return json({ ready: false, clips: [], scenes: [] });
    }
    if (url.pathname === `${prefix}/matches/find` && init?.method === "POST") {
      testWindow.__findBodies?.push(JSON.parse(String(init?.body ?? "{}")));
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
        {
          status: "complete",
          progress: 1,
          message: "Ready",
          manifest: { ready: false, clips: [], scenes: [] },
        },
      ]);
    }

    return originalFetch(input, init);
  };
}

test("primary click recomputes with the AI-proposed episode subset", async ({
  page,
}) => {
  const projectId = "recompute-subset-primary";
  await page.addInitScript(installSubsetMocks, {
    projectId,
    libraryType: "anime",
  });
  await page.goto(`/project/${projectId}/matches`);

  const splitButton = page.getByRole("button", { name: "Recompute (3 ep.)" });
  await expect(splitButton).toBeVisible();
  const fetchesBefore = await page.evaluate(() => window.__episodeFetches ?? 0);
  await splitButton.click();

  await expect
    .poll(async () =>
      page.evaluate(() => (window.__findBodies ?? []).length),
    )
    .toBe(1);
  const body = await page.evaluate(
    () => (window.__findBodies ?? [])[0] as { episodes?: string[] },
  );
  // Scored picks plus the never-matched Episode-04.
  expect(body.episodes).toEqual(["Episode-01", "Episode-02", "Episode-04"]);
  // The episode list is re-read once the recompute completes (a recompute
  // usually follows a series re-index).
  await expect
    .poll(async () => page.evaluate(() => window.__episodeFetches ?? 0))
    .toBeGreaterThan(fetchesBefore);
});

test("modal allows manual episode selection before recompute", async ({
  page,
}) => {
  const projectId = "recompute-subset-modal";
  await page.addInitScript(installSubsetMocks, {
    projectId,
    libraryType: "anime",
  });
  await page.goto(`/project/${projectId}/matches`);

  const fetchesBefore = await page.evaluate(() => window.__episodeFetches ?? 0);
  await page.getByRole("button", { name: "Choose episodes" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // Opening the chooser re-reads the episode list.
  await expect
    .poll(async () => page.evaluate(() => window.__episodeFetches ?? 0))
    .toBeGreaterThan(fetchesBefore);

  // AI-proposed rows are pre-checked and badged (incl. the never-matched
  // Episode-04); the weakly-seen third episode is not.
  await expect(dialog.locator('[aria-label="AI proposed"]')).toHaveCount(3);
  const checkboxes = dialog.locator('input[type="checkbox"]');
  await expect(checkboxes.nth(0)).toBeChecked();
  await expect(checkboxes.nth(1)).toBeChecked();
  await expect(checkboxes.nth(2)).not.toBeChecked();
  await expect(checkboxes.nth(3)).toBeChecked();

  // Unticking one launches without it.
  await checkboxes.nth(1).uncheck();
  await dialog.getByRole("button", { name: "Recompute" }).click();
  await expect
    .poll(async () =>
      page.evaluate(() => (window.__findBodies ?? []).length),
    )
    .toBe(1);
  const body = await page.evaluate(
    () => (window.__findBodies ?? [])[0] as { episodes?: string[] },
  );
  expect(body.episodes).toEqual(["Episode-01", "Episode-04"]);
});

test("selecting no episodes disables the modal launch button", async ({
  page,
}) => {
  const projectId = "recompute-subset-none";
  await page.addInitScript(installSubsetMocks, {
    projectId,
    libraryType: "anime",
  });
  await page.goto(`/project/${projectId}/matches`);

  await page.getByRole("button", { name: "Choose episodes" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "None", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "Recompute", exact: true }),
  ).toBeDisabled();
});

test("pure projects do not show the episode-subset button", async ({
  page,
}) => {
  const projectId = "recompute-subset-pure";
  await page.addInitScript(installSubsetMocks, {
    projectId,
    libraryType: "pure",
  });
  await page.goto(`/project/${projectId}/matches`);

  await expect(
    page.getByRole("button", { name: "Recompute", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Recompute \(\d+ ep\.\)/ }),
  ).toHaveCount(0);
});

declare global {
  interface Window {
    __findBodies?: unknown[];
    __episodeFetches?: number;
  }
}
