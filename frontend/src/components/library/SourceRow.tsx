import { AlertTriangle, ShieldCheck, FolderDown } from "lucide-react";
import type { SourceDetails } from "@/types";

interface SourceRowProps {
  source: SourceDetails;
  isSelected: boolean;
  onSelect: () => void;
  onToggleProtection: () => void;
  onUpdate: () => void;
}

function formatBytes(bytes: number): string {
  const tb = 1024 * 1024 * 1024 * 1024;
  const gb = 1024 * 1024 * 1024;
  const mb = 1024 * 1024;

  if (bytes >= tb) {
    return `${(bytes / tb).toFixed(1)} TB`;
  }
  if (bytes >= gb) {
    return `${(bytes / gb).toFixed(1)} GB`;
  }
  return `${Math.round(bytes / mb)} MB`;
}

export function SourceRow({
  source,
  isSelected,
  onSelect,
  onToggleProtection,
  onUpdate,
}: SourceRowProps) {
  return (
    <div
      onClick={onSelect}
      className={`flex items-center gap-2 rounded px-3 py-2 cursor-pointer transition-colors ${
        isSelected
          ? "bg-[hsl(var(--primary))]/10 ring-1 ring-[hsl(var(--primary))]"
          : "bg-[hsl(var(--card))] hover:bg-[hsl(var(--secondary))]/50"
      }`}
    >
      {/* Source name */}
      <div className="flex items-center gap-1 flex-1 min-w-0">
        <span className="font-semibold truncate">{source.name}</span>
        {source.missing_episodes > 0 && (
          <AlertTriangle
            className="text-amber-500 h-4 w-4 shrink-0"
            title={`${source.missing_episodes} épisode(s) manquant(s)`}
          />
        )}
      </div>

      {/* Episode count */}
      <span className="text-sm text-[hsl(var(--muted-foreground))] w-20 shrink-0">
        {source.episode_count} ep.
      </span>

      {/* FPS */}
      <span className="text-sm text-[hsl(var(--muted-foreground))] w-14 shrink-0">
        {source.fps} fps
      </span>

      {/* Size */}
      <span className="text-sm text-[hsl(var(--muted-foreground))] w-16 shrink-0">
        {formatBytes(source.total_size_bytes)}
      </span>

      {/* Actions */}
      <div className="flex gap-1 justify-end w-20 shrink-0">
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggleProtection();
          }}
          className={`p-1 rounded transition-colors hover:bg-[hsl(var(--secondary))] ${
            source.purge_protected
              ? "text-green-500 opacity-100"
              : "text-[hsl(var(--muted-foreground))] opacity-30 hover:opacity-60"
          }`}
          title={
            source.purge_protected
              ? "Protégé contre la purge"
              : "Non protégé contre la purge"
          }
        >
          <ShieldCheck className="h-4 w-4" />
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onUpdate();
          }}
          className="p-1 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--secondary))] hover:text-[hsl(var(--foreground))] transition-colors"
          title="Mettre à jour les épisodes"
        >
          <FolderDown className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
