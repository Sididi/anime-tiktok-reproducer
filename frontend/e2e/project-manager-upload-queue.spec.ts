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

const INITIAL_PROJECT_ROWS = [
  {
    project_id: "project-alpha",
    anime_title: "Project Alpha",
    library_type: "anime",
    language: "fr",
    local_size_bytes: 1024,
    uploaded: false,
    uploaded_status: "red",
    can_upload_status: "green",
    can_upload_reasons: [],
    has_metadata: true,
    drive_video_count: 1,
    drive_video_name: "alpha.mp4",
    drive_video_web_url: "https://drive.example/alpha",
    drive_folder_id: "folder-alpha",
    drive_folder_url: "https://drive.example/folder-alpha",
    drive_video_id: "drive-alpha",
    created_at: "2026-04-12T09:00:00Z",
    scheduled_at: null,
    scheduled_account_id: null,
  },
  {
    project_id: "project-beta",
    anime_title: "Project Beta",
    library_type: "anime",
    language: "fr",
    local_size_bytes: 2048,
    uploaded: false,
    uploaded_status: "red",
    can_upload_status: "green",
    can_upload_reasons: [],
    has_metadata: true,
    drive_video_count: 1,
    drive_video_name: "beta.mp4",
    drive_video_web_url: "https://drive.example/beta",
    drive_folder_id: "folder-beta",
    drive_folder_url: "https://drive.example/folder-beta",
    drive_video_id: "drive-beta",
    created_at: "2026-04-12T09:05:00Z",
    scheduled_at: null,
    scheduled_account_id: null,
  },
];

