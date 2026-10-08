import { expect, test, type Page } from "@playwright/test";
import { installEventHubMock } from "./helpers/eventHubMock";

async function setup(page: Page, error?: string) {
  const submissions: Record<string, unknown>[] = [];
  await page.addInitScript(installEventHubMock, {});
  await page.addInitScript(() => {
    localStorage.setItem("libraryType", "pure");
    window.open = () => null;
  });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/projects/start-async") {
      submissions.push(route.request().postDataJSON());
      if (error) return route.fulfill({status: 400, json: {detail: error}});
      return route.fulfill({json: {
        project_id: "temporary-demo", job_id: "startup-demo", library_type: "pure",
        status: "queued", progress: 0, phase: "queued", ready_url: null,
        created_at: "2026-10-08T10:00:00Z", updated_at: "2026-10-08T10:00:00Z",
      }});
    }
    if (path === "/api/tiktok-urls/check") return route.fulfill({json: {exists: false}});
    if (path.endsWith("/source-details")) return route.fulfill({json: []});
    if (path.endsWith("/jobs")) return route.fulfill({json: {jobs: []}});
    return route.fulfill({json: {}});
  });
  await page.goto("/");
  await page.getByPlaceholder("https://www.tiktok.com/@user/video/...").fill("https://www.tiktok.com/@demo/video/123");
  return submissions;
}

test("Pure starts its classic flow when the optional source is empty", async ({page}) => {
  const submissions = await setup(page);
  await expect(page.getByText("Laisse ce champ vide", {exact: false})).toBeVisible();
  await page.getByRole("button", {name: "Démarrer", exact: true}).click();
  await expect.poll(() => submissions.length).toBe(1);
  expect(submissions[0]).toMatchObject({library_type: "pure"});
  expect(submissions[0]).not.toHaveProperty("temporary_source");
});

test("Pure sends distinct TikTok/source URLs and the advanced indexing density", async ({page}) => {
  const submissions = await setup(page);
  await page.getByLabel("Source originale", {exact: false}).fill(" https://www.youtube.com/watch?v=source123 ");
  await page.getByText("Réglages avancés", {exact: true}).click();
  await page.getByLabel("Densité d’indexation").selectOption("4");
  await expect(page.getByText("Le projet s’ouvrira une fois prêt", {exact: false})).toBeVisible();
  await page.getByRole("button", {name: "Démarrer", exact: true}).click();
  await expect.poll(() => submissions.length).toBe(1);
  expect(submissions[0]).toEqual({
    library_type: "pure", tiktok_url: "https://www.tiktok.com/@demo/video/123",
    temporary_source: {kind: "url", value: "https://www.youtube.com/watch?v=source123", fps: 4},
  });
  await expect(page.getByLabel("Source originale", {exact: false})).toHaveValue("");
});

test("local folder uses the backend path, defaults to 2 fps, and excludes subfolders", async ({page}) => {
  const submissions = await setup(page);
  await page.getByRole("button", {name: "Dossier local", exact: true}).click();
  await page.getByLabel("Source originale", {exact: false}).fill("/media/film");
  await expect(page.getByRole("button", {name: "Parcourir", exact: true})).toBeVisible();
  await expect(page.getByText("Seules les vidéos directement dans le dossier", {exact: false})).toBeVisible();
  await page.getByRole("button", {name: "Démarrer", exact: true}).click();
  await expect.poll(() => submissions.length).toBe(1);
  expect(submissions[0].temporary_source).toEqual({kind: "folder", value: "/media/film", fps: 2});
});

test("invalid source stays editable after the server rejects startup", async ({page}) => {
  const error = "Le dossier source est introuvable sur la machine du backend.";
  const submissions = await setup(page, error);
  await page.getByRole("button", {name: "Dossier local", exact: true}).click();
  await page.getByLabel("Source originale", {exact: false}).fill("/missing");
  await page.getByRole("button", {name: "Démarrer", exact: true}).click();
  await expect(page.getByText(error, {exact: true})).toBeVisible();
  await expect(page.getByLabel("Source originale", {exact: false})).toHaveValue("/missing");
  await expect(page.getByRole("button", {name: "Démarrer", exact: true})).toBeEnabled();
  expect(submissions).toHaveLength(1);
});
