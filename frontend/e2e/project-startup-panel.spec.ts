import { expect, test } from "@playwright/test";

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

function installStartupMocks({
  popupBlocked = false,
  terminalEvent,
}: {
  popupBlocked?: boolean;
  terminalEvent: Record<string, unknown>;
}) {
  return () => {
    const encoder = new TextEncoder();
    const originalFetch = window.fetch.bind(window);
    let startupController: ReadableStreamDefaultController<Uint8Array> | null =
      null;

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

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(requestUrl, window.location.origin);

      if (url.pathname === "/api/anime/source-details") {
        return new Response(JSON.stringify(SOURCE_DETAILS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.pathname === "/api/anime/jobs/stream") {
        return new Response(new ReadableStream({ start(controller) { controller.close(); } }), {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }

      if (url.pathname === "/api/tiktok-urls/check") {
        return new Response(
          JSON.stringify({
            exists: false,
            video_id: "123456789",
            registered_at: null,
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (url.pathname === "/api/projects/startup/jobs/stream") {
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            startupController = controller;
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }

      if (url.pathname === "/api/projects/start-async") {
        window.setTimeout(() => {
          startupController?.enqueue(
            encoder.encode(`data: ${JSON.stringify(terminalEvent)}\n\n`),
          );
          startupController?.close();
        }, 50);

        return new Response(
          JSON.stringify({
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
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (url.pathname === "/api/projects/project-1/startup/retry") {
        return new Response(
          JSON.stringify({
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
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      return originalFetch(input, init);
    };
  };
}

test("Démarrer launches background startup and redirects the pre-opened tab", async ({
  page,
}) => {
  await page.addInitScript(
    installStartupMocks({
      terminalEvent: {
        job_id: "startup-job-1",
        project_id: "project-1",
        anime_name: "Demo Source",
        series_id: "series-1",
        library_type: "anime",
        tiktok_url: "https://www.tiktok.com/@demo/video/123",
        status: "complete",
        progress: 1,
        phase: "complete",
        message: "Project startup complete.",
        error: null,
        ready_url: "/project/project-1/scenes",
        created_at: "2026-04-01T10:00:00Z",
        updated_at: "2026-04-01T10:00:10Z",
      },
    }),
  );

  await page.goto("/");
  await page.getByText("Demo Source").click();
  await page.getByPlaceholder("https://www.tiktok.com/@user/video/...").fill(
    "https://www.tiktok.com/@demo/video/123",
  );
  await page.getByRole("button", { name: "Démarrer" }).click();

  await page.getByRole("button", { name: /startup/ }).click();
  await expect(page.getByText("Demo Source")).toBeVisible();
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
  await page.addInitScript(
    installStartupMocks({
      terminalEvent: {
        job_id: "startup-job-1",
        project_id: "project-1",
        anime_name: "Demo Source",
        series_id: "series-1",
        library_type: "anime",
        tiktok_url: "https://www.tiktok.com/@demo/video/123",
        status: "error",
        progress: 0.8,
        phase: "activation",
        message: null,
        error: "Storage Box activation failed",
        ready_url: "/project/project-1/scenes",
        created_at: "2026-04-01T10:00:00Z",
        updated_at: "2026-04-01T10:00:10Z",
      },
    }),
  );

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
  await page.addInitScript(
    installStartupMocks({
      popupBlocked: true,
      terminalEvent: {
        job_id: "startup-job-1",
        project_id: "project-1",
        anime_name: "Demo Source",
        series_id: "series-1",
        library_type: "anime",
        tiktok_url: "https://www.tiktok.com/@demo/video/123",
        status: "complete",
        progress: 1,
        phase: "complete",
        message: "Project startup complete.",
        error: null,
        ready_url: "/project/project-1/scenes",
        created_at: "2026-04-01T10:00:00Z",
        updated_at: "2026-04-01T10:00:10Z",
      },
    }),
  );

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
  await page.addInitScript(
    installStartupMocks({
      terminalEvent: {
        job_id: "startup-job-1",
        project_id: "project-1",
        anime_name: "Demo Source",
        series_id: "series-1",
        library_type: "anime",
        tiktok_url: "https://www.tiktok.com/@demo/video/123",
        status: "complete",
        progress: 1,
        phase: "complete",
        message: "Project startup complete.",
        error: null,
        ready_url: "/project/project-1/scenes",
        created_at: "2026-04-01T10:00:00Z",
        updated_at: "2026-04-01T10:00:10Z",
      },
    }),
  );

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
