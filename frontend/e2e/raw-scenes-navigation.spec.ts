import { expect, test, type Page } from "@playwright/test";

async function getVisibleVideoReadyState(page: Page) {
  return page.locator("video").evaluateAll((videos) => {
    const visibleVideos = videos.filter((video) => {
      const rect = video.getBoundingClientRect();
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        rect.bottom > 0 &&
        rect.top < window.innerHeight
      );
    });

    if (visibleVideos.length === 0) {
      return -1;
    }

    return Math.min(...visibleVideos.map((video) => video.readyState));
  });
}

test("raw-scene players stay ready after client-side navigation from transcription", async ({
  page,
}) => {
  const projectId = "a2bb45027e0d";

  await page.goto(`/project/${projectId}/transcription`);
  await expect(
    page.getByRole("heading", { name: "Transcription" }),
  ).toBeVisible();

  await expect.poll(() => getVisibleVideoReadyState(page), {
    message: "expected visible transcription clips to be mounted before navigation",
    timeout: 15000,
  }).toBeGreaterThanOrEqual(1);

  await page.evaluate((nextPath) => {
    window.history.pushState({}, "", nextPath);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, `/project/${projectId}/raw-scenes`);

  await expect(page).toHaveURL(new RegExp(`/project/${projectId}/raw-scenes$`));
  await expect(
    page.getByRole("heading", { name: "Raw Scene Validation" }),
  ).toBeVisible();

  await expect.poll(() => getVisibleVideoReadyState(page), {
    message:
      "expected visible raw-scene players to reach at least metadata readiness after SPA navigation",
    timeout: 20000,
  }).toBeGreaterThanOrEqual(1);

  await expect(page.getByText("Failed to load")).toHaveCount(0);
});
