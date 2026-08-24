import { expect, test } from "@playwright/test";
import { installEventHubMock } from "./helpers/eventHubMock";

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

function installMocks(payload: {
  account: typeof ACCOUNT;
  row: typeof ROW;
  failingPreflight?: "copyright" | "facebook" | "instagram" | "youtube";
}) {
  const { account, row, failingPreflight } = payload;
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

    if (url.pathname === "/api/accounts") {
      return json({ accounts: [account] });
    }
    if (url.pathname === "/api/project-manager/projects") {
      return json({ projects: [row] });
    }
    if (url.pathname === "/api/project-manager/upload-jobs") {
      return json({ jobs: [] });
    }
    if (url.pathname === "/api/anime/source-details") {
      return json([]);
    }
    if (url.pathname === "/api/projects/startup/jobs") {
      return json({ jobs: [] });
    }
    if (url.pathname.endsWith("/copyright-check")) {
      if (failingPreflight === "copyright") {
        return new Response(
          JSON.stringify({ detail: "Drive metadata temporarily unavailable" }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        );
      }
      return json({ copyrighted: false });
    }
    if (
      url.pathname.endsWith("/facebook-check") ||
      url.pathname.endsWith("/instagram-check") ||
      url.pathname.endsWith("/youtube-check")
    ) {
      if (failingPreflight && url.pathname.endsWith(`/${failingPreflight}-check`)) {
        return new Response(
          JSON.stringify({ detail: "Drive metadata temporarily unavailable" }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        );
      }
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
  await page.addInitScript(installEventHubMock, {});
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

test("failed preflight clears Checking and never queues an upload", async ({
  page,
}) => {
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installMocks, {
    account: ACCOUNT,
    row: ROW,
    failingPreflight: "facebook",
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
  await expect(projectRow).toBeVisible();
  await page.getByRole("button", { name: "All Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();
  await projectRow.getByRole("button", { name: /^Upload$/ }).click();

  await expect(page.getByText("Drive metadata temporarily unavailable")).toBeVisible();
  await expect(projectRow.getByRole("button", { name: /^Upload$/ })).toBeVisible();
  expect(
    await page.evaluate(
      () => (window as unknown as { __uploadCalled?: boolean }).__uploadCalled,
    ),
  ).toBe(false);
});

test("closing Project Manager unsubscribes from upload job events", async ({
  page,
}) => {
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
  await page.goto("/");

  const uploadSubscriptions = () =>
    page.evaluate(
      () =>
        (
          window as unknown as {
            __atrEventHub?: {
              debugSnapshot(): { subscriptions: Record<string, number> };
            };
          }
        ).__atrEventHub?.debugSnapshot().subscriptions.upload_jobs ?? 0,
    );

  await expect.poll(uploadSubscriptions).toBe(0);
  await page.getByRole("button", { name: "Projects" }).click();
  // The modal subscribes while open; the browser-wide stream itself stays up.
  await expect.poll(uploadSubscriptions).toBe(1);

  await page.keyboard.press("Escape");
  await expect.poll(uploadSubscriptions).toBe(0);
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

test("Schedule mode queues upload when checks resolve immediately", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installSchedulingMocks);
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
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

function installUrgentMocks(payload: {
  withCollisions?: boolean;
}) {
  const { withCollisions } = payload;
  const testWindow = window as Window &
    typeof globalThis & {
      __urgentApplied?: boolean;
      __urgentApplyBody?: unknown;
      __uploadBody?: unknown;
    };
  testWindow.__urgentApplied = false;
  const inThirty = new Date(Date.now() + 30 * 60 * 1000).toISOString();
  const inForty = new Date(Date.now() + 40 * 60 * 1000).toISOString();
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
    if (url.pathname.endsWith("/urgent-preview") && init?.method === "POST") {
      const req = JSON.parse(String(init?.body ?? "{}")) as {
        tiktok_only?: boolean;
      };
      if (!withCollisions) {
        return json({
          window_minutes: 60,
          platforms: ["youtube", "tiktok"],
          immediate_platforms: req.tiktok_only ? ["tiktok"] : ["youtube", "tiktok"],
          phase1: [],
          phase2: [],
        });
      }
      return json({
        window_minutes: 60,
        platforms: ["youtube", "tiktok"],
        immediate_platforms: req.tiktok_only ? ["tiktok"] : ["youtube", "tiktok"],
        phase1: [
          {
            project_id: "near1",
            anime_title: "Near TT",
            account_id: "acc_a",
            items: [
              {
                platform: "tiktok",
                slot: inThirty,
                scheduled_at: inThirty,
                manual: false,
                movable: true,
                reason: null,
                best_effort: false,
                suggested_slot: null,
              },
            ],
          },
        ],
        phase2: req.tiktok_only
          ? []
          : [
              {
                project_id: "near2",
                anime_title: "Near IG",
                account_id: "acc_a",
                items: [
                  {
                    platform: "instagram",
                    slot: inForty,
                    scheduled_at: inForty,
                    manual: false,
                    movable: false,
                    reason: "unmovable_processing",
                    best_effort: false,
                    suggested_slot: null,
                  },
                ],
              },
            ],
      });
    }
    if (url.pathname.endsWith("/urgent-apply") && init?.method === "POST") {
      testWindow.__urgentApplied = true;
      testWindow.__urgentApplyBody = JSON.parse(String(init?.body ?? "{}"));
      return json({ shifts: [], own_schedules: {} });
    }
    if (url.pathname.endsWith("/upload") && init?.method === "POST") {
      // This wrapper runs before installMocks' handler (it was installed
      // after, so it is outermost): capture the body and answer directly.
      testWindow.__uploadBody = JSON.parse(String(init?.body ?? "{}"));
      (window as unknown as { __uploadCalled?: boolean }).__uploadCalled = true;
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

test("Urgent immediate: no collisions → apply then upload with immediate flag", async ({
  page,
}) => {
  // installMocks first so the urgent mocks wrap LAST (and therefore run
  // FIRST at fetch time — their /upload handler must capture the body).
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
  await page.addInitScript(installUrgentMocks, { withCollisions: false });
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
  await expect(projectRow).toBeVisible();

  await page.getByRole("button", { name: "All Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();

  await projectRow.getByRole("button", { name: "Upload options" }).click();
  await page.getByRole("button", { name: /Upload urgently \(immediate\)/ }).click();

  await expect(
    page.getByText(/Aucun upload prévu dans l'heure à venir/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continuer" }).click();

  // The urgent-apply call fires only at the very end (after the preflight
  // checks), immediately before the upload request.
  await page.waitForFunction(
    () =>
      (window as unknown as { __urgentApplied?: boolean }).__urgentApplied === true,
  );
  await page.waitForFunction(
    () => (window as unknown as { __uploadCalled?: boolean }).__uploadCalled === true,
  );
  const uploadBody = await page.evaluate(
    () => (window as unknown as { __uploadBody?: unknown }).__uploadBody,
  );
  expect(uploadBody).toMatchObject({ immediate: true, immediate_platforms: null });
});

test("Urgent immediate: collisions render both phases; movable ones gate Continue", async ({
  page,
}) => {
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
  await page.addInitScript(installUrgentMocks, { withCollisions: true });
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();

  const projectRow = page.locator("tr").filter({ hasText: "Show Alpha" });
  await expect(projectRow).toBeVisible();
  await page.getByRole("button", { name: "All Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();

  await projectRow.getByRole("button", { name: "Upload options" }).click();
  await page.getByRole("button", { name: /Upload urgently \(immediate\)/ }).click();

  // Phase 1 lists the movable TikTok collision, phase 2 the unmovable one.
  await expect(page.getByText(/1\. TikTok — uploads à moins de 60 min/)).toBeVisible();
  await expect(page.getByText("Near TT")).toBeVisible();
  await expect(page.getByText(/en cours de publication — publiera quand même/)).toBeVisible();
  // A movable collision without a recorded re-timing blocks Continue.
  await expect(page.getByRole("button", { name: "Continuer" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Re-planifier" })).toBeVisible();
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
  // Paris-LOCAL hours (the test pins timezoneId Europe/Paris): computing
  // "tomorrow" in UTC breaks between 00:00–02:00 Paris, when the Paris day is
  // already one ahead of the UTC day — the mocked slots would land on the
  // Paris day BEFORE the one clickTomorrow selects.
  const tomorrowAt = (hourLocal: number): string => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(hourLocal, 0, 0, 0);
    return d.toISOString();
  };
  const takenIso = tomorrowAt(14); // 14:00 Paris
  const freeIso = tomorrowAt(18); // 18:00 Paris
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
        relocate: {
          displaced: [
            {
              project_id: "projB",
              anime_title: "Naruto",
              from_slot: takenIso,
              to_slot: freeIso,
              requires_platform_notification: true,
              platform: "tiktok",
            },
            {
              project_id: "projB",
              anime_title: "Naruto",
              from_slot: takenIso,
              to_slot: freeIso,
              requires_platform_notification: false,
              platform: "youtube",
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
    await page.addInitScript(installEventHubMock, {});
    await page.addInitScript(installManualMocks);
    await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
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

  test("Amber chip opens takeover modal and reserves with a relocate steal", async ({
    page,
  }) => {
    await page.addInitScript(installEventHubMock, {});
    await page.addInitScript(installStealAnchorMocks);
    await page.addInitScript(installMocks, { account: ACCOUNT, row: ROW });
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

    // Takeover modal: the occupant's new TikTok-first timings render
    // (single relocate strategy — the old cascade/next-free choice is gone).
    await expect(
      page.getByText(/Nouveaux horaires de «Naruto»/),
    ).toBeVisible();
    await expect(page.getByText(/↳ TT ·/)).toBeVisible();
    await expect(page.getByText(/↳ YT ·/)).toBeVisible();

    await page.getByRole("button", { name: "Libérer le slot" }).click();

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
        tiktok: { mode: "relocate", expected_occupant_id: "projB" },
      },
    });
  });
});
