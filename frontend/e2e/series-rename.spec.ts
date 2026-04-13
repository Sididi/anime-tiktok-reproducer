import { expect, test } from "@playwright/test";

test("renaming a source refreshes the list and preserves selection by series_id", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    let currentName = "Old Name";

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
              name: currentName,
              series_id: "series-1",
              episode_count: 1,
              local_episode_count: 1,
              total_size_bytes: 1024,
              fps: 24,
              is_fully_local: true,
              project_pin_count: 0,
              permanent_pin: false,
              storage_release_id: "release-1",
              torrent_count: 0,
              hydration_status: "fully_local",
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

      if (url.pathname === "/api/projects/startup/jobs") {
        return new Response(
          JSON.stringify({ jobs: [] }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      if (url.pathname === "/api/projects/startup/jobs/stream") {
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

      if (url.pathname === "/api/anime/series-1/rename" && init?.method === "PATCH") {
        const body =
          typeof init.body === "string" ? JSON.parse(init.body) : {};
        if (body.new_name !== "New Name" || body.library_type !== "anime") {
          return new Response(
            JSON.stringify({
              detail: {
                code: "series_rename_invalid",
                message: "Unexpected rename payload",
              },
            }),
            {
              status: 400,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        currentName = "New Name";
        return new Response(
          JSON.stringify({
            status: "renamed",
            series_id: "series-1",
            library_type: "anime",
            old_name: "Old Name",
            new_name: "New Name",
            storage_release_id: "release-2",
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      return originalFetch(input, init);
    };
  });

  await page.goto("/");
  await page.getByText("Old Name").click();

  const selectedRow = page.locator('[data-series-id="series-1"]');
  await expect(selectedRow).toHaveAttribute("data-selected", "true");

  await page.getByTitle("Renommer la source").click();
  const renameDialog = page.getByRole("dialog");
  await renameDialog.getByLabel("Nouveau nom").fill("New Name");
  await renameDialog.getByRole("button", { name: "Renommer", exact: true }).click();

  await expect(page.getByText("New Name")).toBeVisible();
  await expect(page.getByText("Old Name")).toHaveCount(0);
  await expect(selectedRow).toHaveAttribute("data-selected", "true");
});
