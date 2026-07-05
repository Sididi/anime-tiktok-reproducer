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

// All shared events sit at offset 0 (today): ScheduleX only renders the
// current Mon–Sun week, so any positive offset falls off the grid when the
// suite runs on a Sunday.
const EVENTS = [
  {
    project_id: "p1",
    anime_title: "Show Alpha",
    account_id: "acc_a",
    account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "youtube",
    slot: isoForOffset(0, 6, 0),
    scheduled_at: isoForOffset(0, 6, 8),
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
    slot: isoForOffset(0, 9, 0),
    scheduled_at: isoForOffset(0, 9, 14),
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
  // Free chips must be > now+30min, so anchor the target slot tomorrow and
  // drive the picker calendar there (same pattern as the steal test).
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
  // Click "Déplacer" on the first (YouTube) row.
  await page.getByRole("dialog").getByRole("button", { name: /Déplacer/i }).first().click();

  // Navigate the picker calendar to tomorrow so the future slot surfaces.
  const dayLabel = await page.evaluate(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return String(d.getDate());
  });
  await page
    .getByRole("heading", { name: "Pick YT slot" })
    .locator("..")
    .getByRole("button", { name: dayLabel, exact: true })
    .first()
    .click();

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

  // The second event card is Show Beta (TT-only).
  await page
    .locator(".sx__time-grid-event-inner > div")
    .nth(1)
    .click();
  await expect(page.getByRole("dialog", { name: "Event details" })).toBeVisible();
  // Per-platform popover: the only platform row is TT — two-step inline
  // confirmation: "Annuler" arms the button, "Confirmer ?" executes.
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Annuler", exact: true })
    .first()
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Confirmer \?/ })
    .click();
  await page.waitForFunction(
    () => (window as unknown as { __deleted?: boolean }).__deleted === true,
  );
});

// Anchor at offset 0 (today) so the event always lands inside the ScheduleX
// week view regardless of which weekday the suite runs on. (ScheduleX only
// renders the current Mon–Sun week; +1/+2 offsets fall off the grid on Sundays.)
const MANUAL_EVENTS = [
  {
    project_id: "pm",
    anime_title: "Manual Show",
    account_id: "acc_a",
    account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "tiktok",
    slot: isoForOffset(0, 15, 0),
    scheduled_at: isoForOffset(0, 15, 0),
    drive_folder_url: null,
    status: "scheduled",
    manual: true,
  },
];

test("Manual event shows the M badge and manual note in the popover", async ({
  page,
}) => {
  await page.addInitScript(installMocks(MANUAL_EVENTS, ACCOUNTS), [
    MANUAL_EVENTS,
    ACCOUNTS,
  ]);
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  await expect(page.getByText("Manual Show").first()).toBeVisible();

  // The dashed "M" badge on the calendar card carries the manual title.
  await expect(
    page.locator('span[title="Programmation manuelle"]'),
  ).toBeVisible();

  await page.locator(".sx__time-grid-event-inner > div").first().click();
  const dialog = page.getByRole("dialog", { name: "Event details" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/Programmation manuelle/)).toBeVisible();
});

// Mocks a single-platform steal: an occupied YouTube slot the user can grab via
// the switch modal, plus switch-preview / switch-apply. Captures the apply mode.
// `takenIso` is the amber (occupied) slot; `freeIso` a decoy free slot so the
// picker's calendar day isn't struck as "full".
function installStealMocks(events: unknown[], accounts: unknown[]) {
  return ([eventsArg, accountsArg, takenIso, freeIso]: [
    unknown[],
    unknown[],
    string,
    string,
  ]) => {
    const orig = window.fetch.bind(window);
    const json = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), {
        status,
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

      if (path === "/api/accounts") return json({ accounts: accountsArg });
      if (path === "/api/scheduling/events") return json({ events: eventsArg });
      if (path === "/api/scheduling/free-slots") {
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
      if (path.endsWith("/switch-preview") && method === "POST") {
        return json({
          platform: "youtube",
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
                to_slot: takenIso,
                requires_platform_notification: true,
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
                to_slot: takenIso,
                requires_platform_notification: true,
              },
            ],
            blockers: [],
          },
        });
      }
      if (path.endsWith("/switch-apply") && method === "POST") {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          mode?: string;
        };
        // @ts-expect-error inject flag for assertions
        window.__switchMode = body.mode;
        return json({
          platform: "youtube",
          slot: takenIso,
          occupant_project_id: "projB",
          occupant_title: "Naruto",
          uploaded_count: 0,
          cascade: { displaced: [], blockers: [] },
          next_free: { displaced: [], blockers: [] },
          notification_status: {},
        });
      }
      if (path === "/api/anime/source-details") return json([]);
      if (path === "/api/anime/jobs/stream") return emptyStream();
      if (path === "/api/projects/startup/jobs") return json({ jobs: [] });
      if (path === "/api/projects/startup/jobs/stream") return emptyStream();
      return orig(input, init);
    };
  };
}

const STEAL_EVENTS = [
  {
    project_id: "p1",
    anime_title: "Show Alpha",
    account_id: "acc_a",
    account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "youtube",
    slot: isoForOffset(0, 15, 0),
    scheduled_at: isoForOffset(0, 15, 0),
    drive_folder_url: "https://drive.example/p1",
    status: "scheduled",
    manual: false,
  },
];

test("Single-platform steal applies a cascade switch", async ({ page }) => {
  // Anchor the occupied + decoy slots to tomorrow so they're future (a stealable
  // amber chip requires > now+30min); the picker has its own month calendar, so
  // it doesn't depend on the ScheduleX current-week constraint.
  const tomorrowAt = (hourUtc: number): string => {
    const d = new Date();
    d.setUTCHours(hourUtc, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString();
  };
  const takenIso = tomorrowAt(12);
  const freeIso = tomorrowAt(16);
  await page.addInitScript(installStealMocks(STEAL_EVENTS, ACCOUNTS), [
    STEAL_EVENTS,
    ACCOUNTS,
    takenIso,
    freeIso,
  ]);
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  await expect(page.getByText("Show Alpha").first()).toBeVisible();

  await page.locator(".sx__time-grid-event-inner > div").first().click();
  await expect(page.getByRole("dialog", { name: "Event details" })).toBeVisible();
  // Déplacer on the YouTube row → single-platform slot picker.
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Déplacer/i })
    .first()
    .click();

  // Drive the picker calendar to tomorrow so the future taken/free slots surface.
  const dayLabel = await page.evaluate(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return String(d.getDate());
  });
  await page
    .getByRole("heading", { name: "Pick YT slot" })
    .locator("..")
    .getByRole("button", { name: dayLabel, exact: true })
    .first()
    .click();

  // The occupied slot renders as an amber, clickable chip.
  const amberChip = page.locator('button[title*="Occupé par"]');
  await expect(amberChip).toBeVisible();
  await amberChip.click();

  // Switch modal → choose the chained cascade.
  await page.getByRole("button", { name: /Cascader/ }).click();
  // Commit the single-platform reschedule with the steal encoded.
  await page.getByRole("button", { name: "Schedule", exact: true }).click();

  await page.waitForFunction(
    () =>
      (window as unknown as { __switchMode?: string }).__switchMode ===
      "cascade",
  );
});
