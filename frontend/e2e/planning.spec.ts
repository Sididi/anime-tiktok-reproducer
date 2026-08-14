import { expect, test, type Page } from "@playwright/test";

/**
 * Planning board e2e — all backend traffic is mocked at the network layer
 * (page.route), so these tests exercise the real data layer (React Query),
 * the board, the details panel, the precedence dialog and quick-assign.
 */

const ACCOUNTS = [
  {
    id: "acc_a",
    name: "Account A",
    language: "fr",
    avatar_url: "/api/accounts/acc_a/avatar",
    supported_types: ["anime"],
    slots: ["14:00", "18:00"],
  },
  {
    id: "acc_b",
    name: "Account B",
    language: "en",
    avatar_url: "/api/accounts/acc_b/avatar",
    supported_types: ["anime"],
    slots: ["12:00"],
  },
];

/** An instant `hoursFromNow` in the future (keeps events inside the current week view when small). */
function futureIso(hoursFromNow: number): string {
  return new Date(Date.now() + hoursFromNow * 3600_000).toISOString();
}

const SLOT_P1 = futureIso(2);
const SLOT_GHOST = futureIso(4);

function makeEvents() {
  return [
    {
      project_id: "p1",
      anime_title: "Show Alpha",
      account_id: "acc_a",
      account_avatar_url: "/api/accounts/acc_a/avatar",
      account_name: "Account A",
      platform: "tiktok",
      slot: SLOT_P1,
      scheduled_at: SLOT_P1,
      drive_folder_url: "https://drive.example/p1",
      status: "scheduled",
      manual: false,
      timing_locked: false,
    },
    {
      project_id: "p1",
      anime_title: "Show Alpha",
      account_id: "acc_a",
      account_avatar_url: "/api/accounts/acc_a/avatar",
      account_name: "Account A",
      platform: "youtube",
      slot: SLOT_P1,
      scheduled_at: SLOT_P1,
      drive_folder_url: "https://drive.example/p1",
      status: "scheduled",
      manual: false,
      timing_locked: false,
    },
  ];
}

interface MockState {
  events: ReturnType<typeof makeEvents>;
  patchResponses: Array<{ status: number; body: unknown }>;
  patchBodies: unknown[];
  deleteResponse: { status: number; body?: unknown };
  deleteCount: number;
  reserveBodies: unknown[];
}

async function installMocks(page: Page, state: MockState) {
  // Catch-all first: Playwright matches the MOST RECENTLY registered route,
  // so specific handlers below take precedence. Regex anchored to the URL
  // path start — a glob like **/api/** would also swallow Vite module URLs
  // such as /src/api/client.ts and blank the whole app.
  await page.route(/^https?:\/\/[^/]+\/api\//, (route) =>
    route.fulfill({ json: {}, status: 200 }),
  );
  await page.route("**/api/accounts", (route) =>
    route.fulfill({ json: { accounts: ACCOUNTS } }),
  );
  await page.route("**/api/accounts/*/avatar", (route) =>
    route.fulfill({ status: 404, body: "" }),
  );
  await page.route("**/api/scheduling/events*", (route) =>
    route.fulfill({ json: { events: state.events } }),
  );
  // Register free-slots BEFORE free-slots-range: most-recent wins, and the
  // "free-slots*" glob would otherwise shadow the range endpoint's URL.
  await page.route("**/api/scheduling/free-slots?*", (route) =>
    route.fulfill({
      json: {
        slots: [
          { slot: SLOT_GHOST, available: true },
          { slot: futureIso(28), available: true },
        ],
      },
    }),
  );
  await page.route("**/api/scheduling/free-slots-range*", (route) =>
    route.fulfill({
      json: {
        slots: {
          tiktok: [{ slot: SLOT_GHOST, available: true }],
          youtube: [],
          facebook: [],
          instagram: [],
        },
      },
    }),
  );
  await page.route("**/api/scheduling/projects/*/platforms/*", (route) => {
    if (route.request().method() === "PATCH") {
      state.patchBodies.push(route.request().postDataJSON());
      const next = state.patchResponses.shift() ?? {
        status: 200,
        body: { slot: SLOT_GHOST, scheduled_at: SLOT_GHOST, notification_status: "ok" },
      };
      return route.fulfill({ status: next.status, json: next.body as object });
    }
    if (route.request().method() === "DELETE") {
      state.deleteCount += 1;
      return route.fulfill({
        status: state.deleteResponse.status,
        json: (state.deleteResponse.body as object) ?? {},
      });
    }
    return route.fulfill({ json: {} });
  });
  await page.route("**/api/project-manager/projects", (route) =>
    route.fulfill({
      json: {
        projects: [
          {
            project_id: "ready1",
            anime_title: "Ready Project",
            language: "fr",
            can_upload_status: "green",
            uploaded_status: "red",
            scheduled_at: null,
          },
          {
            project_id: "not-ready",
            anime_title: "Not Ready",
            language: "fr",
            can_upload_status: "orange",
            uploaded_status: "red",
            scheduled_at: null,
          },
        ],
      },
    }),
  );
  await page.route("**/api/project-manager/projects/*/upload-restrictions", (route) =>
    route.fulfill({
      json: {
        mother_project_id: null,
        family_project_ids: [],
        blocked_accounts: [],
        blocked_windows: [],
        min_spacing_days: 30,
      },
    }),
  );
  await page.route("**/api/scheduling/resolve-anchor", (route) =>
    route.fulfill({
      json: {
        resolved: {
          tiktok: { slot: SLOT_GHOST, scheduled_at: SLOT_GHOST, available: true },
        },
        conflicts: [],
      },
    }),
  );
  await page.route("**/api/scheduling/projects/*/reserve-anchor", (route) => {
    state.reserveBodies.push(route.request().postDataJSON());
    return route.fulfill({
      json: { platform_schedules: {}, notification_status: {} },
    });
  });
}

