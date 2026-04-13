import { expect, test, type Page } from "@playwright/test";

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

type ProjectRow = (typeof INITIAL_PROJECT_ROWS)[number];

interface UploadRequestBody {
  account_id?: string | null;
  platforms?: string[] | null;
  facebook_strategy?: string | null;
  youtube_strategy?: string | null;
  copyright_audio_path?: string | null;
}

interface MockProjectConfig {
  copyrightCheck?: Record<string, unknown>;
  buildAudioPath?: string;
  facebookCheck?: Record<string, unknown>;
  youtubeCheck?: Record<string, unknown>;
  upload?: {
    jobId: string;
    runningDelayMs?: number;
    completeDelayMs?: number | null;
    completionRowPatch?: Partial<ProjectRow>;
    result?: Record<string, unknown> | null;
  };
}

const STACKED_PROJECT_CONFIGS: Record<string, MockProjectConfig> = {
  "project-alpha": {
    copyrightCheck: {
      copyrighted: true,
      music_key: "track-a",
      music_display_name: "Track A",
      no_music_file_id: "no-music-alpha",
      no_music_available: true,
      available_musics: [
        { key: "replacement-1", display_name: "Replacement One" },
      ],
      drive_video_id: "drive-alpha",
    },
    buildAudioPath: "/tmp/project-alpha-replacement.wav",
    facebookCheck: {
      needed: false,
      duration_seconds: 45,
      speed_factor: 1,
      sped_up_available: false,
    },
    youtubeCheck: {
      needed: false,
      duration_seconds: 45,
      speed_factor: 1,
      sped_up_available: false,
    },
    upload: {
      jobId: "upload-job-alpha",
      runningDelayMs: 80,
    },
  },
  "project-beta": {
    copyrightCheck: { copyrighted: false },
    facebookCheck: {
      needed: true,
      duration_seconds: 112,
      speed_factor: 1.25,
      sped_up_available: true,
    },
    youtubeCheck: {
      needed: false,
      duration_seconds: 112,
      speed_factor: 1,
      sped_up_available: false,
    },
    upload: {
      jobId: "upload-job-beta",
      runningDelayMs: 20,
      completeDelayMs: 220,
      completionRowPatch: {
        uploaded: true,
        uploaded_status: "green",
        scheduled_at: "2020-01-01T00:00:00Z",
      },
      result: { ok: true },
    },
  },
};

const FACEBOOK_CHAIN_CONFIGS: Record<string, MockProjectConfig> = {
  "project-alpha": {
    copyrightCheck: {
      copyrighted: true,
      music_key: "track-a",
      music_display_name: "Track A",
      no_music_file_id: "no-music-alpha",
      no_music_available: true,
      available_musics: [
        { key: "replacement-1", display_name: "Replacement One" },
      ],
      drive_video_id: "drive-alpha",
    },
    buildAudioPath: "/tmp/project-alpha-replacement.wav",
    facebookCheck: {
      needed: true,
      duration_seconds: 112,
      speed_factor: 1.25,
      sped_up_available: true,
    },
    youtubeCheck: {
      needed: false,
      duration_seconds: 112,
      speed_factor: 1,
      sped_up_available: false,
    },
    upload: {
      jobId: "upload-job-alpha-facebook",
      runningDelayMs: 20,
      completeDelayMs: 80,
      completionRowPatch: {
        uploaded: true,
        uploaded_status: "green",
        scheduled_at: "2026-04-12T10:00:30Z",
      },
      result: { ok: true },
    },
  },
};

const YOUTUBE_CHAIN_CONFIGS: Record<string, MockProjectConfig> = {
  "project-alpha": {
    copyrightCheck: {
      copyrighted: true,
      music_key: "track-a",
      music_display_name: "Track A",
      no_music_file_id: "no-music-alpha",
      no_music_available: true,
      available_musics: [
        { key: "replacement-1", display_name: "Replacement One" },
      ],
      drive_video_id: "drive-alpha",
    },
    buildAudioPath: "/tmp/project-alpha-replacement.wav",
    facebookCheck: {
      needed: false,
      duration_seconds: 45,
      speed_factor: 1,
      sped_up_available: false,
    },
    youtubeCheck: {
      needed: true,
      duration_seconds: 224,
      speed_factor: 1.2444,
      sped_up_available: true,
    },
    upload: {
      jobId: "upload-job-alpha-youtube",
      runningDelayMs: 20,
      completeDelayMs: 80,
      completionRowPatch: {
        uploaded: true,
        uploaded_status: "green",
        scheduled_at: "2026-04-12T10:00:30Z",
      },
      result: { ok: true },
    },
  },
};

