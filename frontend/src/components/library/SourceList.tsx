import { useMemo } from "react";
import type { SourceDetails } from "@/types";
import { SourceRow } from "./SourceRow";

interface SourceListProps {
  sources: SourceDetails[];
  selectedSource: string | null;
  onSelectSource: (seriesId: string) => void;
  onToggleProtection: (seriesId: string) => void;
  onUpdateSource: (source: SourceDetails) => void;
  onManageTorrents: (source: SourceDetails) => void;
  searchQuery: string;
}

export function SourceList({
  sources,
  selectedSource,
  onSelectSource,
  onToggleProtection,
  onUpdateSource,
  onManageTorrents,
  searchQuery,
}: SourceListProps) {
  const filtered = useMemo(
    () =>
      sources
        .filter((s) =>
          s.name.toLowerCase().includes(searchQuery.toLowerCase()),
        )
        .sort((a, b) =>
          a.name.localeCompare(b.name, undefined, {
            sensitivity: "base",
            numeric: true,
          }),
        ),
    [sources, searchQuery],
  );

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Column headers */}
      <div className="flex items-center gap-2 px-3 py-1 text-xs text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
        <div className="flex-1 min-w-0">Nom</div>
        <div className="w-20 shrink-0">Épisodes</div>
        <div className="w-14 shrink-0">FPS</div>
        <div className="w-16 shrink-0">Taille</div>
        <div className="w-36 shrink-0 text-right">Actions</div>
      </div>

      {/* Source rows */}
      <div className="flex-1 overflow-y-auto flex flex-col gap-1">
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center flex-1 text-sm text-[hsl(var(--muted-foreground))]">
            Aucune source trouvée
          </div>
        ) : (
          filtered.map((source) => (
            <SourceRow
              key={source.series_id}
              source={source}
              isSelected={selectedSource === source.series_id}
              onSelect={() => onSelectSource(source.series_id)}
              onTogglePin={() => onToggleProtection(source.series_id)}
              onUpdate={() => onUpdateSource(source)}
              onManageEpisodes={() => onManageTorrents(source)}
            />
          ))
        )}
      </div>
    </div>
  );
}