function installProjectManagerUploadMocks({
  sourceDetails,
  initialProjectRows,
}: {
  sourceDetails: typeof SOURCE_DETAILS;
  initialProjectRows: typeof INITIAL_PROJECT_ROWS;
}) {
  const encoder = new TextEncoder();
  const originalFetch = window.fetch.bind(window);
  let projectRows = initialProjectRows.map((row) => ({ ...row }));
  let uploadController: ReadableStreamDefaultController<Uint8Array> | null =
    null;
  const pendingUploadEvents: string[] = [];

  const emitUploadJob = (payload: Record<string, unknown>) => {
    const chunk = `data: ${JSON.stringify(payload)}\n\n`;
    if (uploadController) {
      uploadController.enqueue(encoder.encode(chunk));
    } else {
      pendingUploadEvents.push(chunk);
    }
  };

  const jsonResponse = (payload: unknown) =>
    new Response(JSON.stringify(payload), {
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
      return jsonResponse(sourceDetails);
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

      if (
        url.pathname === "/api/projects/startup/jobs" ||
        url.pathname === "/api/projects/startup/jobs/stream"
      ) {
        if (url.pathname.endsWith("/stream")) {
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
        return jsonResponse({ jobs: [] });
      }

      if (url.pathname === "/api/project-manager/projects") {
        return jsonResponse({ projects: projectRows });
      }

      if (url.pathname === "/api/project-manager/upload-jobs") {
        return jsonResponse({ jobs: [] });
      }

      if (url.pathname === "/api/project-manager/upload-jobs/stream") {
        return new Response(
          new ReadableStream({
            start(controller) {
              uploadController = controller;
              pendingUploadEvents.splice(0).forEach((chunk) => {
                controller.enqueue(encoder.encode(chunk));
              });
            },
          }),
          {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          },
        );
      }

      if (url.pathname === "/api/accounts") {
        return jsonResponse({ accounts: [] });
      }

      if (url.pathname === "/api/project-manager/projects/project-alpha/copyright-check") {
        return jsonResponse({
          copyrighted: true,
          music_key: "track-a",
          music_display_name: "Track A",
          no_music_file_id: "no-music-alpha",
          no_music_available: true,
          available_musics: [
            { key: "replacement-1", display_name: "Replacement One" },
          ],
          drive_video_id: "drive-alpha",
        });
      }

      if (url.pathname === "/api/project-manager/projects/project-alpha/copyright-build-audio") {
        return jsonResponse({ audio_path: "/tmp/project-alpha-replacement.wav" });
      }

      if (url.pathname === "/api/project-manager/projects/project-alpha/facebook-check") {
        return jsonResponse({
          needed: false,
          duration_seconds: 45,
          speed_factor: 1,
          sped_up_available: false,
        });
      }

      if (url.pathname === "/api/project-manager/projects/project-alpha/youtube-check") {
        return jsonResponse({
          needed: false,
          duration_seconds: 45,
          speed_factor: 1,
          sped_up_available: false,
        });
      }

      if (url.pathname === "/api/project-manager/projects/project-beta/copyright-check") {
        return jsonResponse({ copyrighted: false });
      }

      if (url.pathname === "/api/project-manager/projects/project-beta/facebook-check") {
        return jsonResponse({
          needed: true,
          duration_seconds: 112,
          speed_factor: 1.25,
          sped_up_available: true,
        });
      }

      if (url.pathname === "/api/project-manager/projects/project-beta/youtube-check") {
        return jsonResponse({
          needed: false,
          duration_seconds: 112,
          speed_factor: 1,
          sped_up_available: false,
        });
      }

      if (
        url.pathname === "/api/project-manager/projects/project-alpha/upload" &&
        init?.method === "POST"
      ) {
        window.setTimeout(() => {
          emitUploadJob({
            job_id: "upload-job-alpha",
            project_id: "project-alpha",
            account_id: null,
            status: "running",
            phase: "platform_upload",
            message: "Uploading to social platforms...",
            error: null,
            result: null,
            created_at: "2026-04-12T10:00:00Z",
            updated_at: "2026-04-12T10:00:05Z",
          });
        }, 80);

        return jsonResponse({
          job_id: "upload-job-alpha",
          project_id: "project-alpha",
          account_id: null,
          status: "queued",
          phase: "queued",
          message: "Upload queued",
          error: null,
          result: null,
          created_at: "2026-04-12T10:00:00Z",
          updated_at: "2026-04-12T10:00:00Z",
        });
      }

      if (
        url.pathname === "/api/project-manager/projects/project-beta/upload" &&
        init?.method === "POST"
      ) {
        window.setTimeout(() => {
          emitUploadJob({
            job_id: "upload-job-beta",
            project_id: "project-beta",
            account_id: null,
            status: "running",
            phase: "platform_upload",
            message: "Uploading to social platforms...",
            error: null,
            result: null,
            created_at: "2026-04-12T10:00:00Z",
            updated_at: "2026-04-12T10:00:05Z",
          });
        }, 20);
        window.setTimeout(() => {
          projectRows = projectRows.map((row) =>
            row.project_id === "project-beta"
              ? {
                  ...row,
                  uploaded: true,
                  uploaded_status: "green",
                  scheduled_at: "2020-01-01T00:00:00Z",
                }
              : row,
          );
          emitUploadJob({
            job_id: "upload-job-beta",
            project_id: "project-beta",
            account_id: null,
            status: "complete",
            phase: "complete",
            message: "Upload complete.",
            error: null,
            result: { ok: true },
            created_at: "2026-04-12T10:00:00Z",
            updated_at: "2026-04-12T10:00:30Z",
          });
        }, 220);

        return jsonResponse({
          job_id: "upload-job-beta",
          project_id: "project-beta",
          account_id: null,
          status: "queued",
          phase: "queued",
          message: "Upload queued",
          error: null,
          result: null,
          created_at: "2026-04-12T10:00:00Z",
          updated_at: "2026-04-12T10:00:00Z",
        });
      }

      if (
        url.pathname.startsWith("/api/project-manager/projects/project-beta/facebook-preview/") ||
        url.pathname.startsWith("/api/project-manager/projects/project-alpha/copyright-video") ||
        url.pathname.startsWith("/api/project-manager/projects/project-alpha/copyright-audio")
      ) {
        return new Response("", {
          status: 200,
          headers: { "Content-Type": "video/mp4" },
        });
      }

    return originalFetch(input, init);
  };
}

test("Project Manager stacks upload prompts and keeps other uploads alive through refresh", async ({
  page,
}) => {
  await page.addInitScript(installProjectManagerUploadMocks, {
    sourceDetails: SOURCE_DETAILS,
    initialProjectRows: INITIAL_PROJECT_ROWS,
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const alphaRow = page.locator("tr").filter({ hasText: "Project Alpha" });
  const betaRow = page.locator("tr").filter({ hasText: "Project Beta" });

  await expect(alphaRow).toBeVisible();
  await expect(betaRow).toBeVisible();

  await alphaRow.getByRole("button", { name: "Upload" }).click();
  await expect(betaRow.getByRole("button", { name: "Upload" })).toBeEnabled();
  await betaRow.getByRole("button", { name: "Upload" }).click();

  await expect(page.getByText("Remplacement de la musique")).toBeVisible();
  await expect(
    page.getByText("Vidéo trop longue pour Facebook"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Couper à 1:30" }).click();
  await expect(
    page.getByRole("button", { name: "Utiliser cet audio" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Utiliser cet audio" }).click();

  await expect(alphaRow.getByRole("button", { name: "Uploading" })).toBeVisible();

  await expect
    .poll(async () => {
      const text = await betaRow.textContent();
      return text?.includes("Uploaded");
    })
    .toBeTruthy();

  await expect(alphaRow.getByRole("button", { name: "Uploading" })).toBeVisible();
});
