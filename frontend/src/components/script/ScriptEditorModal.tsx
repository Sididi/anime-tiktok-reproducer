import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { X } from "lucide-react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Button } from "@/components/ui";
import { SceneHeader } from "./SceneHeaderExtension";
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
  title?: string;
  saveLabel?: string;
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
function buildTipTapDoc(
  scenes: SceneJsonEntry[],
  rawSceneIndices: Set<number>,
) {
  const content: Record<string, unknown>[] = [];

  for (const scene of scenes) {
    content.push({
      type: "sceneHeader",
      attrs: {
        sceneIndex: scene.scene_index,
        isRaw: rawSceneIndices.has(scene.scene_index),
      },
    });
    content.push({
      type: "paragraph",
      content: scene.text ? [{ type: "text", text: scene.text }] : [],
    });
  }

  return { type: "doc", content };
}

/**
 * Extract scene texts from the TipTap editor JSON.
 * Collects all paragraph text between consecutive sceneHeader nodes.
 * Raw scenes (by scene_index) are always returned as empty string.
 */
function extractScenesFromEditor(
  editorJson: Record<string, unknown>,
  rawSceneIndices: Set<number>,
): string[] {
  const content = editorJson.content as Array<Record<string, unknown>>;
  if (!content) return [];

  const scenes: string[] = [];
  let currentTexts: string[] = [];
  let inScene = false;
  let currentIsRaw = false;

  for (const node of content) {
    if (node.type === "sceneHeader") {
      if (inScene) {
        scenes.push(currentIsRaw ? "" : currentTexts.join(" ").trim());
      }
      const attrs = node.attrs as { sceneIndex: number };
      currentIsRaw = rawSceneIndices.has(attrs.sceneIndex);
      currentTexts = [];
      inScene = true;
    } else if (inScene && node.type === "paragraph") {
      if (!currentIsRaw) {
        const nodeContent = node.content as
          | Array<Record<string, unknown>>
          | undefined;
        if (nodeContent) {
          const text = nodeContent
            .filter((c) => c.type === "text")
            .map((c) => c.text as string)
            .join("");
          if (text) currentTexts.push(text);
        }
      }
    }
  }

  // Last scene
  if (inScene) {
    scenes.push(currentIsRaw ? "" : currentTexts.join(" ").trim());
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
  title = "Edit Script",
  saveLabel = "Save Changes",
}: ScriptEditorModalProps) {
  const [updateCounter, setUpdateCounter] = useState(0);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [scenePositions, setScenePositions] = useState<number[]>([]);

  // Parse the incoming JSON
  const parsedScript = useMemo<ParsedScript | null>(() => {
    try {
      return JSON.parse(scenesJson);
    } catch {
      return null;
    }
  }, [scenesJson]);

  // Set of scene indices that are raw (locked)
  const rawSceneIndices = useMemo(
    () =>
      new Set(
        transcription.scenes.filter((s) => s.is_raw).map((s) => s.scene_index),
      ),
    [transcription],
  );

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
    content:
      parsedScript && Array.isArray(parsedScript.scenes)
        ? buildTipTapDoc(parsedScript.scenes, rawSceneIndices)
        : "",
    onUpdate: () => {
      setUpdateCounter((c) => c + 1);
    },
  });

  // Reset content when modal opens
  useEffect(() => {
    if (
      isOpen &&
      editor &&
      parsedScript &&
      Array.isArray(parsedScript.scenes)
    ) {
      editor.commands.setContent(
        buildTipTapDoc(parsedScript.scenes, rawSceneIndices),
      );
      setUpdateCounter((c) => c + 1);
    }
  }, [isOpen, editor, parsedScript, rawSceneIndices]);

  // Measure scene header chip positions for aligning stats
  useEffect(() => {
    if (!isOpen || !editor) return;

    const measure = () => {
      const container = scrollContainerRef.current;
      if (!container) return;

      const chips = container.querySelectorAll(".scene-header-chip");
      if (chips.length === 0) return;

      const containerRect = container.getBoundingClientRect();
      const positions = Array.from(chips).map((chip) => {
        const chipRect = chip.getBoundingClientRect();
        return chipRect.top - containerRect.top + container.scrollTop;
      });
      setScenePositions(positions);
    };

    requestAnimationFrame(measure);
  }, [updateCounter, isOpen, editor]);

  // Live duration stats
  const sceneStats = useMemo(() => {
    if (
      !editor ||
      !parsedScript ||
      !Array.isArray(parsedScript.scenes) ||
      !transcription
    )
      return [];

    const editorJson = editor.getJSON();
    const texts = extractScenesFromEditor(
      editorJson as Record<string, unknown>,
      rawSceneIndices,
    );

    return parsedScript.scenes.map((scene, i) => {
      const isRaw = rawSceneIndices.has(scene.scene_index);
      const origScene = transcription.scenes[i];
      const originalDuration = origScene
        ? origScene.end_time - origScene.start_time
        : parseFloat(scene.duration_seconds) || 0;

      if (isRaw) {
        return {
          sceneIndex: scene.scene_index,
          isRaw: true,
          estimatedDuration: 0,
          originalDuration,
          deltaPct: 0,
          category: "green" as const,
        };
      }

      const newText = texts[i] || "";
      const estimatedDuration = estimateTtsDuration(newText, targetLanguage);
      const speedRatio =
        originalDuration > 0 ? estimatedDuration / originalDuration : 1;
      const deltaPct = (speedRatio - 1) * 100;
      const category = getSpeedCategory(speedRatio);

      return {
        sceneIndex: scene.scene_index,
        isRaw: false,
        estimatedDuration,
        originalDuration,
        deltaPct,
        category,
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    updateCounter,
    parsedScript,
    transcription,
    targetLanguage,
    editor,
    rawSceneIndices,
  ]);

  const totals = useMemo(() => {
    const nonRawStats = sceneStats.filter((s) => !s.isRaw);
    const totalEstimated = nonRawStats.reduce(
      (s, x) => s + x.estimatedDuration,
      0,
    );
    const totalOriginal = nonRawStats.reduce(
      (s, x) => s + x.originalDuration,
      0,
    );
    return { totalEstimated, totalOriginal };
  }, [sceneStats]);

  const handleSave = useCallback(() => {
    if (!editor || !parsedScript) return;

    const editorJson = editor.getJSON();
    const texts = extractScenesFromEditor(
      editorJson as Record<string, unknown>,
      rawSceneIndices,
    );

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
  }, [editor, parsedScript, rawSceneIndices, onSave, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-6xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content — single scroll container for linked scroll */}
        <div
          ref={scrollContainerRef}
          className="flex-1 overflow-y-auto min-h-0"
        >
          <div className="flex">
            {/* Editor panel */}
            <div className="flex-[7] p-4 border-r border-[hsl(var(--border))]">
              <EditorContent
                editor={editor}
                className="tiptap-editor prose prose-invert max-w-none min-h-[300px]"
              />
            </div>

            {/* Stats panel — absolutely positioned per-scene, aligned with editor */}
            <div className="flex-[3] relative">
              {scenePositions.length > 0 &&
                sceneStats.map((stat, i) => (
                  <div
                    key={stat.sceneIndex}
                    className="absolute left-0 right-0 px-4 font-mono text-xs whitespace-nowrap"
                    style={{ top: scenePositions[i] ?? 0 }}
                  >
                    {stat.isRaw ? (
                      <span className="text-[hsl(var(--muted-foreground))] opacity-50">
                        🔒 locked
                      </span>
                    ) : (
                      <>
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
                      </>
                    )}
                  </div>
                ))}
            </div>
          </div>
        </div>

        {/* Footer — totals + actions */}
        <div className="flex items-center justify-between p-4 border-t border-[hsl(var(--border))]">
          <div className="text-sm font-mono text-[hsl(var(--muted-foreground))]">
            Total: {totals.totalEstimated.toFixed(1)}s{" / "}
            {totals.totalOriginal.toFixed(1)}s
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={handleSave}>{saveLabel}</Button>
          </div>
        </div>
      </div>
    </div>
  );
}
