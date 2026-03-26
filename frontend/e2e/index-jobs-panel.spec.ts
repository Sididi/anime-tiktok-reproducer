import { expect, test } from "@playwright/test";

test("completed index jobs surface skipped unreadable-file warnings", async ({ page }) => {
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    const jobsPayload = {
      id: "job-1",
      job_type: "index",
      source_name: "Classroom of the Elite",
      library_type: "anime",
      source_path: "/tmp/classroom",
      fps: 2,
      status: "complete",
      progress: 1,
      phase: "complete",
      message: "Successfully indexed Classroom of the Elite",
      error: null,
      warnings: ["Ignored unreadable source file: S01E08-clean-no-attachments.mkv"],
      unmatched_files: [],
      linked_torrents: 0,
      series_id: "series-1",
      storage_release_id: "release-1",
      created_at: "2026-03-26T12:00:00Z",
    };

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(requestUrl, window.location.origin);

      if (url.pathname === "/api/anime/source-details") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.pathname === "/api/anime/jobs/stream") {
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(`data: ${JSON.stringify(jobsPayload)}\n\n`),
            );
            controller.close();
          },
        });

        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }

      return originalFetch(input, init);
    };
  });

  await page.goto("/");

  await expect(page.getByText("Classroom of the Elite")).toBeVisible();
  await expect(page.getByText("Terminé — 1 fichier ignoré")).toBeVisible();
  await expect(
    page.locator('[title*="Ignored unreadable source file: S01E08-clean-no-attachments.mkv"]'),
  ).toBeVisible();
});
