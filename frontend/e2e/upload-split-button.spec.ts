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
  // Anchor mock dates to "tomorrow" so the picker never marks them as past
  // (`isPast` disables past days in the calendar). Helpers live inside the
  // function so `addInitScript` serializes them with the page.
  const tomorrowUtc = (() => {
    const d = new Date();
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() + 1);
    return d;
  })();
  const tomorrowAt = (hour: number, minute = 0): string => {
    const d = new Date(tomorrowUtc);
    d.setUTCHours(hour, minute, 0, 0);
    return d.toISOString();
  };
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
          { slot: tomorrowAt(12, 0), available: true },
          { slot: tomorrowAt(16, 0), available: true },
        ],
      });
    }
    if (url.pathname === "/api/scheduling/resolve-anchor") {
      return json({
        resolved: {
          tiktok: {
            slot: tomorrowAt(12, 0),
            scheduled_at: tomorrowAt(12, 8),
            available: true,
          },
          youtube: {
            slot: tomorrowAt(12, 0),
            scheduled_at: tomorrowAt(12, 9),
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
  // Mocked slots are seeded for tomorrow so the calendar's `isPast` rule
  // never disables them.
  const dayLabel = await page.evaluate(() => {
    const d = new Date();
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() + 1);
    return String(d.getUTCDate());
  });
  await page
    .getByRole("heading", { name: "Pick a slot" })
    .locator("..")
    // A trailing off-month cell can share the same day number, so scope to the
    // first (in-month) match.
    .getByRole("button", { name: dayLabel, exact: true })
    .first()
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

// Helper: the day-cell label the calendar renders for "tomorrow". The calendar
// prints `date.getDate()`; we anchor mocked slots to tomorrow so `isPast` never
// disables them.
async function clickTomorrow(page: import("@playwright/test").Page) {
  const dayLabel = await page.evaluate(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return String(d.getDate());
  });
  await page
    .getByRole("heading", { name: "Pick a slot" })
    .locator("..")
    .getByRole("button", { name: dayLabel, exact: true })
    .first()
    .click();
}

// Mocks for the manual custom-time flow. Captures the `at` sent to
// reserve-manual so the test can assert the exact instant. free-slots returns
// no chips (custom time doesn't need any) — the day is still selectable because
// `isFull` only strikes days that HAVE configured-but-taken slots.
function installManualMocks() {
  const testWindow = window as Window &
    typeof globalThis & { __manualAt?: string };
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
    if (url.pathname === "/api/scheduling/free-slots") {
      return json({ slots: [] });
    }
    if (url.pathname.includes("/reserve-manual") && init?.method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as { at?: string };
      testWindow.__manualAt = body.at;
      return json({
        platform_schedules: {
          tiktok: {
            slot: body.at,
            scheduled_at: body.at,
            manual: true,
          },
        },
        notification_status: {},
      });
    }
    return orig(input, init);
  };
}

// Mocks for the amber-chip → switch modal → reserve-anchor steal flow. Anchors a
// taken slot (occupied by projB / "Naruto") plus a decoy free slot to tomorrow.
function installStealAnchorMocks() {
  const tomorrowAt = (hourUtc: number): string => {
    const d = new Date();
    d.setUTCHours(hourUtc, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString();
  };
  const takenIso = tomorrowAt(12); // 14:00 Paris
  const freeIso = tomorrowAt(16); // 18:00 Paris
  const testWindow = window as Window &
    typeof globalThis & {
      __anchorBody?: unknown;
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
    if (url.pathname === "/api/scheduling/free-slots") {
      return json({
        slots: [
          {
            slot: takenIso,
            available: false,
            taken_by_project_id: "projB",
            taken_by_title: "Naruto",
          },
          { slot: freeIso, available: true },
        ],
      });
    }
    if (url.pathname.endsWith("/switch-preview") && init?.method === "POST") {
      return json({
        platform: "tiktok",
        slot: takenIso,
        occupant_project_id: "projB",
        occupant_title: "Naruto",
        uploaded_count: 0,
        cascade: {
          displaced: [
            {
              project_id: "projB",
              anime_title: "Naruto",
              from_slot: takenIso,
              to_slot: freeIso,
              requires_platform_notification: true,
            },
            {
              project_id: "projC",
              anime_title: "Bleach",
              from_slot: freeIso,
              to_slot: tomorrowAt(20),
              requires_platform_notification: false,
            },
          ],
          blockers: [],
        },
        next_free: {
          displaced: [
            {
              project_id: "projB",
              anime_title: "Naruto",
              from_slot: takenIso,
              to_slot: freeIso,
              requires_platform_notification: true,
            },
          ],
          blockers: [],
        },
      });
    }
    if (url.pathname === "/api/scheduling/resolve-anchor") {
      return json({
        resolved: {
          tiktok: { slot: takenIso, scheduled_at: takenIso, available: true },
        },
        conflicts: [],
      });
    }
    if (url.pathname.includes("/reserve-anchor") && init?.method === "POST") {
      testWindow.__anchorBody = JSON.parse(String(init?.body ?? "{}"));
      testWindow.__anchored = true;
      return json({
        platform_schedules: {
          tiktok: { slot: takenIso, scheduled_at: takenIso },
        },
      });
    }
    return orig(input, init);
  };
}

test.describe("manual custom-time + slot switching", () => {
  // Pin the browser timezone so `17:23` maps to a fixed UTC instant regardless
  // of the CI host clock. Europe/Paris in July (CEST) = UTC+2 → 15:23Z.
  test.use({ timezoneId: "Europe/Paris" });

  test("Custom time schedules a manual reservation", async ({ page }) => {
    await page.addInitScript(installManualMocks);
    await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
    await page.addInitScript(installCheckDelay);
    await page.goto("/");
    await page.getByRole("button", { name: "Projects" }).click();

    const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
    await expect(projectRow).toBeVisible();

    await page.getByRole("button", { name: "All Projects" }).click();
    await page.getByRole("button", { name: "Account A" }).click();

    await projectRow.getByRole("button", { name: "Upload options" }).click();
    await page
      .getByRole("button", { name: /Schedule for specific slot/ })
      .click();

    await clickTomorrow(page);

    // Tick "Heure personnalisée" → the auto-resolve / override sections vanish
    // and the submit button flips to the manual label.
    await page.getByRole("checkbox").check();
    await expect(page.getByText("Other platforms (auto)")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Programmer (manuel)" }),
    ).toBeVisible();

    await page.locator('input[type="time"]').fill("17:23");
    await page.getByRole("button", { name: "Programmer (manuel)" }).click();

    await page.waitForFunction(() =>
      typeof (window as unknown as { __manualAt?: string }).__manualAt ===
      "string",
    );
    const at = await page.evaluate(
      () => (window as unknown as { __manualAt?: string }).__manualAt,
    );
    expect(at).toMatch(/T15:23:00\.000Z$/);
  });

  test("Amber chip opens switch modal and reserves with a next-free steal", async ({
    page,
  }) => {
    await page.addInitScript(installStealAnchorMocks);
    await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
    await page.addInitScript(installCheckDelay);
    await page.goto("/");
    await page.getByRole("button", { name: "Projects" }).click();

    const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
    await expect(projectRow).toBeVisible();

    await page.getByRole("button", { name: "All Projects" }).click();
    await page.getByRole("button", { name: "Account A" }).click();

    await projectRow.getByRole("button", { name: "Upload options" }).click();
    await page
      .getByRole("button", { name: /Schedule for specific slot/ })
      .click();

    await clickTomorrow(page);

    // The occupied slot renders as an amber, still-clickable chip.
    const amberChip = page.locator('button[title*="Occupé par"]');
    await expect(amberChip).toBeVisible();
    await expect(amberChip).toHaveClass(/border-amber-500\/60/);
    await expect(amberChip).toBeEnabled();
    await amberChip.click();

    // Switch modal: both displacement plans render.
    await expect(
      page.getByText(/Cascade en chaîne — 2 vidéos déplacées/),
    ).toBeVisible();
    await expect(
      page.getByText(/Prochain slot libre — 1 vidéo déplacée/),
    ).toBeVisible();

    await page
      .getByRole("button", { name: /Slot libre suivant \(1 vidéo\)/ })
      .click();

    // Completing the schedule reserves the anchor with the encoded steal.
    await page.getByRole("button", { name: "Schedule", exact: true }).click();

    await page.waitForFunction(
      () => (window as unknown as { __anchored?: boolean }).__anchored === true,
    );
    const body = await page.evaluate(
      () => (window as unknown as { __anchorBody?: unknown }).__anchorBody,
    );
    expect(body).toMatchObject({
      steals: {
        tiktok: { mode: "next_free", expected_occupant_id: "projB" },
      },
    });
  });
});