function installProjectManagerUploadMocks({
  sourceDetails,
  initialProjectRows,
  projectConfigs,
}: {
  sourceDetails: typeof SOURCE_DETAILS;
  initialProjectRows: typeof INITIAL_PROJECT_ROWS;
  projectConfigs: Record<string, MockProjectConfig>;
}) {
  const encoder = new TextEncoder();
  const originalFetch = window.fetch.bind(window);
  const uploadRequestsByProject: Record<string, UploadRequestBody[]> = {};
  const testWindow = window as Window &
    typeof globalThis & {
      __projectManagerUploadRequests?: Record<string, UploadRequestBody[]>;
    };
  let projectRows = initialProjectRows.map((row) => ({ ...row }));
  let uploadController: ReadableStreamDefaultController<Uint8Array> | null =
    null;
  const pendingUploadEvents: string[] = [];

  testWindow.__projectManagerUploadRequests = uploadRequestsByProject;

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

  const emptyEventStream = () =>
    new Response(
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

  const parseJsonBody = (body: RequestInit["body"]): UploadRequestBody => {
    if (typeof body !== "string") {
      return {};
    }
    try {
      return JSON.parse(body) as UploadRequestBody;
    } catch {
      return {};
    }
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
      return jsonResponse(sourceDetails);
    }

    if (url.pathname === "/api/anime/jobs/stream") {
      return emptyEventStream();
    }

    if (
      url.pathname === "/api/projects/startup/jobs" ||
      url.pathname === "/api/projects/startup/jobs/stream"
    ) {
      if (url.pathname.endsWith("/stream")) {
        return emptyEventStream();
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

    const projectActionMatch = url.pathname.match(
      /^\/api\/project-manager\/projects\/([^/]+)\/(copyright-check|copyright-build-audio|facebook-check|youtube-check|upload)$/,
    );

    if (projectActionMatch) {
      const [, projectId, action] = projectActionMatch;
      const config = projectConfigs[projectId];
      if (!config) {
        return new Response("Not found", { status: 404 });
      }

      if (action === "copyright-check" && config.copyrightCheck) {
        return jsonResponse(config.copyrightCheck);
      }

      if (action === "copyright-build-audio" && config.buildAudioPath) {
        return jsonResponse({ audio_path: config.buildAudioPath });
      }

      if (action === "facebook-check" && config.facebookCheck) {
        return jsonResponse(config.facebookCheck);
      }

      if (action === "youtube-check" && config.youtubeCheck) {
        return jsonResponse(config.youtubeCheck);
      }

      if (action === "upload" && init?.method === "POST" && config.upload) {
        const requestBody = parseJsonBody(init.body);
        (uploadRequestsByProject[projectId] ||= []).push(requestBody);

        const queuedJob = {
          job_id: config.upload.jobId,
          project_id: projectId,
          account_id:
            typeof requestBody.account_id === "string"
              ? requestBody.account_id
              : null,
          status: "queued",
          phase: "queued",
          message: "Upload queued",
          error: null,
          result: null,
          created_at: "2026-04-12T10:00:00Z",
          updated_at: "2026-04-12T10:00:00Z",
        };

        window.setTimeout(() => {
          emitUploadJob({
            ...queuedJob,
            status: "running",
            phase: "platform_upload",
            message: "Uploading to social platforms...",
            updated_at: "2026-04-12T10:00:05Z",
          });
        }, config.upload.runningDelayMs ?? 20);

        if (config.upload.completeDelayMs != null) {
          window.setTimeout(() => {
            if (config.upload?.completionRowPatch) {
              projectRows = projectRows.map((row) =>
                row.project_id === projectId
                  ? { ...row, ...config.upload?.completionRowPatch }
                  : row,
              );
            }
            emitUploadJob({
              ...queuedJob,
              status: "complete",
              phase: "complete",
              message: "Upload complete.",
              result: config.upload?.result ?? { ok: true },
              updated_at: "2026-04-12T10:00:30Z",
            });
          }, config.upload.completeDelayMs);
        }

        return jsonResponse(queuedJob);
      }

      return new Response("Not found", { status: 404 });
    }

    if (
      /^\/api\/project-manager\/projects\/[^/]+\/copyright-audio$/.test(
        url.pathname,
      )
    ) {
      return new Response("", {
        status: 200,
        headers: { "Content-Type": "audio/wav" },
      });
    }

    if (
      /^\/api\/project-manager\/projects\/[^/]+\/copyright-video$/.test(
        url.pathname,
      ) ||
      /^\/api\/project-manager\/projects\/[^/]+\/facebook-preview\/(original|sped_up)$/.test(
        url.pathname,
      ) ||
      /^\/api\/project-manager\/projects\/[^/]+\/youtube-preview\/(original|sped_up)$/.test(
        url.pathname,
      )
    ) {
      return new Response("", {
        status: 200,
        headers: { "Content-Type": "video/mp4" },
      });
    }

    return originalFetch(input, init);
  };
}

async function latestUploadRequest(page: Page, projectId: string) {
  return page.evaluate((id) => {
    const testWindow = window as Window &
      typeof globalThis & {
        __projectManagerUploadRequests?: Record<string, UploadRequestBody[]>;
      };
    const requests = testWindow.__projectManagerUploadRequests?.[id] ?? [];
    return requests[requests.length - 1] ?? null;
  }, projectId);
}

test("Project Manager stacks upload prompts and keeps other uploads alive through refresh", async ({
  page,
}) => {
  await page.addInitScript(installProjectManagerUploadMocks, {
    sourceDetails: SOURCE_DETAILS,
    initialProjectRows: INITIAL_PROJECT_ROWS,
    projectConfigs: STACKED_PROJECT_CONFIGS,
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

test("Project Manager keeps copyright audio path through Facebook duration prompt", async ({
  page,
}) => {
  await page.addInitScript(installProjectManagerUploadMocks, {
    sourceDetails: SOURCE_DETAILS,
    initialProjectRows: [INITIAL_PROJECT_ROWS[0]],
    projectConfigs: FACEBOOK_CHAIN_CONFIGS,
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const alphaRow = page.locator("tr").filter({ hasText: "Project Alpha" });
  await expect(alphaRow).toBeVisible();

  await alphaRow.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByText("Remplacement de la musique")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Utiliser cet audio" }),
  ).toBeEnabled();

  await page.getByRole("button", { name: "Utiliser cet audio" }).click();
  await expect(
    page.getByText("Vidéo trop longue pour Facebook"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Couper à 1:30" }).click();

  await expect
    .poll(async () => latestUploadRequest(page, "project-alpha"))
    .toMatchObject({
      facebook_strategy: "cut",
      copyright_audio_path: "/tmp/project-alpha-replacement.wav",
    });
});

test("Project Manager keeps copyright audio path through YouTube duration prompt", async ({
  page,
}) => {
  await page.addInitScript(installProjectManagerUploadMocks, {
    sourceDetails: SOURCE_DETAILS,
    initialProjectRows: [INITIAL_PROJECT_ROWS[0]],
    projectConfigs: YOUTUBE_CHAIN_CONFIGS,
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const alphaRow = page.locator("tr").filter({ hasText: "Project Alpha" });
  await expect(alphaRow).toBeVisible();

  await alphaRow.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByText("Remplacement de la musique")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Utiliser cet audio" }),
  ).toBeEnabled();

  await page.getByRole("button", { name: "Utiliser cet audio" }).click();
  await expect(
    page.getByText("Vidéo trop longue pour YouTube"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Couper à 3:00" }).click();

  await expect
    .poll(async () => latestUploadRequest(page, "project-alpha"))
    .toMatchObject({
      youtube_strategy: "cut",
      copyright_audio_path: "/tmp/project-alpha-replacement.wav",
    });
});
