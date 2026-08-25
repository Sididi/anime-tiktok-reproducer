import { expect, test } from "@playwright/test";
import { installEventHubMock } from "./helpers/eventHubMock";

const SOURCE_DETAILS = [
  {
    name: "Demo Source",
    series_id: "series-1",
    episode_count: 12,
    local_episode_count: 12,
    total_size_bytes: 1024 * 1024 * 1024,
    fps: 24,
    is_fully_local: true,
    project_pin_count: 0,
    permanent_pin: false,
    storage_release_id: "release-1",
    torrent_count: 0,
    hydration_status: "fully_local",
    updated_at: "2026-04-01T10:00:00Z",
  },
];

const QUEUED_JOB = {
  job_id: "startup-job-1",
  project_id: "project-1",
  anime_name: "Demo Source",
  series_id: "series-1",
  library_type: "anime",
  tiktok_url: "https://www.tiktok.com/@demo/video/123",
  status: "queued",
  progress: 0,
  phase: "queued",
  message: "Startup queued",
  error: null,
  ready_url: null,
  created_at: "2026-04-01T10:00:00Z",
  updated_at: "2026-04-01T10:00:00Z",
};

const COMPLETED_JOB = {
  ...QUEUED_JOB,
  status: "complete",
  progress: 1,
  phase: "complete",
  message: "Project startup complete.",
  ready_url: "/project/project-1/scenes",
  updated_at: "2026-04-01T10:00:10Z",
};

const FAILED_JOB = {
  ...QUEUED_JOB,
  status: "error",
  progress: 0.8,
  phase: "activation",
  message: null,
  error: "Storage Box activation failed",
  ready_url: "/project/project-1/scenes",
  updated_at: "2026-04-01T10:00:10Z",
};

// Serialised into the page by addInitScript: everything it needs arrives
// through `config` (no outer-scope references).
function installStartupMocks(config: {
  popupBlocked: boolean;
  terminalEvent: Record<string, unknown> & { project_id: string };
  queuedJob: Record<string, unknown>;
  sourceDetails: unknown[];
}) {
  const { popupBlocked, terminalEvent, queuedJob, sourceDetails } = config;
  const originalFetch = window.fetch.bind(window);

  const createFakeElement = () => ({
    textContent: "",
    style: {},
    appendChild() {},
  });

  const createFakeWindow = () => ({
    closed: false,
    location: { href: "about:blank" },
    document: {
      title: "",
      body: {
        innerHTML: "",
        style: {},
        appendChild() {},
      },
      write() {},
      close() {},
      createElement() {
        return createFakeElement();
      },
    },
    close() {
      this.closed = true;
    },
  });

  (window as unknown as { __openedWindows: Array<Record<string, unknown>> })
    .__openedWindows = [];

  window.open = ((url?: string | URL) => {
    if (popupBlocked) {
      return null;
    }
    const fakeWindow = createFakeWindow();
    if (url) {
      fakeWindow.location.href = String(url);
    }
    (
      window as unknown as { __openedWindows: Array<Record<string, unknown>> }
    ).__openedWindows.push(fakeWindow);
    return fakeWindow as unknown as Window;
  }) as typeof window.open;

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);

    if (url.pathname === "/api/anime/source-details") {
      return json(sourceDetails);
    }

    if (url.pathname === "/api/tiktok-urls/check") {
      return json({ exists: false, video_id: "123456789", registered_at: null });
    }

    if (url.pathname === "/api/projects/start-async") {
      // The background job reports through the shared event stream.
      window.setTimeout(() => {
        const hub = (window as unknown as {
          __atrHub: { pushItem(topic: string, item: unknown): void };
        }).__atrHub;
        hub.pushItem("startup_jobs", {
          key: terminalEvent.project_id,
          project_id: terminalEvent.project_id,
          data: terminalEvent,
        });
      }, 50);
      return json(queuedJob);
    }

    if (url.pathname === "/api/projects/project-1/startup/retry") {
      return json(queuedJob);
    }

    return originalFetch(input, init);
  };
}

