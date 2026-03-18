import { FolderKanban } from "lucide-react";
import type { LibraryType } from "@/types";
import { LIBRARY_TYPE_OPTIONS } from "@/utils/libraryTypes";

interface LibraryHeaderProps {
  selectedType: LibraryType;
  onTypeChange: (type: LibraryType) => void;
  onOpenProjectManager: () => void;
  onOpenPurge: () => void;
}

export function LibraryHeader({
  selectedType,
  onTypeChange,
  onOpenProjectManager,
  onOpenPurge,
}: LibraryHeaderProps) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-[hsl(var(--card))] px-4 py-3">
      <span className="font-bold text-lg text-[hsl(var(--primary))]">
        Anime TikTok Reproducer
      </span>

      <div className="w-px h-6 bg-[hsl(var(--border))]" />

      <select
        value={selectedType}
        onChange={(e) => onTypeChange(e.target.value as LibraryType)}
        className="bg-[hsl(var(--secondary))] text-[hsl(var(--primary))] rounded px-2 py-1 text-sm border-none outline-none cursor-pointer"
      >
        {LIBRARY_TYPE_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>

      <div className="flex-1" />

      <button
        onClick={onOpenProjectManager}
        className="flex items-center gap-1.5 bg-[hsl(var(--secondary))] rounded px-3 py-1.5 text-sm hover:bg-[hsl(var(--secondary))]/80 transition-colors"
      >
        <FolderKanban className="h-4 w-4" />
        <span>Projects</span>
      </button>

      <button
        onClick={onOpenPurge}
        className="rounded px-3 py-1.5 text-sm text-[hsl(var(--destructive))] bg-[hsl(var(--destructive))]/10 hover:bg-[hsl(var(--destructive))]/20 transition-colors"
      >
        Purge
      </button>
    </div>
  );
}
