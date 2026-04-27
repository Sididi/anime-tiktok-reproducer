import { expect, test } from "@playwright/test";

function installMatchesContinuityMocks(targetProjectId: string) {
  const originalFetch = window.fetch.bind(window);

    const project = {
      id: targetProjectId,
      tiktok_url: "https://www.tiktok.com/@demo/video/123",
      source_paths: [],
      phase: "match_validation",
      created_at: "2026-04-13T10:00:00Z",
      updated_at: "2026-04-13T10:00:00Z",
      video_path: "/tmp/demo.mp4",
      video_duration: 12,
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

    const scenes = {
      scenes: Array.from({ length: 6 }, (_, index) => ({
        index,
        start_time: index * 2,
        end_time: index * 2 + 1.5,
        duration: 1.5,
      })),
    };

    const matches = {
      matches: [
        {
          scene_index: 0,
          episode: "Episode-A.mkv",
          start_time: 10,
          end_time: 11.5,
          confidence: 0.98,
          speed_ratio: 1,
          confirmed: true,
          was_no_match: false,
          merged_from: null,
          alternatives: [],
          start_candidates: [],
          middle_candidates: [],
          end_candidates: [],
        },
        {
          scene_index: 1,
          episode: "/library/Demo Source/Episode-A.mp4",
          start_time: 4,
          end_time: 5.5,
          confidence: 0.97,
          speed_ratio: 1,
          confirmed: true,
          was_no_match: false,
          merged_from: null,
          alternatives: [],
          start_candidates: [],
          middle_candidates: [],
          end_candidates: [],
        },
        {
          scene_index: 2,
          episode: "/library/Demo Source/Episode-B.mp4",
          start_time: 20,
          end_time: 21.5,
          confidence: 0.96,
          speed_ratio: 1,
          confirmed: true,
          was_no_match: false,
          merged_from: null,
          alternatives: [],
          start_candidates: [],
          middle_candidates: [],
          end_candidates: [],
        },
        {
          scene_index: 3,
          episode: "Episode-C",
          start_time: 30,
          end_time: 31.5,
          confidence: 0.95,
          speed_ratio: 1,
          confirmed: true,
          was_no_match: false,
          merged_from: null,
          alternatives: [],
          start_candidates: [],
          middle_candidates: [],
          end_candidates: [],
        },
        {
          scene_index: 4,
          episode: "Episode-B",
          start_time: 40,
          end_time: 41.5,
          confidence: 0.94,
          speed_ratio: 1,
          confirmed: true,
          was_no_match: false,
          merged_from: null,
          alternatives: [],
          start_candidates: [],
          middle_candidates: [],
          end_candidates: [],
        },
        {
          scene_index: 5,
          episode: "Episode-C.mp4",
          start_time: 50,
          end_time: 51.5,
          confidence: 0.93,
          speed_ratio: 1,
          confirmed: true,
          was_no_match: false,
          merged_from: null,
          alternatives: [],
          start_candidates: [],
          middle_candidates: [],
          end_candidates: [],
        },
      ],
    };

    const playbackManifest = {
      ready: true,
      fingerprint: "continuity-test",
      generated_at: "2026-04-13T10:00:01Z",
      scenes: [],
      scene_status: {},
    };

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
        return new Response(JSON.stringify(scenes), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.pathname === `/api/projects/${targetProjectId}/matches`) {
        return new Response(JSON.stringify(matches), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (
        url.pathname ===
        `/api/projects/${targetProjectId}/matches/playback/manifest`
      ) {
        return new Response(JSON.stringify(playbackManifest), {
          status: 200,
          headers: { "Content-Type": "application/json" },
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

test("matches page highlights non-continuous scenes and navigates across all claims", async ({
  page,
}) => {
  const projectId = "project-continuity";
  await page.addInitScript(installMatchesContinuityMocks, projectId);

  await page.goto(`/project/${projectId}/matches`);

  await expect(
    page.getByRole("heading", { name: "Match Validation" }),
  ).toBeVisible();

  await expect(page.locator("[data-continuity-kind]")).toHaveCount(4);

  const scene2 = page.locator(
    '[data-scene-index="1"][data-continuity-kind="non_continuous"]',
  );
  const scene3 = page.locator(
    '[data-scene-index="2"][data-continuity-kind="episode_change"]',
  );
  const scene4 = page.locator(
    '[data-scene-index="3"][data-continuity-kind="episode_change"]',
  );
  const scene5 = page.locator(
    '[data-scene-index="4"][data-continuity-kind="non_continuous"]',
  );
  const scene2Row = scene2.locator("xpath=../..");
  const scene4Row = scene4.locator("xpath=../..");
  const scene5Row = scene5.locator("xpath=../..");

  await expect(scene2.getByText("Non-continuous")).toBeVisible();
  await expect(scene3.getByText("Episode change")).toBeVisible();
  await expect(scene4.getByText("Episode change")).toBeVisible();
  await expect(scene5.getByText("Non-continuous")).toBeVisible();
  await expect(
    page.locator('[data-scene-index="5"][data-continuity-kind]'),
  ).toHaveCount(0);

  await expect(
    scene2Row.getByRole("button", { name: "Previous claimed scene" }),
  ).toBeDisabled();
  await expect(
    scene5Row.getByRole("button", { name: "Next claimed scene" }),
  ).toBeDisabled();

  await scene2Row.getByRole("button", { name: "Next claimed scene" }).click();
  await expect(scene3).toHaveClass(/ring-2/);

  await scene4Row.getByRole("button", { name: "Next claimed scene" }).click();
  await expect(scene5).toHaveClass(/ring-2/);

  await scene5Row
    .getByRole("button", { name: "Previous claimed scene" })
    .click();
  await expect(scene4).toHaveClass(/ring-2/);
});
