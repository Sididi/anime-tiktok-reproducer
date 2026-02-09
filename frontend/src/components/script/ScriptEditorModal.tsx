import { useState, useEffect, useMemo, useCallback } from "react";
import { X } from "lucide-react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Button } from "@/components/ui";
import { SceneHeader } from "./SceneHeaderExtension";
import {
  estimateTtsDuration,
  getDeltaCategory,
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
  duration_seconds: string;
  estimated_word_count: number;
  [key: string]: unknown;
}

interface ParsedScript {
  language: string;
  scenes: SceneJsonEntry[];
  [key: string]: unknown;
}

/**
 * Build a TipTap-compatible JSON document from parsed scene data.
 * Alternates sceneHeader nodes with paragraph nodes containing the scene text.
 */
function buildTipTapDoc(scenes: SceneJsonEntry[]) {
  const content: Record<string, unknown>[] = [];

  for (const scene of scenes) {
    content.push({
      type: "sceneHeader",
      attrs: { sceneIndex: scene.scene_index },
    });
    content.push({
      type: "paragraph",
      content: scene.text
        ? [{ type: "text", text: scene.text }]
        : [],
    });
  }

  return { type: "doc", content };
}

/**
 * Extract scene texts from the TipTap editor JSON.
 * Collects all paragraph text between consecutive sceneHeader nodes.
 */
function extractScenesFromEditor(
  editorJson: Record<string, unknown>,
): string[] {
  const content = editorJson.content as Array<Record<string, unknown>>;
  if (!content) return [];

  const scenes: string[] = [];
  let currentTexts: string[] = [];
  let inScene = false;

  for (const node of content) {
    if (node.type === "sceneHeader") {
      if (inScene) {
        scenes.push(currentTexts.join(" ").trim());
      }
      currentTexts = [];
      inScene = true;
    } else if (inScene && node.type === "paragraph") {
      const nodeContent = node.content as Array<Record<string, unknown>> | undefined;
      if (nodeContent) {
        const text = nodeContent
          .filter((c) => c.type === "text")
          .map((c) => c.text as string)
          .join("");
        if (text) currentTexts.push(text);
      }
    }
  }

  // Last scene
  if (inScene) {
    scenes.push(currentTexts.join(" ").trim());
  }

  return scenes;
}

export function ScriptEditorModal({
  isOpen,
  onClose,
  onSave,
  scenesJson,
  transcription,
  targetLanguage,
}: ScriptEditorModalProps) {
  const [updateCounter, setUpdateCounter] = useState(0);

  // Parse the incoming JSON
  const parsedScript = useMemo<ParsedScript | null>(() => {
    try {
      return JSON.parse(scenesJson);
    } catch {
      return null;
    }
  }, [scenesJson]);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        codeBlock: false,
        code: false,
        blockquote: false,
        horizontalRule: false,
        bold: false,
        italic: false,
        strike: false,
      }),
      SceneHeader,
    ],
    content: parsedScript ? buildTipTapDoc(parsedScript.scenes) : "",
    onUpdate: () => {
      setUpdateCounter((c) => c + 1);
    },
  });

  // Reset content when modal opens
  useEffect(() => {
    if (isOpen && editor && parsedScript) {
      editor.commands.setContent(buildTipTapDoc(parsedScript.scenes));
      setUpdateCounter((c) => c + 1);
    }
  }, [isOpen, editor, parsedScript]);

  // Live duration stats
  const sceneStats = useMemo(() => {
    if (!editor || !parsedScript || !transcription) return [];

    const editorJson = editor.getJSON();
    const texts = extractScenesFromEditor(editorJson as Record<string, unknown>);

    return parsedScript.scenes.map((scene, i) => {
      const newText = texts[i] || "";
      const estimatedDuration = estimateTtsDuration(newText, targetLanguage);

      // Original duration from transcription
      const origScene = transcription.scenes[i];
      const originalDuration = origScene
        ? origScene.end_time - origScene.start_time
        : parseFloat(scene.duration_seconds) || 0;

      const deltaPct =
        originalDuration > 0
          ? ((estimatedDuration - originalDuration) / originalDuration) * 100
          : 0;
      const category = getDeltaCategory(deltaPct);

      return {
        sceneIndex: scene.scene_index,
        estimatedDuration,
        originalDuration,
        deltaPct,
        category,
        wordCount: newText.trim().split(/\s+/).filter(Boolean).length,
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updateCounter, parsedScript, transcription, targetLanguage, editor]);

  const totals = useMemo(() => {
    const totalEstimated = sceneStats.reduce((s, x) => s + x.estimatedDuration, 0);
    const totalOriginal = sceneStats.reduce((s, x) => s + x.originalDuration, 0);
    return { totalEstimated, totalOriginal };
  }, [sceneStats]);

  const handleSave = useCallback(() => {
    if (!editor || !parsedScript) return;

    const editorJson = editor.getJSON();
    const texts = extractScenesFromEditor(editorJson as Record<string, unknown>);

    const updatedScript: ParsedScript = {
      ...parsedScript,
      scenes: parsedScript.scenes.map((scene, i) => ({
        ...scene,
        text: texts[i] || "",
        estimated_word_count: (texts[i] || "")
          .trim()
          .split(/\s+/)
          .filter(Boolean).length,
      })),
    };

    onSave(JSON.stringify(updatedScript, null, 2));
    onClose();
  }, [editor, parsedScript, onSave, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-6xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <h2 className="text-lg font-semibold">Edit Script</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-hidden flex min-h-0">
          {/* Left panel - Editor */}
          <div className="flex-[7] overflow-y-auto p-4 border-r border-[hsl(var(--border))]">
            <EditorContent
              editor={editor}
              className="tiptap-editor prose prose-invert max-w-none min-h-[300px]"
            />
          </div>

          {/* Right panel - Duration stats */}
          <div className="flex-[3] overflow-y-auto p-4 space-y-2">
            <h3 className="text-sm font-semibold text-[hsl(var(--muted-foreground))] uppercase tracking-wide mb-3">
              Duration Estimates
            </h3>

            {sceneStats.map((stat) => (
              <div
                key={stat.sceneIndex}
                className="flex items-center justify-between py-1.5 px-2 rounded hover:bg-[hsl(var(--muted))] text-sm"
              >
                <span className="font-medium text-[hsl(var(--muted-foreground))]">
                  Scene {stat.sceneIndex + 1}
                </span>
                <div className="text-right font-mono text-xs">
                  <span>~{stat.estimatedDuration.toFixed(1)}s</span>
                  <span className="text-[hsl(var(--muted-foreground))]">
                    {" / "}
                    {stat.originalDuration.toFixed(1)}s
                  </span>
                  <span className={`ml-1.5 font-semibold ${DELTA_COLORS[stat.category]}`}>
                    {stat.deltaPct >= 0 ? "+" : ""}
                    {stat.deltaPct.toFixed(0)}%
                  </span>
                </div>
              </div>
            ))}

            {/* Separator */}
            <div className="border-t border-[hsl(var(--border))] my-2" />

            {/* Totals */}
            <div className="flex items-center justify-between py-1.5 px-2 text-sm font-semibold">
              <span>Total</span>
              <div className="text-right font-mono text-xs">
                <span>{totals.totalEstimated.toFixed(1)}s</span>
                <span className="text-[hsl(var(--muted-foreground))]">
                  {" / "}
                  {totals.totalOriginal.toFixed(1)}s
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-2 p-4 border-t border-[hsl(var(--border))]">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave}>Save Changes</Button>
        </div>
      </div>
    </div>
  );
}
