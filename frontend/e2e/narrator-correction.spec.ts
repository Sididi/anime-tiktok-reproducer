import { expect, test, type Page } from "@playwright/test";

async function mockProject(page: Page, legacy = false) {
  let selected: string[] | null = null;
  let previous: string[] | null = null;
  const keeps = (id: string) => (selected ?? ["SPEAKER_01"]).includes(id);
  let revision = 1;
  let undo = false;
  const bodies: unknown[] = [];
  const transcript = () => ({ language: "hi", unrecovered_gaps: [[298, 303.49]], scenes: [
    { scene_index: 0, start_time: 0, end_time: 4, text: keeps("SPEAKER_00") ? "Opening storyteller restored" : "", words: [], is_raw: !keeps("SPEAKER_00") },
    { scene_index: 1, start_time: 4, end_time: 12, text: keeps("SPEAKER_01") ? "Character dialogue" : "", words: [], is_raw: !keeps("SPEAKER_01") },
  ] });
  const response = () => ({
    transcription: transcript(),
    detection: { has_raw_scenes: transcript().scenes.some((s) => s.is_raw),
      tts_speaker_id: selected?.[0] ?? "SPEAKER_01", selected_narrator_ids: selected ?? ["SPEAKER_01"],
      selection_origin: selected ? "manual" : "automatic", speaker_count: 2, scene_parent_indices: [0, 1],
      candidates: transcript().scenes.filter((s) => s.is_raw).map((s) => ({
        scene_index: s.scene_index, start_time: s.start_time, end_time: s.end_time,
        confidence: 1, reason: "non_tts_speaker", was_split: false, original_scene_index: s.scene_index })) },
    narrator: { revision: String(revision), analysis_id: "analysis", correction_available: !legacy, undo_available: undo,
      recoverable_text: { "0": "Opening storyteller restored", "1": "Character dialogue" },
      unavailable_reason: legacy ? "Re-run transcription to prepare recoverable voice analysis." : null,
      speakers: [0, 1].map((n) => ({ speaker_id: `SPEAKER_0${n}`, duration: n ? 8 : 4,
        samples: [{ start_time: n ? 4 : 0, end_time: n ? 10 : 4, text: n ? "Dialogue sample" : "Storytelling sample" }] })) },
  });
  await page.route("**/api/projects/narrator-e2e**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/projects/narrator-e2e", "");
    const json = (body: unknown) => route.fulfill({ json: body });
    if (path === "/raw-scenes/narrator" && request.method() === "PUT") {
      const body = request.postDataJSON(); bodies.push(body);
      expect(body.expected_revision).toBe(String(revision));
      previous = selected;
      selected = body.speaker_ids;
      undo = true; revision++;
      return json(response());
    }
    if (path === "/raw-scenes/narrator/undo") {
      expect(request.postDataJSON().expected_revision).toBe(String(revision));
      selected = previous; undo = false; revision++;
      return json(response());
    }
    if (path === "/raw-scenes") return json(response());
    if (path === "/transcription") return json({ transcription: transcript() });
    if (path === "/transcription/config") return json({ full_auto_enabled: false });
    if (path === "/scenes") return json({ scenes: transcript().scenes.map((s) => ({ index: s.scene_index, start_time: s.start_time, end_time: s.end_time })) });
    if (path === "") return json({ id: "narrator-e2e", phase: "raw_scene_validation", library_type: "pure",
      video_duration: 12, video_path: "/tmp/video.mp4", source_paths: [], video_fps: 30 });
    return route.fulfill({ status: 404, body: "Fixture has no media" });
  });
  return bodies;
}

