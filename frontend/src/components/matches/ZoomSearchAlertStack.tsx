import { ScanSearch } from "lucide-react";
import { useZoomSearchAlertStore } from "@/stores/zoomSearchAlertStore";

interface ZoomSearchAlertStackProps {
  projectId: string;
  onAlertClick: (sceneIndex: number) => void;
}

/**
 * Stacked completion alerts for extensive zoom searches (top-right, below
 * the transient toast slot). Clicking an alert teleports to the scene and
 * dismisses it; the scene card glows for as long as its alert is up.
 */
export function ZoomSearchAlertStack({
  projectId,
  onAlertClick,
}: ZoomSearchAlertStackProps) {
  const alerts = useZoomSearchAlertStore((state) => state.alerts);
  const visible = alerts.filter((alert) => alert.projectId === projectId);
  if (visible.length === 0) return null;

  return (
    <div className="fixed top-16 right-4 z-[130] flex w-80 flex-col gap-2">
      {visible.map((alert) => (
        <button
          key={alert.jobId}
          type="button"
          data-zoom-search-alert-card
          data-scene-index={alert.sceneIndex}
          onClick={() => onAlertClick(alert.sceneIndex)}
          className="flex items-start gap-3 rounded-lg border border-emerald-400/60 bg-[hsl(var(--card))] px-4 py-3 text-left shadow-lg transition-colors hover:bg-[hsl(var(--accent))]"
        >
          <ScanSearch className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" />
          <span className="min-w-0">
            <span className="block text-sm font-medium">
              Extensive zoom search finished — Scene {alert.sceneIndex + 1}
            </span>
            <span className="block text-xs text-[hsl(var(--muted-foreground))]">
              {alert.message ||
                (alert.changed
                  ? alert.applied
                    ? "Match updated — click to review"
                    : "Result saved as alternative (scene was edited)"
                  : "Existing match confirmed")}
            </span>
          </span>
        </button>
      ))}
    </div>
  );
}
