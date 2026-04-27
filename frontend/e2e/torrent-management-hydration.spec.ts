import { expect, test } from "@playwright/test";

function installTorrentManagementMocks() {
  return () => {
    const originalFetch = window.fetch.bind(window);
    let episodeLocal = false;
    let seriesStatePolls = 0;

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(requestUrl, window.location.origin);

      if (url.pathname === "/api/anime/source-details") {
        return new Response(
          JSON.stringify([
            {
              name: "Demo Source",
              series_id: "series-1",
              episode_count: 1,
              local_episode_count: episodeLocal ? 1 : 0,
              total_size_bytes: 1024 * 1024,
              fps: 24,
              is_fully_local: episodeLocal,
              project_pin_count: 0,
              permanent_pin: false,
              storage_release_id: "release-1",
              torrent_count: 0,
              hydration_status: episodeLocal ? "fully_local" : "not_hydrated",
              updated_at: "2026-04-01T10:00:00Z",
            },
          ]),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (url.pathname === "/api/anime/jobs/stream") {
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.close();
            },
          }),
          {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          },
        );
      }

      if (url.pathname === "/api/anime/series-1/episodes") {
        return new Response(
          JSON.stringify({
            storage_box: {
              available: true,
              series_id: "series-1",
              release_id: "release-1",
              episode_count: 1,
              local_episode_count: episodeLocal ? 1 : 0,
              episodes: [
                {
                  episode_key: "ep-1",
                  size_bytes: 1024 * 1024,
                  local: episodeLocal,
                  local_relative_path: episodeLocal ? "Demo/ep-1.mp4" : null,
                },
              ],
            },
            torrents: {
              torrent_count: 0,
              items: [],
            },
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (url.pathname === "/api/anime/series-1/hydrate") {
        seriesStatePolls = 0;
        return new Response(
          JSON.stringify({
            series_id: "series-1",
            release_id: "release-1",
            hydration_status: "hydrating_episodes",
            local_episode_count: 0,
            expected_episode_count: 1,
            is_fully_local: false,
            permanent_pin: false,
            project_pin_count: 0,
            last_error: null,
            operation: {
              type: "hydrate",
              status: "pending",
              progress: 0,
              error: null,
              updated_at: "2026-04-01T10:00:00Z",
            },
            updated_at: "2026-04-01T10:00:00Z",
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (url.pathname === "/api/anime/series-1/state") {
        seriesStatePolls += 1;
        if (seriesStatePolls >= 2) {
          episodeLocal = true;
          return new Response(
            JSON.stringify({
              series_id: "series-1",
              release_id: "release-1",
              hydration_status: "fully_local",
              local_episode_count: 1,
              expected_episode_count: 1,
              is_fully_local: true,
              permanent_pin: false,
              project_pin_count: 0,
              last_error: null,
              operation: {
                type: "hydrate",
                status: "complete",
                progress: 1,
                error: null,
                updated_at: "2026-04-01T10:00:05Z",
              },
              updated_at: "2026-04-01T10:00:05Z",
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }

        return new Response(
          JSON.stringify({
            series_id: "series-1",
            release_id: "release-1",
            hydration_status: "hydrating_episodes",
            local_episode_count: 0,
            expected_episode_count: 1,
            is_fully_local: false,
            permanent_pin: false,
            project_pin_count: 0,
            last_error: null,
            operation: {
              type: "hydrate",
              status: "running",
              progress: 0.5,
              error: null,
              updated_at: "2026-04-01T10:00:02Z",
            },
            updated_at: "2026-04-01T10:00:02Z",
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

test("torrent management waits for background hydration and refreshes source state", async ({
  page,
}) => {
  await page.addInitScript(installTorrentManagementMocks());

  await page.goto("/");
  await page.getByText("Demo Source").click();
  await page.getByTitle("Gérer les épisodes").click();

  await expect(page.getByText("Storage Box principal")).toBeVisible();
  await expect(page.getByText("0/1 épisode(s)")).toBeVisible();

  await page.getByRole("button", { name: "Télécharger", exact: true }).click();

  await expect(page.getByRole("button", { name: "Téléchargement..." })).toBeVisible();
  await expect(page.getByText("1/1 épisode(s)")).toBeVisible();
  await expect(page.getByText("Local", { exact: true })).toBeVisible();
});
