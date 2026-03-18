import { Search, Plus } from "lucide-react";

interface SearchBarProps {
  searchQuery: string;
  onSearchChange: (q: string) => void;
  onNewSource: () => void;
}

export function SearchBar({
  searchQuery,
  onSearchChange,
  onNewSource,
}: SearchBarProps) {
  return (
    <div className="flex items-center gap-3 bg-[hsl(var(--card))] rounded-lg px-4 py-2.5">
      <Search className="h-4 w-4 text-[hsl(var(--muted-foreground))] shrink-0" />

      <input
        type="text"
        value={searchQuery}
        onChange={(e) => onSearchChange(e.target.value)}
        placeholder="Rechercher une source..."
        className="flex-1 bg-transparent border-none outline-none text-sm placeholder:text-[hsl(var(--muted-foreground))]"
      />

      <button
        onClick={onNewSource}
        className="flex items-center gap-1.5 bg-green-600 hover:bg-green-700 text-white rounded-md px-3 py-1.5 text-sm font-medium transition-colors shrink-0"
      >
        <Plus className="h-4 w-4" />
        <span>Nouvelle source</span>
      </button>
    </div>
  );
}