async function installMocks(
  page: import("@playwright/test").Page,
  options: { popupBlocked?: boolean; terminalEvent: typeof COMPLETED_JOB | typeof FAILED_JOB },
) {
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installStartupMocks, {
    popupBlocked: options.popupBlocked ?? false,
    terminalEvent: options.terminalEvent,
    queuedJob: QUEUED_JOB,
    sourceDetails: SOURCE_DETAILS,
  });
}

test("Démarrer launches background startup and redirects the pre-opened tab", async ({
  page,
}) => {
  await installMocks(page, { terminalEvent: COMPLETED_JOB });

  await page.goto("/");
  await page.getByText("Demo Source").click();
  await page.getByPlaceholder("https://www.tiktok.com/@user/video/...").fill(
    "https://www.tiktok.com/@demo/video/123",
  );
  await page.getByRole("button", { name: "Démarrer" }).click();

  await page.getByRole("button", { name: /startup/ }).click();
  // The source list and the startup panel both name the source.
  await expect(page.getByText("Demo Source", { exact: true })).toHaveCount(2);
  await expect(page.getByText("Terminé")).toBeVisible();

  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as { __openedWindows: Array<{ location: { href: string } }> }
          ).__openedWindows[0]?.location.href,
      ),
    )
    .toBe("/project/project-1/scenes");
});

test("failed startup surfaces retry and open actions", async ({ page }) => {
  await installMocks(page, { terminalEvent: FAILED_JOB });

  await page.goto("/");
  await page.getByText("Demo Source").click();
  await page.getByPlaceholder("https://www.tiktok.com/@user/video/...").fill(
    "https://www.tiktok.com/@demo/video/123",
  );
  await page.getByRole("button", { name: "Démarrer" }).click();

  await page.getByRole("button", { name: /startup/ }).click();
  await expect(page.getByText("Storage Box activation failed")).toBeVisible();
  await expect(page.getByRole("button", { name: "Ouvrir" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Relancer" })).toBeVisible();
});

test("popup-blocked startup keeps the open action available", async ({ page }) => {
  await installMocks(page, { popupBlocked: true, terminalEvent: COMPLETED_JOB });

  await page.goto("/");
  await page.getByText("Demo Source").click();
  await page.getByPlaceholder("https://www.tiktok.com/@user/video/...").fill(
    "https://www.tiktok.com/@demo/video/123",
  );
  await page.getByRole("button", { name: "Démarrer" }).click();

  await page.getByRole("button", { name: /startup/ }).click();
  await expect(page.getByRole("button", { name: "Ouvrir" })).toBeVisible();
});

test("series search can be cleared manually and resets on Démarrer", async ({
  page,
}) => {
  await installMocks(page, { terminalEvent: COMPLETED_JOB });

  await page.goto("/");
  await page.getByText("Demo Source").click();

  const searchInput = page.getByPlaceholder("Rechercher une source...");
  await searchInput.fill("Demo");
  await expect(page.getByRole("button", { name: "Effacer la recherche" })).toBeVisible();
  await page.getByRole("button", { name: "Effacer la recherche" }).click();
  await expect(searchInput).toHaveValue("");

  await searchInput.fill("Demo");
  await page.getByPlaceholder("https://www.tiktok.com/@user/video/...").fill(
    "https://www.tiktok.com/@demo/video/123",
  );
  await page.getByRole("button", { name: "Démarrer" }).click();

  await page.getByRole("button", { name: /startup/ }).click();
  await expect(searchInput).toHaveValue("");
  await expect(page.getByText("Terminé")).toBeVisible();
});

test("a startup job already in the snapshot is listed on load", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {
    topics: {
      startup_jobs: [
        { key: FAILED_JOB.project_id, project_id: FAILED_JOB.project_id, data: FAILED_JOB },
      ],
    },
  });
  await page.addInitScript(installStartupMocks, {
    popupBlocked: false,
    terminalEvent: COMPLETED_JOB,
    queuedJob: QUEUED_JOB,
    sourceDetails: SOURCE_DETAILS,
  });

  await page.goto("/");
  await page.getByRole("button", { name: /startup/ }).click();
  await expect(page.getByText("Storage Box activation failed")).toBeVisible();
});
