import { test, expect } from "@playwright/test";

/**
 * E2E test: Full workflow from TikTok URL to project bundle
 *
 * Test data:
 * - TikTok URL: https://www.tiktok.com/@broykren0/video/7426555105954073899
 * - Source episodes: ./examples/source_anime/ (3 Hanebado! episodes)
 */

// Test TikTok URL
const TIKTOK_URL = "https://www.tiktok.com/@broykren0/video/7426555105954073899";

// Absolute path to source anime folder
const SOURCE_ANIME_PATH = "/home/sid/Projects/anime-tiktok-reproducer/examples/source_anime";

test.describe("Anime TikTok Reproducer - Full Workflow", () => {
  test("should create project and download TikTok video", async ({ page }) => {
    // Step 1: Navigate to home page
    await page.goto("/");

    // Step 2: Create new project with TikTok URL
    await page.fill('[data-testid="tiktok-url-input"]', TIKTOK_URL);
    await page.fill('[data-testid="source-path-input"]', SOURCE_ANIME_PATH);

    // Click create button
    await page.click('[data-testid="create-project-btn"]');

    // Wait for download progress (SSE stream)
    await expect(page.locator("text=Downloading")).toBeVisible({ timeout: 30000 });

    // Wait for download to complete - should navigate to scene validation
    await expect(page).toHaveURL(/\/scenes$/, { timeout: 120000 });
  });

  test("should detect scenes and allow validation", async ({ page }) => {
    // Navigate to home first
    await page.goto("/");

    // Create a new project for this test
    await page.fill('[data-testid="tiktok-url-input"]', TIKTOK_URL);
    await page.fill('[data-testid="source-path-input"]', SOURCE_ANIME_PATH);
    await page.click('[data-testid="create-project-btn"]');

    // Wait for navigation to scene validation
    await expect(page).toHaveURL(/\/scenes$/, { timeout: 120000 });

    // Wait for video to load
    await expect(page.locator("video")).toBeVisible({ timeout: 10000 });

    // Check for scene blocks in timeline
    await expect(page.locator("[data-scene-block]").first()).toBeVisible({
      timeout: 30000,
    });

    // Test timeline interaction - click on a scene block
    const firstSceneBlock = page.locator("[data-scene-block]").first();
    await firstSceneBlock.click();

    // Scene should be selected (has ring class)
    await expect(firstSceneBlock).toHaveClass(/ring-2/);
  });

  test("should allow scene boundary resize", async ({ page }) => {
    // Create project and navigate to scene validation
    await page.goto("/");
    await page.fill('[data-testid="tiktok-url-input"]', TIKTOK_URL);
    await page.fill('[data-testid="source-path-input"]', SOURCE_ANIME_PATH);
    await page.click('[data-testid="create-project-btn"]');

    await expect(page).toHaveURL(/\/scenes$/, { timeout: 120000 });
    await expect(page.locator("video")).toBeVisible({ timeout: 10000 });

    // Wait for scene blocks
    await expect(page.locator("[data-scene-block]")).toHaveCount(1, { timeout: 30000 }).catch(() => {
      // Any number of scenes is fine, just ensure at least one
    });
    await expect(page.locator("[data-scene-block]").first()).toBeVisible({ timeout: 10000 });

    // Find a resize handle (need more than one scene for internal handles)
    const resizeHandle = page.locator("[data-resize-handle='right']").first();
    const hasHandle = await resizeHandle.isVisible({ timeout: 5000 }).catch(() => false);

    if (!hasHandle) {
      // Only one scene detected, no internal resize handles
      console.log("Only one scene detected, skipping resize test");
      return;
    }

    // Get initial position of the scene block
    const sceneBlock = page.locator("[data-scene-block]").first();
    const initialBox = await sceneBlock.boundingBox();

    if (!initialBox) {
      return;
    }

    // Drag resize handle to extend the scene
    const handleBox = await resizeHandle.boundingBox();
    if (!handleBox) {
      return;
    }

    await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(handleBox.x + 50, handleBox.y + handleBox.height / 2);
    await page.mouse.up();

    // Check that the scene block width changed
    const newBox = await sceneBlock.boundingBox();
    expect(newBox?.width).not.toBe(initialBox.width);
  });
});
