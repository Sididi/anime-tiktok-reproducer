import { ALL_PLATFORMS, type Platform } from "@/types";
import {
  PLATFORM_LABELS,
  PLATFORM_SHORT,
  platformBgHsl,
  platformTranslucentHsl,
} from "./platformColors";

interface PlatformCheckboxesProps {
  selected: Platform[];
  onChange: (next: Platform[]) => void;
}

export function PlatformCheckboxes({ selected, onChange }: PlatformCheckboxesProps) {
  const allSelected = selected.length === ALL_PLATFORMS.length;
  const toggleAll = () => onChange(allSelected ? [] : [...ALL_PLATFORMS]);
  const toggleOne = (p: Platform) =>
    onChange(selected.includes(p) ? selected.filter((x) => x !== p) : [...selected, p]);

  return (
    <div className="flex items-center gap-1 flex-wrap">
      <button
        type="button"
        onClick={toggleAll}
        className={`text-xs px-2 py-1 rounded border transition-colors ${
          allSelected
            ? "bg-[hsl(var(--secondary))] border-[hsl(var(--border))]"
            : "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))]"
        }`}
        aria-label="Toggle all platforms"
      >
        {allSelected ? "All" : "None"}
      </button>
      {ALL_PLATFORMS.map((p) => {
        const active = selected.includes(p);
        return (
          <button
            key={p}
            type="button"
            onClick={() => toggleOne(p)}
            className="text-xs px-2 py-1 rounded border transition-colors"
            style={{
              backgroundColor: active ? platformTranslucentHsl(p, 0.25) : "transparent",
              borderColor: active ? platformBgHsl(p) : "hsl(var(--border))",
              color: active ? platformBgHsl(p) : "hsl(var(--muted-foreground))",
            }}
            aria-pressed={active}
            title={PLATFORM_LABELS[p]}
          >
            {PLATFORM_SHORT[p]}
          </button>
        );
      })}
    </div>
  );
}