function defaultState(): MockState {
  return {
    events: makeEvents(),
    patchResponses: [],
    patchBodies: [],
    deleteResponse: { status: 204 },
    deleteCount: 0,
    reserveBodies: [],
  };
}

async function openPlanning(page: Page, state: MockState, accountId = "acc_a") {
  await installMocks(page, state);
  await page.addInitScript((acc) => {
    localStorage.setItem("atr.planning.account_id", acc);
    localStorage.setItem(
      "atr.planning.platforms",
      JSON.stringify(["youtube", "facebook", "instagram", "tiktok"]),
    );
  }, accountId);
  await page.goto("/planning");
}

test.describe("Planning board", () => {
  test("renders grouped event cards and free-slot ghosts", async ({ page }) => {
    const state = defaultState();
    await openPlanning(page, state);

    const card = page.getByTestId("planning-event-card");
    await expect(card).toHaveCount(1); // p1 tiktok+youtube grouped at one instant
    await expect(card).toContainText("Show Alpha");
    await expect(card).toContainText("TT");
    await expect(card).toContainText("YT");
    await expect(page.getByTestId("planning-ghost-slot")).toHaveCount(1);
  });

  test("hides ghosts and shows a hint when no account is selected", async ({ page }) => {
    const state = defaultState();
    await installMocks(page, state);
    await page.addInitScript(() => {
      localStorage.setItem("atr.planning.account_id", "null");
    });
    await page.goto("/planning");

    await expect(page.getByTestId("planning-event-card")).toHaveCount(1);
    await expect(page.getByTestId("planning-ghost-slot")).toHaveCount(0);
    await expect(
      page.getByText("Sélectionnez un compte pour afficher les créneaux libres"),
    ).toBeVisible();
  });

  test("switches views and keeps the view in the URL", async ({ page }) => {
    const state = defaultState();
    await openPlanning(page, state);

    await page.getByRole("tab", { name: "Agenda" }).click();
    await expect(page).toHaveURL(/view=agenda/);
    await expect(page.getByText("Aujourd'hui").first()).toBeVisible();

    await page.getByRole("tab", { name: "Mois" }).click();
    await expect(page).toHaveURL(/view=month/);
  });

  test("details panel shows per-platform rows and cancel errors surface", async ({ page }) => {
    const state = defaultState();
    state.deleteResponse = { status: 409, body: { detail: "pool_busy" } };
    await openPlanning(page, state);

    await page.getByTestId("planning-event-card").click();
    const panel = page.getByTestId("planning-details-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("TikTok");
    await expect(panel).toContainText("YouTube");

    // Cancel one platform: 409 from the backend must surface, not vanish.
    await panel.getByRole("button", { name: "Annuler", exact: true }).first().click();
    await page.getByRole("button", { name: "Annuler le créneau" }).click();
    await expect(page.getByText(/Échec de l'annulation.*pool_busy/)).toBeVisible();
    // The event card is still there (nothing silently removed).
    await expect(page.getByTestId("planning-event-card")).toHaveCount(1);
  });

  test("precedence 409 opens the dialog and retries with confirm_before_tiktok", async ({ page }) => {
    const state = defaultState();
    state.patchResponses = [
      { status: 409, body: { detail: "tiktok_precedence" } },
      {
        status: 200,
        body: { slot: SLOT_GHOST, scheduled_at: SLOT_GHOST, notification_status: "ok" },
      },
    ];
    await openPlanning(page, state);

    await page.getByTestId("planning-event-card").click();
    const panel = page.getByTestId("planning-details-panel");
    // Reschedule the YouTube row via the slot picker.
    await panel.getByRole("button", { name: "Déplacer" }).nth(1).click();
    // A slot chip's accessible name is exactly its Paris "HH:MM".
    const ghostTimeParis = new Intl.DateTimeFormat("fr-FR", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/Paris",
    }).format(new Date(SLOT_GHOST));
    await page.getByRole("button", { name: ghostTimeParis, exact: true }).click();
    await page.getByRole("button", { name: "Schedule" }).click();

    // Precedence dialog appears; confirming retries with the flag.
    await expect(page.getByText("TikTok doit publier en premier")).toBeVisible();
    await page.getByRole("button", { name: "Continuer" }).click();
    await expect
      .poll(() => state.patchBodies.length, { timeout: 10_000 })
      .toBe(2);
    expect(
      (state.patchBodies[1] as { confirm_before_tiktok: boolean }).confirm_before_tiktok,
    ).toBe(true);
  });

  test("quick-assign reserves a ready project on a ghost slot", async ({ page }) => {
    const state = defaultState();
    await openPlanning(page, state);

    await page.getByTestId("planning-ghost-slot").click();
    const panel = page.getByTestId("planning-quick-assign-panel");
    await expect(panel).toBeVisible();
    // Only the green+unscheduled project is listed.
    await expect(panel).toContainText("Ready Project");
    await expect(panel).not.toContainText("Not Ready");

    await panel.getByText("Ready Project").click();
    await expect(panel.getByRole("button", { name: "Réserver ce créneau" })).toBeEnabled();
    await panel.getByRole("button", { name: "Réserver ce créneau" }).click();

    await expect.poll(() => state.reserveBodies.length).toBe(1);
    const body = state.reserveBodies[0] as { account_id: string; tiktok_slot: string };
    expect(body.account_id).toBe("acc_a");
    expect(body.tiktok_slot).toBe(SLOT_GHOST);
    await expect(page.getByText(/Créneau réservé/)).toBeVisible();
  });
});
