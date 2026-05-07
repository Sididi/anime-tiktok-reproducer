import { expect, test } from "@playwright/test";

const ACCOUNT = {
  id: "acc_a",
  name: "Account A",
  language: "fr",
  avatar_url: "/api/accounts/acc_a/avatar",
  supported_types: ["anime"],
  slots: ["14:00"],
  slots_by_platform: {
    youtube: ["14:00"],
    facebook: ["14:00"],
    instagram: ["14:00"],
    tiktok: ["14:00", "18:00"],
  },
};

const ROW = {
  project_id: "p1",
  anime_title: "Show Alpha",
  library_type: "anime",
  language: "fr",
  local_size_bytes: 1024,
  uploaded: false,
  uploaded_status: "red",
  can_upload_status: "green",
  can_upload_reasons: [],
  has_metadata: true,
  drive_video_count: 1,
  drive_video_name: "p1.mp4",
  drive_video_web_url: "https://drive.example/p1",
  drive_folder_id: "folder",
  drive_folder_url: "https://drive.example/folder",
  drive_video_id: "drive-1",
  created_at: "2026-04-12T09:00:00Z",
  scheduled_at: null,
  scheduled_account_id: null,
  llm_preset_resolved: "default",
  llm_preset_is_default: true,
  min_playback_speed_resolved: 1,
  min_playback_speed_is_default: true,
  template_resolved: "default",
  template_is_default: true,
};

function installMocks(payload: { account: typeof ACCOUNT; row: typeof ROW }) {
  const { account, row } = payload;
  const testWindow = window as Window &
    typeof globalThis & {
      __uploadCalled?: boolean;
    };
  testWindow.__uploadCalled = false;
  const orig = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const emptyStream = () =>
      new Response(
        new ReadableStream({
          start(c) {
            c.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );

    if (url.pathname === "/api/accounts") {
      return json({ accounts: [account] });
    }
    if (url.pathname === "/api/project-manager/projects") {
      return json({ projects: [row] });
    }
    if (url.pathname === "/api/project-manager/upload-jobs") {
      return json({ jobs: [] });
    }
    if (url.pathname === "/api/project-manager/upload-jobs/stream") {
      return emptyStream();
    }
    if (url.pathname === "/api/anime/source-details") {
      return json([]);
    }
    if (
      url.pathname === "/api/anime/jobs/stream" ||
      url.pathname === "/api/projects/startup/jobs/stream"
    ) {
      return emptyStream();
    }
    if (url.pathname === "/api/projects/startup/jobs") {
      return json({ jobs: [] });
    }
    if (url.pathname.endsWith("/copyright-check")) {
      return json({ copyrighted: false });
    }
    if (
      url.pathname.endsWith("/facebook-check") ||
      url.pathname.endsWith("/youtube-check")
    ) {
      return json({
        needed: false,
        duration_seconds: 30,
        speed_factor: 1,
        sped_up_available: false,
      });
    }
    if (url.pathname.endsWith("/upload") && init?.method === "POST") {
      testWindow.__uploadCalled = true;
      return json({
        job_id: "j1",
        project_id: "p1",
        account_id: "acc_a",
        status: "queued",
        phase: "prepare",
        message: null,
        error: null,
        platform_results: [],
        result: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
    }
    return orig(input, init);
  };
}

test("Auto upload (single click on Upload) still works as before", async ({
  page,
}) => {
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  // Wait for the project row to render before interacting.
  const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
  await expect(projectRow).toBeVisible();

  // Open the account selector dropdown (trigger initially shows "All Projects").
  await page.getByRole("button", { name: "All Projects" }).click();
  // Click the "Account A" entry inside the dropdown.
  await page.getByRole("button", { name: "Account A" }).click();

  // Click the left half of the UploadSplitButton (label "Upload").
  await projectRow.getByRole("button", { name: /^Upload$/ }).click();

  await page.waitForFunction(
    () => (window as unknown as { __uploadCalled?: boolean }).__uploadCalled === true,
  );
});
