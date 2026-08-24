import { expect, test } from "@playwright/test";
import { installEventHubMock } from "./helpers/eventHubMock";

const COMPLETED_JOB = {
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

const INDEXING_JOB = {
  id: "job-2",
  job_type: "index",
  source_name: "Solo Leveling",
  library_type: "anime",
  source_path: "/tmp/solo-leveling",
  fps: 2,
  status: "indexing",
  progress: 0.54,
  phase: "indexing",
  message: "Processing Solo Leveling/ep03.mp4 (batch 3, frames 48)",
  current_file: "Solo Leveling/ep03.mp4",
  total_files: 4,
  completed_files: 1,
  current_file_progress: 0.42,
  current_file_frames_processed: 48,
  current_file_total_frames: 114,
  current_file_batches_processed: 3,
  error: null,
  warnings: [],
  unmatched_files: [],
  linked_torrents: 0,
  series_id: "series-2",
  storage_release_id: null,
  created_at: "2026-03-26T12:05:00Z",
};

function installLibraryMocks() {
  const originalFetch = window.fetch.bind(window);
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

    return originalFetch(input, init);
  };
}

test("completed index jobs surface skipped unreadable-file warnings", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {
    topics: {
      index_jobs: [{ key: COMPLETED_JOB.id, project_id: null, data: COMPLETED_JOB }],
    },
  });
  await page.addInitScript(installLibraryMocks);

  await page.goto("/");

  await expect(page.getByText("Classroom of the Elite")).toBeVisible();
  await expect(page.getByText("Terminé — 1 fichier ignoré")).toBeVisible();
  await expect(
    page.locator('[title*="Ignored unreadable source file: S01E08-clean-no-attachments.mkv"]'),
  ).toBeVisible();
});

test("index jobs surface per-file sampled-frame progress details", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {
    topics: {
      index_jobs: [{ key: INDEXING_JOB.id, project_id: null, data: INDEXING_JOB }],
    },
  });
  await page.addInitScript(installLibraryMocks);

  await page.goto("/");

  await expect(page.getByText("Solo Leveling", { exact: true })).toBeVisible();
  await expect(page.getByText("1/4 fichiers")).toBeVisible();
  await expect(page.getByText("Solo Leveling/ep03.mp4")).toBeVisible();
  await expect(page.getByText("42%")).toBeVisible();
  await expect(page.getByText("48/114 frames")).toBeVisible();
});

test("a live update replaces the job and a snapshot drops vanished jobs", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {
    topics: {
      index_jobs: [{ key: INDEXING_JOB.id, project_id: null, data: INDEXING_JOB }],
    },
  });
  await page.addInitScript(installLibraryMocks);

  await page.goto("/");
  await expect(page.getByText("42%")).toBeVisible();

  await page.evaluate((job) => {
    const hub = (window as unknown as {
      __atrHub: { pushItem(topic: string, item: unknown): void };
    }).__atrHub;
    hub.pushItem("index_jobs", {
      key: job.id,
      project_id: null,
      data: { ...job, progress: 0.8, current_file_progress: 0.75, completed_files: 3 },
    });
  }, INDEXING_JOB);
  await expect(page.getByText("75%")).toBeVisible();
  await expect(page.getByText("3/4 fichiers")).toBeVisible();

  // A backend restart re-sends snapshots; a job missing from the new
  // snapshot must disappear from the panel.
  await page.evaluate(() => {
    const hub = (window as unknown as {
      __atrHub: { topics: Record<string, unknown[]>; close(): void };
    }).__atrHub;
    hub.topics.index_jobs = [];
    hub.close();
  });
  await expect(page.getByText("Solo Leveling")).toHaveCount(0, { timeout: 10000 });
});
