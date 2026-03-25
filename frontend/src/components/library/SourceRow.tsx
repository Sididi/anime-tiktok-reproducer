import { HardDrive, ShieldCheck, FolderDown, Cable, Loader2 } from "lucide-react";
import type { SourceDetails } from "@/types";

interface SourceRowProps {
  source: SourceDetails;
  isSelected: boolean;
  onSelect: () => void;
  onTogglePin: () => void;
  onUpdate: () => void;
  onManageEpisodes: () => void;
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
  onTogglePin,
  onUpdate,
  onManageEpisodes,
}: SourceRowProps) {
  const hydrationInProgress =
    source.hydration_status === "hydrating_index" ||
    source.hydration_status === "hydrating_episodes";
  const episodeLabel =
    source.local_episode_count > 0 &&
    source.local_episode_count !== source.episode_count
      ? `${source.local_episode_count}/${source.episode_count} ep.`
      : `${source.episode_count} ep.`;

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
      </div>

      {/* Episode count */}
      <span className="text-sm text-[hsl(var(--muted-foreground))] w-20 shrink-0">
        {episodeLabel}
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
      <div className="flex gap-1 justify-end w-32 shrink-0">
        {source.is_fully_local && (
          <div
            className="p-1 mr-auto text-sky-400 opacity-30"
            title="Tous les épisodes sont disponibles localement"
          >
            <HardDrive className="h-4 w-4" />
          </div>
        )}
        {hydrationInProgress && (
          <div
            className="p-1 text-sky-400 opacity-70"
            title="Hydratation locale en cours"
          >
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onTogglePin();
          }}
          className={`p-1 rounded transition-colors hover:bg-[hsl(var(--secondary))] ${
            source.permanent_pin
              ? "text-green-500 opacity-100"
              : "text-[hsl(var(--muted-foreground))] opacity-30 hover:opacity-60"
          }`}
          title={
            source.permanent_pin
              ? "Épinglé localement"
              : "Protéger de l'éviction locale"
          }
        >
          <ShieldCheck className="h-4 w-4" />
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onManageEpisodes();
          }}
          className="p-1 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--secondary))] hover:text-[hsl(var(--foreground))] transition-colors"
          title="Gérer les épisodes"
        >
          <Cable className="h-4 w-4" />
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
