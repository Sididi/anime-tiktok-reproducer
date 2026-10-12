import { expect, test } from "@playwright/test";
import { installEventHubMock } from "./helpers/eventHubMock";

const activeRow = {
  project_id: "archive-test", anime_title: "Archive Test", library_type: "anime",
  language: "fr", local_size_bytes: 1048576, uploaded: true, uploaded_status: "green",
  can_upload_status: "green", can_upload_reasons: [], has_metadata: true,
  drive_video_count: 1, drive_video_name: "video.mp4", drive_video_id: "video",
  drive_video_web_url: "https://drive.example/video", drive_folder_id: "original",
  drive_folder_url: "https://drive.example/original", created_at: "2026-01-01T10:00:00Z",
  scheduled_at: "2026-01-02T10:00:00Z", scheduled_account_id: null,
  mother_project_id: "parent", llm_preset_resolved: "default", llm_preset_is_default: true,
  min_playback_speed_resolved: 1, min_playback_speed_is_default: true,
  template_resolved: "default", template_is_default: true,
};
const archivedRow = {
  ...activeRow, archived_at: "2026-10-12T10:00:00Z", local_size_bytes: 1200,
  can_upload_status: "red", has_metadata: false, drive_video_id: null,
  drive_video_count: 0, drive_folder_id: "archive",
  drive_folder_url: "https://drive.example/archive", llm_preset_resolved: null,
  template_resolved: null, min_playback_speed_resolved: null,
  source_url: "https://www.tiktok.com/@example/video/123",
  upload_last_result: { platforms: [{ platform: "youtube", status: "uploaded", url: "https://youtu.be/example" }] },
};

test("deletion keeps the archive row, replaces its Drive link, and disables actions", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {});
  let deleted = false;
  let managerReads = 0;
  let deleteRequests = 0;
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/project-manager/projects/archive-test" && route.request().method() === "DELETE") {
      deleted = true;
      deleteRequests++;
      return route.fulfill({ json: { status: "deleted", local_deleted: true, project: archivedRow } });
    }
    if (path === "/api/project-manager/projects") {
      managerReads++;
      return route.fulfill({ json: { projects: [deleted ? archivedRow : activeRow] } });
    }
    if (path === "/api/accounts") return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith("/jobs") || path.endsWith("/upload-jobs")) return route.fulfill({ json: { jobs: [] } });
    if (path === "/api/projects" || path === "/api/anime/source-details") return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  const row = page.getByRole("row").filter({ hasText: "Archive Test" });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Delete project", exact: true }).click();
  await page.getByRole("button", { name: "Confirm delete", exact: true }).click();
  await expect(row.getByText("Archive-only · Read-only")).toBeVisible();
  await expect(row.getByRole("link", { name: "Drive archive" })).toHaveAttribute("href", "https://drive.example/archive");
  await expect(row.getByRole("button", { name: "Archive-only project — already deleted" })).toBeDisabled();
  await expect(row.getByRole("button", { name: /^Upload$/ })).toBeDisabled();
  await expect(row.getByRole("button", { name: "Preview video" })).toHaveCount(0);
  await row.getByText("Archive information").click();
  await expect(row.getByRole("link", { name: "Original source" })).toBeVisible();
  await expect(row.getByRole("link", { name: "Post", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Multi-select delete" }).click();
  await expect(row.getByRole("checkbox")).toBeDisabled();
  expect(deleteRequests).toBe(1);
  expect(managerReads).toBe(1);
});

for (const path of ["/project/archive-test/matches", "/projects/archive-test/matches", "/project/archive-test/prompter"]) {
  test(`direct archive URL is disabled before work pages load: ${path}`, async ({ page }) => {
    await page.addInitScript(installEventHubMock, {});
    const projectRequests: string[] = [];
    await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.startsWith("/api/projects/archive-test")) projectRequests.push(pathname);
      return route.fulfill({ status: 410, json: { detail: {
        code: "project_archived", message: "This project is archive-only and is disabled.",
      } } });
    });
    await page.goto(path);
    await expect(page.getByRole("heading", { name: "Archive-only project" })).toBeVisible();
    await expect(page.getByText(/This project is disabled/)).toBeVisible();
    // Development StrictMode may repeat the metadata read; work/media APIs
    // must never be reached.
    expect([...new Set(projectRequests)]).toEqual(["/api/projects/archive-test"]);
  });
}

test("bulk deletion runs six at a time and restores failed selections after reload", async ({ page }) => {
  await page.addInitScript(installEventHubMock, {});
  const rows = Array.from({ length: 8 }, (_, index) => ({
    ...activeRow, project_id: `bulk-${index}`, anime_title: `Bulk ${index}`,
  }));
  const completed = new Set<string>();
  const requests: string[] = [];
  let running = 0;
  let peak = 0;
  let fail = true;
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const path = new URL(route.request().url()).pathname;
    const match = path.match(/^\/api\/project-manager\/projects\/(bulk-\d+)$/);
    if (match && route.request().method() === "DELETE") {
      const id = match[1];
      requests.push(id);
      running++;
      peak = Math.max(peak, running);
      await new Promise((resolve) => setTimeout(resolve, 250));
      running--;
      if (id === "bulk-7" && fail) return route.fulfill({ status: 503, json: { detail: "Temporary Drive failure" } });
      completed.add(id);
      return route.fulfill({ json: { status: "deleted", local_deleted: true, project: {
        ...archivedRow, project_id: id, anime_title: `Bulk ${id.slice(-1)}`,
      } } });
    }
    if (path === "/api/project-manager/projects") return route.fulfill({ json: { projects: rows.map((row) => completed.has(row.project_id) ? {
      ...archivedRow, project_id: row.project_id, anime_title: row.anime_title,
    } : row) } });
    if (path === "/api/accounts") return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith("/jobs") || path.endsWith("/upload-jobs")) return route.fulfill({ json: { jobs: [] } });
    if (path === "/api/projects" || path === "/api/anime/source-details") return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  await page.getByRole("button", { name: "Multi-select delete" }).click();
  for (const row of rows) await page.getByRole("row").filter({ hasText: row.anime_title }).getByRole("checkbox").check();
  await page.getByRole("button", { name: "Delete (8)", exact: true }).click();
  await page.getByRole("button", { name: "Delete All", exact: true }).click();
  await expect(page.getByRole("button", { name: /Deleting \d\/8/ })).toBeVisible();
  await expect(page.getByText("Deleted 7/8 selected projects. 1 failed.")).toBeVisible();
  expect(peak).toBe(6);
  expect(requests).toHaveLength(8);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("project-manager-pending-deletions") ?? "[]"))).toEqual(["bulk-7"]);
  await page.reload();
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: "Bulk 7" }).getByRole("checkbox")).toBeChecked();
  fail = false;
  await page.getByRole("button", { name: "Delete (1)", exact: true }).click();
  await page.getByRole("button", { name: "Delete All", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: "Bulk 7" }).getByText("Archive-only · Read-only")).toBeVisible();
  expect(requests).toHaveLength(9);
  expect(await page.evaluate(() => localStorage.getItem("project-manager-pending-deletions"))).toBeNull();
});
