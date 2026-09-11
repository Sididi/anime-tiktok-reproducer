import type { ScriptRepairReport } from "@/types";

export function ScriptRepairNotice({ report }: { report: ScriptRepairReport | null | undefined }) {
  if (!report || report.status === "not_needed") return null;
  return (
    <div aria-label="Script repair details" className="rounded border border-amber-500/40 bg-amber-500/5 p-3 text-sm space-y-2">
      {report.changes.length > 0 && <p>
        {report.changes.length} scene{report.changes.length === 1 ? "" : "s"} repaired.
        {report.review_required && " Review the changes before generating audio."}
      </p>}
      {report.warning && <p>{report.warning}</p>}
      {report.unresolved_scene_indices.length > 0 && <p>
        Still missing narration: {report.unresolved_scene_indices.map((i) => `Scene ${i + 1}`).join(", ")}.
      </p>}
      {report.source_gap_scene_indices.length > 0 && <p>
        Source text was missing in {report.source_gap_scene_indices.map((i) => `Scene ${i + 1}`).join(", ")}.
        {" "}Check these against the video; repairs use neighboring narration.
      </p>}
      {report.changes.length > 0 && <details>
        <summary className="cursor-pointer font-medium">Before / after</summary>
        <div className="max-h-48 overflow-y-auto space-y-3 mt-2">
          {report.changes.map((change) => <div key={change.scene_index}>
            <strong>Scene {change.scene_index + 1}</strong>
            <p className="text-[hsl(var(--muted-foreground))]">Before: {change.before || "(empty)"}</p>
            <p>After: {change.after}</p>
          </div>)}
        </div>
      </details>}
    </div>
  );
}
