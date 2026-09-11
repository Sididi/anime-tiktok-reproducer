import type { ScriptValidationIssue, Transcription } from "@/types";

export const hasNarration = (text: string) => /[\p{L}\p{N}]/u.test(text);

export function validateScriptPayload(payload: unknown, transcription: Transcription) {
  const issues: ScriptValidationIssue[] = [];
  const add = (code: ScriptValidationIssue["code"], message: string, scene_index: number | null = null) =>
    issues.push({ code, message, scene_index });
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    add("invalid_structure", "Script JSON root must be an object");
  } else {
    const scenes = (payload as Record<string, unknown>).scenes;
    if (!Array.isArray(scenes) || !scenes.length) {
      add("invalid_structure", 'JSON must contain a non-empty "scenes" array');
    } else {
      if (scenes.length !== transcription.scenes.length)
        add("invalid_structure", `Scene count mismatch: expected ${transcription.scenes.length}, got ${scenes.length}`);
      transcription.scenes.forEach((expected, position) => {
        const scene = scenes[position];
        const index = expected.scene_index;
        if (!scene || typeof scene !== "object" || Array.isArray(scene)) {
          add("invalid_structure", `Scene at position ${position} is not an object`, index);
          return;
        }
        if (!Number.isInteger(scene.scene_index) || scene.scene_index !== index)
          add("invalid_structure", `Scene index mismatch at position ${position}: expected ${index}, got ${scene.scene_index}`, index);
        if (typeof scene.text !== "string")
          add("invalid_structure", `Scene ${index} must contain a text string`, index);
        else if (expected.is_raw && scene.text.trim())
          add("raw_text", `Scene ${index} is raw and must keep an empty text`, index);
        else if (!expected.is_raw && !hasNarration(scene.text))
          add("empty_narration", `Scene ${index} must contain non-empty text (spoken words)`, index);
      });
    }
  }
  return {
    valid: issues.length === 0,
    error: issues.length ? issues.map((issue) => issue.message).join("; ") : null,
    issues,
    repairable: issues.length > 0 && issues.every((issue) => issue.code === "empty_narration"),
    editable: !issues.some((issue) => issue.code === "invalid_structure"),
  };
}

export function inspectScriptJson(value: string, transcription: Transcription | null) {
  if (!transcription) return null;
  try {
    return validateScriptPayload(JSON.parse(value), transcription);
  } catch {
    return {
      valid: false, error: "Invalid JSON", repairable: false, editable: false,
      issues: [{ code: "invalid_json", message: "Invalid JSON", scene_index: null }] as ScriptValidationIssue[],
    };
  }
}

export function narrationSignature(value: string): string {
  try {
    const parsed = JSON.parse(value);
    return JSON.stringify(parsed.scenes.map((s: { scene_index: number; text: string }) => [s.scene_index, s.text.trim()]));
  } catch {
    return value;
  }
}