for (const stage of ["transcription", "raw-scenes"]) {
  test(`narrator correction and reloadable Undo on ${stage}`, async ({ page }) => {
    const bodies = await mockProject(page);
    await page.goto(`/project/narrator-e2e/${stage}`);
    await expect(page.getByText("Narrator: Voice 2 (automatic)", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Change narrator", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText(/resets transcript edits and raw-scene decisions/)).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Play Voice 1 sample 1" })).toBeVisible();
    await dialog.getByRole("checkbox", { name: /Voice 1/ }).check();
    await dialog.getByRole("button", { name: "Apply narrator and reset edits" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByText("Narrator: Voice 1 (selected)", { exact: true })).toBeVisible();
    expect(bodies).toEqual([{ speaker_ids: ["SPEAKER_00"], expected_revision: "1" }]);
    await page.reload();
    await page.getByRole("button", { name: "Undo narrator change" }).click();
    await expect(page.getByText("Narrator: Voice 2 (automatic)", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Undo narrator change" })).toHaveCount(0);
    await expect(page.getByText(/Speech could not be transcribed at/)).toBeVisible();
  });
}

test("legacy project explains the required retranscription", async ({ page }) => {
  await mockProject(page, true);
  await page.goto("/project/narrator-e2e/transcription");
  await expect(page.getByRole("button", { name: "Change narrator" })).toBeDisabled();
  await expect(page.getByText("Re-run transcription to prepare recoverable voice analysis.")).toBeVisible();
});

test("rejecting raw previews original words and edits hide Undo", async ({ page }) => {
  await mockProject(page);
  await page.goto("/project/narrator-e2e/raw-scenes");
  await page.getByRole("button", { name: "Mark as TTS", exact: true }).click();
  await expect(page.getByRole("textbox")).toHaveValue("Opening storyteller restored");
  await page.getByRole("button", { name: "Change narrator", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /Voice 1/ }).check();
  await dialog.getByRole("button", { name: "Apply narrator and reset edits" }).click();
  await expect(page.getByRole("button", { name: "Undo narrator change" })).toBeVisible();
  await page.getByRole("button", { name: "Mark as TTS", exact: true }).click();
  await expect(page.getByRole("textbox")).toHaveValue("Character dialogue");
  await expect(page.getByRole("button", { name: "Undo narrator change" })).toHaveCount(0);
});

for (const stage of ["transcription", "raw-scenes"]) {
  test(`merge narration voices, reload, unmerge and Undo on ${stage}`, async ({ page }) => {
    const bodies = await mockProject(page);
    await page.goto(`/project/narrator-e2e/${stage}`);
    const open = () => page.getByRole("button", { name: "Change narrator", exact: true }).click();
    const dialog = page.getByRole("dialog");
    const apply = dialog.getByRole("button", { name: "Apply narrator and reset edits" });
    await open();
    await dialog.getByRole("radio", { name: "Manual (select one or more voices)", exact: true }).check();
    await expect(apply).toBeDisabled();
    await dialog.getByRole("checkbox", { name: /Voice 1/ }).check();
    await dialog.getByRole("checkbox", { name: /Voice 2/ }).check();
    await apply.click();
    await expect(page.getByText("Narrator: Voice 1 + Voice 2 (merged)", { exact: true })).toBeVisible();
    expect(bodies[0]).toEqual({ speaker_ids: ["SPEAKER_00", "SPEAKER_01"], expected_revision: "1" });
    await page.reload();
    // Controls stay accessible even though both voices kept means no raw candidates.
    await open();
    await expect(dialog.getByRole("checkbox", { name: /Voice 1/ })).toBeChecked();
    await expect(dialog.getByRole("checkbox", { name: /Voice 2/ })).toBeChecked();
    await dialog.getByRole("checkbox", { name: /Voice 2/ }).uncheck();
    await apply.click();
    await expect(page.getByText("Narrator: Voice 1 (selected)", { exact: true })).toBeVisible();
    await page.reload();
    await page.getByRole("button", { name: "Undo narrator change" }).click();
    await expect(page.getByText("Narrator: Voice 1 + Voice 2 (merged)", { exact: true })).toBeVisible();
    await open();
    await dialog.getByRole("radio", { name: "Automatic (longest speaking voice)", exact: true }).check();
    await apply.click();
    await expect(page.getByText("Narrator: Voice 2 (automatic)", { exact: true })).toBeVisible();
    expect(bodies[2]).toEqual({ speaker_ids: null, expected_revision: "4" });
  });
}
