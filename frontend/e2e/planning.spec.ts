import { expect, test } from "@playwright/test";

const ACCOUNTS = [
  {
    id: "acc_a",
    name: "Account A",
    language: "fr",
    avatar_url: "/api/accounts/acc_a/avatar",
    supported_types: ["anime"],
    slots: ["14:00", "18:00"],
  },
];

// Build event slots near "now" so they fall inside the rendered week regardless
// of when the test executes. ScheduleX week view shows the current week.
function isoForOffset(dayOffset: number, hour: number, minute = 0): string {
  const d = new Date();
  d.setUTCHours(hour, minute, 0, 0);
  d.setUTCDate(d.getUTCDate() + dayOffset);
  return d.toISOString();
}

const EVENTS = [
  {
    project_id: "p1",
    anime_title: "Show Alpha",
    account_id: "acc_a",
    account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "youtube",
    slot: isoForOffset(1, 12, 0),
    scheduled_at: isoForOffset(1, 12, 8),
    drive_folder_url: "https://drive.example/p1",
    status: "scheduled",
  },
  {
    project_id: "p2",
    anime_title: "Show Beta",
    account_id: "acc_a",
    account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "tiktok",
    slot: isoForOffset(2, 16, 0),
    scheduled_at: isoForOffset(2, 16, 14),
    drive_folder_url: null,
    status: "scheduled",
  },
];

function installMocks(events: unknown[], accounts: unknown[]) {
  return ([eventsArg, accountsArg]: [unknown[], unknown[]]) => {
    const orig = window.fetch.bind(window);
    const jsonResponse = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const emptyEventStream = () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(requestUrl, window.location.origin);
      const path = url.pathname;

      if (path === "/api/accounts") {
        return jsonResponse({ accounts: accountsArg });
      }
      if (path === "/api/scheduling/events") {
        return jsonResponse({ events: eventsArg });
      }
      if (path === "/api/anime/source-details") {
        return jsonResponse([]);
      }
      if (path === "/api/anime/jobs/stream") {
        return emptyEventStream();
      }
      if (path === "/api/projects/startup/jobs") {
        return jsonResponse({ jobs: [] });
      }
      if (path === "/api/projects/startup/jobs/stream") {
        return emptyEventStream();
      }
      return orig(input, init);
    };

    // expose for assertions
    void eventsArg;
  };
}

test("Planning modal opens and shows mocked events", async ({ page }) => {
  await page.addInitScript(installMocks(EVENTS, ACCOUNTS), [EVENTS, ACCOUNTS]);
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  // ScheduleX renders event titles inside the time-grid cells.
  await expect(page.getByText("Show Alpha").first()).toBeVisible();
  await expect(page.getByText("Show Beta").first()).toBeVisible();
});

test("Planning modal filters by platform", async ({ page }) => {
  await page.addInitScript(installMocks(EVENTS, ACCOUNTS), [EVENTS, ACCOUNTS]);
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  await expect(page.getByText("Show Alpha").first()).toBeVisible();

  // Click the YouTube chip — its accessible name resolves to the title attribute.
  const youtubeChip = page.getByRole("button", { name: "YT", exact: true });
  await expect(youtubeChip).toHaveAttribute("aria-pressed", "true");
  await youtubeChip.click();
  await expect(youtubeChip).toHaveAttribute("aria-pressed", "false");
});

function installMocksWithMutations(events: unknown[], accounts: unknown[]) {
  return ([eventsArg, accountsArg, freeSlotIso]: [
    unknown[],
    unknown[],
    string,
  ]) => {
    const orig = window.fetch.bind(window);
    const jsonResponse = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    const emptyEventStream = () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(requestUrl, window.location.origin);
      const path = url.pathname;
      const method = (init?.method ?? "GET").toUpperCase();

      if (path === "/api/accounts") {
        return jsonResponse({ accounts: accountsArg });
      }
      if (path === "/api/scheduling/events") {
        return jsonResponse({ events: eventsArg });
      }
      if (path === "/api/scheduling/free-slots") {
        return jsonResponse({
          slots: [{ slot: freeSlotIso, available: true }],
        });
      }
      if (
        path.startsWith("/api/scheduling/projects/") &&
        path.endsWith("/platforms/youtube") &&
        method === "PATCH"
      ) {
        // @ts-expect-error inject flag for assertions
        window.__patched = true;
        return jsonResponse({
          slot: freeSlotIso,
          scheduled_at: freeSlotIso,
          notification_status: "ok",
        });
      }
      if (
        path.startsWith("/api/scheduling/projects/") &&
        path.endsWith("/platforms/tiktok") &&
        method === "DELETE"
      ) {
        // @ts-expect-error inject flag for assertions
        window.__deleted = true;
        return new Response(null, { status: 204 });
      }
      if (path === "/api/anime/source-details") {
        return jsonResponse([]);
      }
      if (path === "/api/anime/jobs/stream") {
        return emptyEventStream();
      }
      if (path === "/api/projects/startup/jobs") {
        return jsonResponse({ jobs: [] });
      }
      if (path === "/api/projects/startup/jobs/stream") {
        return emptyEventStream();
      }
      return orig(input, init);
    };
  };
}

test("Reschedule slot triggers PATCH and reloads", async ({ page }) => {
  // Pick a slot ISO inside the day of the existing YouTube event so the
  // SlotPickerPopover surfaces it after fetching free-slots for that day.
  const targetSlotIso = isoForOffset(1, 14, 0);
  await page.addInitScript(installMocksWithMutations(EVENTS, ACCOUNTS), [
    EVENTS,
    ACCOUNTS,
    targetSlotIso,
  ]);
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  await expect(page.getByText("Show Alpha").first()).toBeVisible();

  // The React custom component now binds onClick directly on its inner div.
  await page
    .locator(".sx__time-grid-event-inner > div")
    .first()
    .click();
  await expect(page.getByRole("dialog", { name: "Event details" })).toBeVisible();
  // Click "Move" on the first (YouTube) row.
  await page.getByRole("dialog").getByRole("button", { name: /Move/i }).first().click();

  // SlotPickerPopover now drives the flow — pick the offered chip then Schedule.
  const expectedLabel = new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Paris",
  }).format(new Date(targetSlotIso));
  await page
    .getByRole("button", { name: new RegExp(`^${expectedLabel}$`) })
    .first()
    .click();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();

  await page.waitForFunction(
    () => (window as unknown as { __patched?: boolean }).__patched === true,
  );
});

test("Cancel slot triggers DELETE", async ({ page }) => {
  await page.addInitScript(installMocksWithMutations(EVENTS, ACCOUNTS), [
    EVENTS,
    ACCOUNTS,
    isoForOffset(2, 16, 0),
  ]);
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  await expect(page.getByText("Show Beta").first()).toBeVisible();

  page.on("dialog", (dlg) => dlg.accept());

  // The second event card is Show Beta (TT-only).
  await page
    .locator(".sx__time-grid-event-inner > div")
    .nth(1)
    .click();
  await expect(page.getByRole("dialog", { name: "Event details" })).toBeVisible();
  // Per-platform popover: the only platform row is TT — click its "Cancel".
  await page.getByRole("dialog").getByRole("button", { name: /Cancel/i }).first().click();
  await page.waitForFunction(
    () => (window as unknown as { __deleted?: boolean }).__deleted === true,
  );
});
