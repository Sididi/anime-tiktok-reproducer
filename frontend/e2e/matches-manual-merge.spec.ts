import { expect, test } from "@playwright/test";

function installMatchesManualMergeMocks(targetProjectId: string) {
  const originalFetch = window.fetch.bind(window);

  const project = {
    id: targetProjectId,
    tiktok_url: "https://www.tiktok.com/@demo/video/123",
    source_paths: [],
    phase: "match_validation",
    created_at: "2026-04-13T10:00:00Z",
    updated_at: "2026-04-13T10:00:00Z",
    video_path: "/tmp/demo.mp4",
    video_duration: 8,
    video_fps: 24,
    anime_name: "Demo Source",
    series_id: "series-1",
    library_type: "anime",
    output_language: "fr",
    drive_folder_id: null,
    drive_folder_url: null,
    generation_discord_message_id: null,
    final_upload_discord_message_id: null,
    upload_completed_at: null,
    upload_last_result: null,
  };

  let currentScenes = {
    scenes: [
      {
        index: 0,
        start_time: 0,
        end_time: 2.4,
        duration: 2.4,
      },
      {
        index: 1,
        start_time: 2.4,
        end_time: 4.2,
        duration: 1.8,
      },
      {
        index: 2,
        start_time: 4.2,
        end_time: 5.8,
        duration: 1.6,
      },
    ],
  };

  let currentMatches = {
    matches: [
      {
        scene_index: 0,
        episode: "Episode-A.mp4",
        start_time: 10,
        end_time: 12.4,
        confidence: 0.98,
        speed_ratio: 1,
        confirmed: true,
        was_no_match: false,
        merged_from: null,
        alternatives: [
          {
            episode: "Episode-A.mp4",
            start_time: 10,
            end_time: 12.4,
            confidence: 0.96,
            speed_ratio: 1,
            vote_count: 3,
            algorithm: "weighted_avg",
          },
        ],
        start_candidates: [
          {
            episode: "Episode-A.mp4",
            timestamp: 10,
            similarity: 0.96,
            series: "Demo Source",
          },
        ],
        middle_candidates: [],
        end_candidates: [],
      },
      {
        scene_index: 1,
        episode: "",
        start_time: 0,
        end_time: 0,
        confidence: 0,
        speed_ratio: 1,
        confirmed: false,
        was_no_match: true,
        merged_from: null,
        alternatives: [
          {
            episode: "Episode-A.mkv",
            start_time: 30,
            end_time: 31.8,
            confidence: 0.84,
            speed_ratio: 1,
            vote_count: 2,
            algorithm: "weighted_avg",
          },
        ],
        start_candidates: [
          {
            episode: "Episode-A.mkv",
            timestamp: 30,
            similarity: 0.81,
            series: "Demo Source",
          },
        ],
        middle_candidates: [],
        end_candidates: [],
      },
      {
        scene_index: 2,
        episode: "Episode-C.mp4",
        start_time: 50,
        end_time: 51.6,
        confidence: 0.93,
        speed_ratio: 1,
        confirmed: true,
        was_no_match: false,
        merged_from: null,
        alternatives: [
          {
            episode: "Episode-D.mp4",
            start_time: 80,
            end_time: 81.6,
            confidence: 0.72,
            speed_ratio: 1,
            vote_count: 1,
            algorithm: "best_frame",
          },
        ],
        start_candidates: [
          {
            episode: "Episode-C.mp4",
            timestamp: 50,
            similarity: 0.92,
            series: "Demo Source",
          },
        ],
        middle_candidates: [],
        end_candidates: [],
      },
    ],
  };

  let currentPlaybackManifest = {
    ready: true,
    fingerprint: "manual-merge-initial",
    generated_at: "2026-04-13T10:00:01Z",
    scenes: [],
    scene_status: {},
  };

  (window as typeof window & { __manualMergeCalls?: number }).__manualMergeCalls =
    0;

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(requestUrl, window.location.origin);

    if (url.pathname === `/api/projects/${targetProjectId}`) {
      return new Response(JSON.stringify(project), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (url.pathname === `/api/projects/${targetProjectId}/scenes`) {
      return new Response(JSON.stringify(currentScenes), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (url.pathname === `/api/projects/${targetProjectId}/matches`) {
      return new Response(JSON.stringify(currentMatches), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (
      url.pathname ===
      `/api/projects/${targetProjectId}/matches/playback/manifest`
    ) {
      return new Response(JSON.stringify(currentPlaybackManifest), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (
      url.pathname ===
        `/api/projects/${targetProjectId}/matches/merge-with-previous/1` &&
      init?.method === "POST"
    ) {
      (
        window as typeof window & { __manualMergeCalls?: number }
      ).__manualMergeCalls =
        ((window as typeof window & { __manualMergeCalls?: number })
          .__manualMergeCalls || 0) + 1;

      currentScenes = {
        scenes: [
          {
            index: 0,
            start_time: 0,
            end_time: 4.2,
            duration: 4.2,
          },
          {
            index: 1,
            start_time: 4.2,
            end_time: 5.8,
            duration: 1.6,
          },
        ],
      };
      currentMatches = {
        matches: [
          {
            scene_index: 0,
            episode: "Episode-A.mp4",
            start_time: 10,
            end_time: 14.2,
            confidence: 0.99,
            speed_ratio: 1,
            confirmed: false,
            was_no_match: false,
            merged_from: [0, 1],
            alternatives: [],
            start_candidates: [],
            middle_candidates: [],
            end_candidates: [],
          },
          {
            ...currentMatches.matches[2],
            scene_index: 1,
          },
        ],
      };

      return new Response(
        JSON.stringify({
          scenes: currentScenes.scenes,
          matches: currentMatches.matches,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    if (
      url.pathname ===
        `/api/projects/${targetProjectId}/matches/playback/prepare` &&
      init?.method === "POST"
    ) {
      currentPlaybackManifest = {
        ready: true,
        fingerprint: "manual-merge-updated",
        generated_at: "2026-04-13T10:00:02Z",
        scenes: [],
        scene_status: {},
      };
      const ssePayload = `data: ${JSON.stringify({
        status: "complete",
        progress: 1,
        message: "Playback clips ready",
        manifest: currentPlaybackManifest,
      })}\n\n`;
      return new Response(ssePayload, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    if (url.pathname === `/api/projects/${targetProjectId}/sources/episodes`) {
      return new Response(JSON.stringify({ episodes: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (url.pathname === `/api/projects/${targetProjectId}/scenes/config`) {
      return new Response(JSON.stringify({ skip_ui_enabled: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (url.pathname === `/api/projects/${targetProjectId}/matches/config`) {
      return new Response(JSON.stringify({ full_auto_enabled: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    return originalFetch(input, init);
  };
}

test("matches page exposes manual merge button, hint animation, and merged state", async ({
  page,
}) => {
  const projectId = "project-manual-merge";
  await page.addInitScript(installMatchesManualMergeMocks, projectId);

  await page.goto(`/project/${projectId}/matches`);

  await expect(
    page.getByRole("heading", { name: "Match Validation" }),
  ).toBeVisible();

  await expect(
    page.locator('[data-manual-merge-scene-index="0"]'),
  ).toHaveCount(0);

  const hintedButton = page.locator(
    '[data-manual-merge-scene-index="1"][data-manual-merge-hint="true"]',
  );
  const plainButton = page.locator(
    '[data-manual-merge-scene-index="2"][data-manual-merge-hint="false"]',
  );

  await expect(hintedButton).toBeVisible();
  await expect(hintedButton).toHaveClass(/manual-merge-hint-button/);
  await expect(plainButton).toBeVisible();

  await hintedButton.click();

  await page.waitForFunction(() => {
    return (
      (window as typeof window & { __manualMergeCalls?: number })
        .__manualMergeCalls === 1
    );
  });

  await expect(page.getByText("Merged (was scenes 1+2)")).toBeVisible();
  await expect(
    page.getByTitle("Undo merge and restore original scenes"),
  ).toBeVisible();
});
