import { expect, test } from "@playwright/test";

function installMatchStreamMocks(projectId: string) {
  const testWindow = window as typeof window & {
    __deferredStarted?: boolean;
    __deferredHasSignal?: boolean;
    __deferredAborted?: boolean;
    __deferredCancelled?: boolean;
  };
  testWindow.__deferredStarted = false;
  testWindow.__deferredHasSignal = false;
  testWindow.__deferredAborted = false;
  testWindow.__deferredCancelled = false;

  const originalFetch = window.fetch.bind(window);
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  const scene = {
    index: 0,
    start_time: 0,
    end_time: 2,
    duration: 2,
  };
  const match = {
    scene_index: 0,
    episode: "Episode-01.mkv",
    start_time: 10,
    end_time: 12,
    confidence: 0.95,
    speed_ratio: 1,
    confirmed: true,
    alternatives: [],
    start_candidates: [],
    middle_candidates: [],
    end_candidates: [],
  };

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);
    const prefix = `/api/projects/${projectId}`;

    if (url.pathname === prefix) {
      return json({
        id: projectId,
        phase: "match_validation",
        source_paths: [],
        video_path: "/tmp/test.mp4",
        video_duration: 2,
        video_fps: 24,
        anime_name: "Test Anime",
        series_id: "series-test",
        library_type: "anime",
        output_language: "fr",
      });
    }
    if (url.pathname === `${prefix}/scenes`) {
      return json({ scenes: [scene] });
    }
    if (url.pathname === `${prefix}/matches` && init?.method !== "POST") {
      return json({ matches: [] });
    }
    if (url.pathname === `${prefix}/sources/episodes`) {
      return json({ episodes: ["Episode-01.mkv"] });
    }
    if (url.pathname === `${prefix}/scenes/config`) {
      return json({ skip_ui_enabled: true });
    }
    if (url.pathname === `${prefix}/matches/config`) {
      return json({ full_auto_enabled: false });
    }
    if (url.pathname === `${prefix}/transcription`) {
      return json({ transcription: null });
    }
    if (url.pathname === `${prefix}/transcription/config`) {
      return json({ full_auto_enabled: false });
    }
    if (
      url.pathname === `${prefix}/matches/find` &&
      init?.method === "POST"
    ) {
      const payload = `data: ${JSON.stringify({
        status: "complete",
        progress: 1,
        message: "Matched",
        matches: { matches: [match] },
      })}\n\n`;
      return new Response(payload, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (url.pathname === `${prefix}/matches/deferred-download`) {
      testWindow.__deferredStarted = true;
      testWindow.__deferredHasSignal = init?.signal instanceof AbortSignal;
      init?.signal?.addEventListener(
        "abort",
        () => {
          testWindow.__deferredAborted = true;
        },
        { once: true },
      );
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                `data: ${JSON.stringify({
                  status: "running",
                  phase: "download",
                  progress: 0.2,
                  message: "Downloading",
                })}\n\n`,
              ),
            );
          },
          cancel() {
            testWindow.__deferredCancelled = true;
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    }

    return originalFetch(input, init);
  };
}

test("leaving matches aborts and cancels the detached download stream", async ({
  page,
}) => {
  const projectId = "match-stream-lifecycle";
  await page.addInitScript(installMatchStreamMocks, projectId);
  await page.goto(`/project/${projectId}/matches`);

  await page.waitForFunction(() => window.__deferredStarted === true);
  expect(
    await page.evaluate(() => window.__deferredHasSignal),
  ).toBe(true);

  await page.evaluate((id) => {
    window.history.pushState({}, "", `/project/${id}/transcription`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, projectId);

  await expect(page).toHaveURL(`/project/${projectId}/transcription`);
  await page.waitForFunction(
    () => window.__deferredAborted && window.__deferredCancelled,
  );
});

declare global {
  interface Window {
    __deferredStarted?: boolean;
    __deferredHasSignal?: boolean;
    __deferredAborted?: boolean;
    __deferredCancelled?: boolean;
  }
}

