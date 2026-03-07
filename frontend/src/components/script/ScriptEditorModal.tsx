import { useState, useEffect, useMemo, useCallback } from "react";
import { Lock, X } from "lucide-react";
import { Button } from "@/components/ui";
import {
  estimateTtsDuration,
  getSpeedCategory,
  DELTA_COLORS,
} from "./durationEstimation";
import type { Transcription } from "@/types";

interface ScriptEditorModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (updatedJson: string) => void;
  scenesJson: string;
  transcription: Transcription;
  targetLanguage: string;
}

interface SceneJsonEntry {
  scene_index: number;
  text: string;
  duration_seconds?: string;
  estimated_word_count?: number;
  [key: string]: unknown;
}

interface ParsedScript {
  language: string;
  scenes: SceneJsonEntry[];
  [key: string]: unknown;
}

interface SceneDraft {
  scene_index: number;
  text: string;
  isRaw: boolean;
}

export function ScriptEditorModal({
  isOpen,
  onClose,
  onSave,
  scenesJson,
  transcription,
  targetLanguage,
}: ScriptEditorModalProps) {
  const [drafts, setDrafts] = useState<SceneDraft[]>([]);

  const parsedScript = useMemo<ParsedScript | null>(() => {
    try {
      return JSON.parse(scenesJson);
    } catch {
      return null;
    }
  }, [scenesJson]);

  const transcriptionByIndex = useMemo(
    () =>
      new Map(transcription.scenes.map((scene) => [scene.scene_index, scene])),
    [transcription],
  );

  useEffect(() => {
    if (!isOpen || !parsedScript || !Array.isArray(parsedScript.scenes)) {
      return;
    }
    setDrafts(
      parsedScript.scenes.map((scene) => {
        const transcriptionScene = transcriptionByIndex.get(scene.scene_index);
        return {
          scene_index: scene.scene_index,
          text: scene.text,
          isRaw: Boolean(transcriptionScene?.is_raw),
        };
      }),
    );
  }, [isOpen, parsedScript, transcriptionByIndex]);

  const sceneStats = useMemo(() => {
    return drafts.map((draft) => {
      const originalScene = transcriptionByIndex.get(draft.scene_index);
      const originalDuration = originalScene
        ? originalScene.end_time - originalScene.start_time
        : 0;

      if (draft.isRaw) {
        return {
          sceneIndex: draft.scene_index,
          isRaw: true,
          estimatedDuration: 0,
          originalDuration,
          deltaPct: 0,
          category: "green" as const,
        };
      }

      const estimatedDuration = estimateTtsDuration(draft.text, targetLanguage);
      const speedRatio =
        originalDuration > 0 ? estimatedDuration / originalDuration : 1;

      return {
        sceneIndex: draft.scene_index,
        isRaw: false,
        estimatedDuration,
        originalDuration,
        deltaPct: (speedRatio - 1) * 100,
        category: getSpeedCategory(speedRatio),
      };
    });
  }, [drafts, targetLanguage, transcriptionByIndex]);

  const totals = useMemo(() => {
    const editableStats = sceneStats.filter((stat) => !stat.isRaw);
    return {
      totalEstimated: editableStats.reduce((sum, stat) => sum + stat.estimatedDuration, 0),
      totalOriginal: editableStats.reduce((sum, stat) => sum + stat.originalDuration, 0),
    };
  }, [sceneStats]);

  const handleTextChange = useCallback((sceneIndex: number, text: string) => {
    setDrafts((prev) =>
      prev.map((draft) =>
        draft.scene_index === sceneIndex ? { ...draft, text } : draft,
      ),
    );
  }, []);

  const handleSave = useCallback(() => {
    if (!parsedScript) return;

    const draftByIndex = new Map(drafts.map((draft) => [draft.scene_index, draft]));
    const updatedScript: ParsedScript = {
      ...parsedScript,
      scenes: parsedScript.scenes.map((scene) => {
        const draft = draftByIndex.get(scene.scene_index);
        const nextText = draft?.isRaw ? "" : draft?.text ?? scene.text;
        return {
          ...scene,
          text: nextText,
          estimated_word_count: nextText.trim()
            ? nextText.trim().split(/\s+/).filter(Boolean).length
            : 0,
        };
      }),
    };

    onSave(JSON.stringify(updatedScript, null, 2));
    onClose();
  }, [drafts, onClose, onSave, parsedScript]);

  if (!isOpen || !parsedScript) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-6xl max-h-[90vh] overflow-hidden flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <div>
            <h2 className="text-lg font-semibold">Edit Script</h2>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Raw scenes are locked and kept empty.
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto min-h-0 p-4 space-y-4">
          {drafts.map((draft) => {
            const stat = sceneStats.find(
              (entry) => entry.sceneIndex === draft.scene_index,
            );
            return (
              <div
                key={draft.scene_index}
                className="border border-[hsl(var(--border))] rounded-lg bg-[hsl(var(--background))]"
              >
                <div className="flex items-center justify-between gap-4 border-b border-[hsl(var(--border))] px-4 py-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold">
                      Scene {draft.scene_index + 1}
                    </span>
                    {draft.isRaw && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[hsl(var(--muted))] px-2 py-0.5 text-xs text-[hsl(var(--muted-foreground))]">
                        <Lock className="h-3.5 w-3.5" />
                        Locked raw
                      </span>
                    )}
                  </div>
                  {stat &&
                    (stat.isRaw ? (
                      <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                        locked
                      </span>
                    ) : (
                      <div className="text-xs font-mono whitespace-nowrap">
                        <span>~{stat.estimatedDuration.toFixed(1)}s</span>
                        <span className="text-[hsl(var(--muted-foreground))]">
                          {" / "}
                          {stat.originalDuration.toFixed(1)}s
                        </span>
                        <span
                          className={`ml-1.5 font-semibold ${DELTA_COLORS[stat.category]}`}
                        >
                          {stat.deltaPct >= 0 ? "+" : ""}
                          {stat.deltaPct.toFixed(0)}%
                        </span>
                      </div>
                    ))}
                </div>
                <div className="p-4">
                  {draft.isRaw ? (
                    <div className="rounded-md border border-dashed border-[hsl(var(--border))] bg-[hsl(var(--muted))] px-4 py-3 text-sm text-[hsl(var(--muted-foreground))]">
                      Raw scene. No narration is allowed here.
                    </div>
                  ) : (
                    <textarea
                      value={draft.text}
                      onChange={(e) =>
                        handleTextChange(draft.scene_index, e.target.value)
                      }
                      rows={4}
                      className="w-full resize-y rounded-md border border-[hsl(var(--input))] bg-[hsl(var(--background))] p-3 text-sm"
                    />
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <div className="flex items-center justify-between p-4 border-t border-[hsl(var(--border))]">
          <div className="text-sm font-mono text-[hsl(var(--muted-foreground))]">
            Total editable scenes: {totals.totalEstimated.toFixed(1)}s
            {" / "}
            {totals.totalOriginal.toFixed(1)}s
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={handleSave}>Save Changes</Button>
          </div>
        </div>
      </div>
    </div>
  );
}
