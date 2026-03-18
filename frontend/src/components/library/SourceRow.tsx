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
      className={`flex items-center gap-2 bg-[hsl(var(--card))] rounded px-2 py-2 border-l-[3px] ${
        isSelected
          ? "border-l-[hsl(var(--primary))]"
          : "border-l-transparent"
      }`}
    >
      {/* Radio checkbox */}
      <div
        role="radio"
        aria-checked={isSelected}
        tabIndex={0}
        onClick={onSelect}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onSelect();
          }
        }}
        className={`shrink-0 w-[18px] h-[18px] rounded-full border-2 flex items-center justify-center cursor-pointer transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] ${
          isSelected
            ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))]/20"
            : "border-[hsl(var(--border))]"
        }`}
      >
        {isSelected && (
          <div className="w-2 h-2 rounded-full bg-[hsl(var(--primary))]" />
        )}
      </div>

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
          onClick={onToggleProtection}
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
          onClick={onUpdate}
          className="p-1 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--secondary))] hover:text-[hsl(var(--foreground))] transition-colors"
          title="Mettre à jour les épisodes"
        >
          <FolderDown className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
