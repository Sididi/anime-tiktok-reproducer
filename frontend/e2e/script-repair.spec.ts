import { expect, test, type Page } from "@playwright/test";
import type { ScriptRepairReport } from "../src/types";

const projectId = "script-repair-e2e";
const runId = "a".repeat(32);
const script = (texts: string[]) => ({ language: "fr", scenes: texts.map((text, scene_index) => ({ scene_index, text })) });
const original = script(["Il ouvre la porte.", "", "Elle s'enfuit.", ""]);
const repaired = script(["Il ouvre", "la porte.", "Elle s'enfuit.", ""]);
const report: ScriptRepairReport = {
  status: "repaired", attempts: 1, model: "google/gemini-3.1-flash-lite", review_required: true,
  unresolved_scene_indices: [], source_gap_scene_indices: [1], issues: [], warning: null,
  changes: [{ scene_index: 0, before: original.scenes[0].text, after: repaired.scenes[0].text },
    { scene_index: 1, before: "", after: repaired.scenes[1].text }],
};

async function fixture(page: Page, options: { unresolved?: boolean; withAudio?: boolean; hold?: boolean } = {}) {
  const source = { language: "en", scenes: ["He opens", "", "She runs away.", ""].map((text, scene_index) => ({
    scene_index, text, is_raw: scene_index === 3, start_time: scene_index * 2, end_time: scene_index * 2 + 2, words: [],
  })) };
  const config = {
    enabled: true, script_title_selection_enabled: false, static_overlay_title_enabled: false,
    static_overlay_title: null,
    llm: { configured: true, preset_key: "gpt", preset_label: "GPT", big_model: "gpt", light_model: "light" },
    elevenlabs: { configured: true, model_id: "eleven_v3", output_format: "mp3_44100_128" },
    voices: [{ key: "test", display_name: "Test voice", languages: ["fr", "en", "es"] }],
    default_voice_key: "test", voice_config_error: null, musics: [], default_music_key: null, music_config_error: null,
    templates: [{ key: "test", label: "Test", overlay_enabled: false, overlay_title_enabled: false,
      overlay_category_enabled: false, overlay_title_text: null, overlay_category_text: null }],
    llm_presets: [{ key: "gpt", label: "GPT" }],
    current: { llm_preset: "gpt", template: "test", min_playback_speed: 0.75 },
    defaults: { llm_preset: "gpt", template: "test", min_playback_speed: 0.75 },
  };
  let stored: Record<string, unknown> | null = null;
  if (options.unresolved) stored = { script_json: original, repair_report: { ...report, status: "failed", changes: [],
    review_required: false, unresolved_scene_indices: [1], warning: "Please retry explicitly." }, draft_status: "unresolved", draft_origin: "paste" };
  if (options.withAudio) stored = { script_json: repaired, draft_status: "validated", draft_origin: "automation",
    parts: [{ id: "1", char_count: 20, download_url: "unused" }] };
  const requests = { repair: 0, reviewed: 0, automate: [] as Record<string, unknown>[], prepare: 0 };
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(`/api/projects/${projectId}`, "");
    const json = (body: unknown) => route.fulfill({ json: body });
    if (path === "") return json({ id: projectId, phase: "script_restructure", library_type: "pure", video_duration: 8,
      source_paths: [], video_path: "/tmp/test.mp4", video_fps: 30 });
    if (path === "/scenes") return json({ scenes: source.scenes.map((s) => ({ index: s.scene_index, start_time: s.start_time, end_time: s.end_time })) });
    if (path === "/transcription") return json({ transcription: source });
    if (path === "/script/automation/config") return json(config);
    if (path === "/script/settings") return json({ tts_speed: 1, voice_key: "test", video_overlay: {} });
    if (path === "/metadata") return json({ exists: false });
    if (path === "/script/prompt") return json({ prompt: "Mock prompt" });
    if (path === "/script/latest-generation") return json(stored ? {
      exists: true, source: "automation_run", run_id: runId, parts: [], source_matches: true, target_language: "fr", ...stored,
    } : { exists: false, parts: [], script_json: null });
    if (path === "/script/repair") {
      requests.repair++;
      if (options.hold) await held;
      stored = { script_json: repaired, repair_report: report, draft_status: "review_required", draft_origin: "paste" };
      return json({ run_id: runId, script_json: repaired, repair_report: report });
    }
    if (path === "/script/repair/review") {
      requests.reviewed++;
      const body = request.postDataJSON();
      expect(body.script_json.scenes[3].text).toBe("");
      stored = { script_json: body.script_json, repair_report: { ...report, review_required: false }, draft_status: "validated", draft_origin: "paste" };
      return json({ run_id: runId, ...stored });
    }
    if (path === "/script/automate") {
      const body = request.postDataJSON(); requests.automate.push(body);
      const event = body.existing_script_json ? { event: "complete", status: "complete", script_json: body.existing_script_json, run_id: runId, parts: [] }
        : { event: "script_ready", status: "paused", script_json: repaired, repair_report: report, run_id: runId };
      stored = { script_json: repaired, repair_report: report, draft_status: "review_required", draft_origin: "automation" };
      return route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify(event)}\n\n` });
    }
    if (path === "/script/tts/prepare") {
      requests.prepare++;
      return json({ language: "fr", normalized_full_text: "Il ouvre la porte. Elle s'enfuit.", segments: [
        { id: 1, scene_indices: [0, 1, 2], text: "Il ouvre la porte. Elle s'enfuit.", character_count: 37 },
      ] });
    }
    if (path.includes("/parts/")) return route.fulfill({ contentType: "audio/mpeg", body: Buffer.from([0, 0]),
      headers: { "Content-Disposition": 'attachment; filename="old-audio.mp3"' } });
    return route.fulfill({ status: 404, body: "Fixture has no media" });
  });
  await page.goto(`/project/${projectId}/script`);
  await expect(page.getByRole("heading", { name: "Script Restructuration" })).toBeVisible();
  return { requests, release };
}

async function paste(page: Page, payload = original) {
  await page.getByRole("textbox", { name: "Script JSON" }).evaluate((element, text) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData("text", text);
    (element as HTMLTextAreaElement).select();
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData, bubbles: true, cancelable: true }));
  }, JSON.stringify(payload));
}

test("pasted gaps repair once, show changes, persist review, and keep raw scenes empty", async ({ page }) => {
  const { requests } = await fixture(page);
  await paste(page);
  const dialog = page.getByRole("dialog", { name: "Edit Script" });
  await expect(dialog).toBeVisible();
  expect(requests.repair).toBe(1);
  await expect(dialog.locator('[data-scene-index="1"][data-repaired="true"]')).toBeVisible();
  await dialog.getByText("Before / after", { exact: true }).click();
  await expect(dialog.getByText("Before: (empty)", { exact: true })).toBeVisible();
  await expect(dialog.getByText("After: la porte.", { exact: true })).toBeVisible();
  await expect(dialog.getByText(/Source text was missing/)).toBeVisible();
  expect(requests.prepare).toBe(0);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("button", { name: "Automate", exact: true })).toBeDisabled();
  await page.reload();
  await expect(dialog).toBeVisible();
  expect(requests.repair).toBe(1);
  await dialog.getByRole("button", { name: "Save Changes" }).click();
  await expect(dialog).not.toBeVisible();
  expect(requests.reviewed).toBe(1);
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Script JSON" })).toHaveValue(/la porte/);
  await expect(dialog).not.toBeVisible();
  expect(requests.repair).toBe(1);
});

test("automation repair forces review even with validation unchecked", async ({ page }) => {
  const { requests } = await fixture(page);
  await page.getByRole("checkbox", { name: "Validate script before TTS" }).uncheck();
  await page.getByRole("button", { name: "Automate", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Validate Script", exact: true });
  await expect(dialog).toBeVisible();
  expect(requests.automate).toHaveLength(1);
  expect(requests.automate[0].pause_after_script).toBe(false);
  await dialog.getByRole("button", { name: "Validate Script and Continue" }).click();
  await expect(dialog).not.toBeVisible();
  await expect.poll(() => requests.automate.length).toBe(2);
  expect(requests.reviewed).toBe(1);
  expect(requests.automate[1].existing_script_json).toEqual(expect.objectContaining({ scenes: expect.any(Array) }));
});

test("unresolved draft reload never retries automatically and remains editable", async ({ page }) => {
  const { requests } = await fixture(page, { unresolved: true });
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Save Changes" }).click();
  await expect(dialog.getByRole("alert")).toContainText("non-empty text");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(requests.repair).toBe(0);
  await page.getByRole("textbox", { name: "Script JSON" }).locator("..").getByRole("button", { name: "Edit Script", exact: true }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Retry repair" }).click();
  await expect(dialog.getByText(/2 scenes repaired/)).toBeVisible();
  expect(requests.repair).toBe(1);
});

for (const action of ["edit", "cancel", "language"] as const) {
  test(`late repair result is discarded after ${action}`, async ({ page }) => {
    const { requests, release } = await fixture(page, { hold: true });
    await paste(page);
    await expect.poll(() => requests.repair).toBe(1);
    if (action === "edit") await page.getByRole("textbox", { name: "Script JSON" }).fill(JSON.stringify(script(["My edit", "", "Stayed", ""])));
    if (action === "cancel") await page.getByRole("button", { name: "Cancel repair" }).click();
    if (action === "language") await page.getByRole("combobox", { name: "Langue de sortie" }).selectOption("en");
    release();
    await expect(page.getByRole("button", { name: "Cancel repair" })).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "Script JSON" })).toHaveValue(action === "edit" ? /My edit/ : /Il ouvre la porte/);
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
}

test("ordinary typing and valid pasted scripts incur no repair call", async ({ page }) => {
  const { requests } = await fixture(page);
  await page.getByRole("textbox", { name: "Script JSON" }).fill(JSON.stringify(original));
  await expect(page.getByRole("button", { name: "Repair empty scenes" })).toBeVisible();
  expect(requests.repair).toBe(0);
  await paste(page, repaired);
  await expect(page.getByRole("button", { name: "Repair empty scenes" })).toHaveCount(0);
  await expect(page.getByText("Valid JSON", { exact: true })).toBeVisible();
  expect(requests.repair).toBe(0);
});

test("changed narration invalidates previously loaded audio", async ({ page }) => {
  const { requests } = await fixture(page, { withAudio: true });
  await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeEnabled();
  await paste(page);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Save Changes" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
  expect(requests.repair).toBe(1);
  await expect(page.getByText("old-audio.mp3", { exact: true })).toHaveCount(0);
});
