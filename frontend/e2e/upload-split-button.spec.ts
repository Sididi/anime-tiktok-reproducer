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

function installSchedulingMocks() {
  const testWindow = window as Window &
    typeof globalThis & {
      __anchored?: boolean;
    };
  testWindow.__anchored = false;
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
    // Slots are sent as UTC ISO; SlotChips renders them via Europe/Paris.
    // 12:00Z → 14:00 Paris (CEST in May), 16:00Z → 18:00 Paris.
    if (url.pathname === "/api/scheduling/free-slots") {
      return json({
        slots: [
          { slot: "2026-05-08T12:00:00Z", available: true },
          { slot: "2026-05-08T16:00:00Z", available: true },
        ],
      });
    }
    if (url.pathname === "/api/scheduling/resolve-anchor") {
      return json({
        resolved: {
          tiktok: {
            slot: "2026-05-08T12:00:00Z",
            scheduled_at: "2026-05-08T12:08:00Z",
            available: true,
          },
          youtube: {
            slot: "2026-05-08T12:00:00Z",
            scheduled_at: "2026-05-08T12:09:00Z",
            available: true,
          },
        },
        conflicts: [],
      });
    }
    if (url.pathname.includes("/reserve-anchor") && init?.method === "POST") {
      testWindow.__anchored = true;
      return json({
        platform_schedules: {
          tiktok: {
            slot: "2026-05-08T12:00:00Z",
            scheduled_at: "2026-05-08T12:08:00Z",
          },
        },
      });
    }
    return orig(input, init);
  };
}

// Wrap fetch one more time so that copyright/facebook/youtube check responses
// resolve on a macrotask (setTimeout). Otherwise React 18 doesn't have time to
// commit `setUploadSession` and update the session ref before the awaited check
// resolves, which causes `isSessionCurrent` to return false and the upload flow
// to halt at "checking_copyright". The auto path doesn't trip on this because
// it has no preceding await before the first setUploadSession.
function installCheckDelay() {
  const orig = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);
    if (
      url.pathname.endsWith("/copyright-check") ||
      url.pathname.endsWith("/facebook-check") ||
      url.pathname.endsWith("/youtube-check")
    ) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    return orig(input, init);
  };
}

test("Schedule mode reserves anchor before upload", async ({ page }) => {
  await page.addInitScript(installSchedulingMocks);
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
  await page.addInitScript(installCheckDelay);
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
  await expect(projectRow).toBeVisible();

  await page.getByRole("button", { name: "All Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();

  await projectRow.getByRole("button", { name: "Upload options" }).click();
  await page.getByRole("button", { name: /Schedule for specific slot/ }).click();
  // Slots are mocked for 2026-05-08; click that day in the calendar so the
  // popover's same-day filter surfaces them.
  await page
    .getByRole("heading", { name: "Pick a slot" })
    .locator("..")
    .getByRole("button", { name: "8", exact: true })
    .click();
  await page.getByRole("button", { name: /^14:00$/ }).first().click();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();

  await page.waitForFunction(
    () => (window as unknown as { __anchored?: boolean }).__anchored === true,
  );
  await page.waitForFunction(
    () => (window as unknown as { __uploadCalled?: boolean }).__uploadCalled === true,
  );
});

function installCascadeMocks() {
  const testWindow = window as Window &
    typeof globalThis & {
      __cascadeApplied?: boolean;
    };
  testWindow.__cascadeApplied = false;
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
    if (url.pathname.endsWith("/cascade-preview")) {
      return json({
        per_platform: [
          {
            platform: "tiktok",
            target_slot: "2026-05-07T14:00:00Z",
            target_scheduled_at: "2026-05-07T14:09:00Z",
            displaced: [
              {
                project_id: "x",
                anime_title: "Bumped",
                from_slot: "2026-05-07T14:00:00Z",
                to_slot: "2026-05-07T18:00:00Z",
                requires_platform_notification: true,
              },
            ],
          },
        ],
        blockers: [],
      });
    }
    if (url.pathname.endsWith("/cascade-apply") && init?.method === "POST") {
      testWindow.__cascadeApplied = true;
      return json({
        per_platform: [],
        blockers: [],
        notification_status: {},
      });
    }
    return orig(input, init);
  };
}

test("Urgent mode previews and applies cascade", async ({ page }) => {
  await page.addInitScript(installCascadeMocks);
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
  await page.addInitScript(installCheckDelay);
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
  await expect(projectRow).toBeVisible();

  await page.getByRole("button", { name: "All Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();

  await projectRow.getByRole("button", { name: "Upload options" }).click();
  await page.getByRole("button", { name: /Upload urgently/ }).click();

  await expect(page.getByText(/will be shifted/i).first()).toBeVisible();

  await page.getByRole("button", { name: /Confirm urgent upload/ }).click();

  await page.waitForFunction(
    () =>
      (window as unknown as { __cascadeApplied?: boolean }).__cascadeApplied ===
      true,
  );
  await page.waitForFunction(
    () => (window as unknown as { __uploadCalled?: boolean }).__uploadCalled === true,
  );
});
