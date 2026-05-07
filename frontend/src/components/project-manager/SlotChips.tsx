import type { FreeSlot } from "@/types";

interface SlotChipsProps {
  slots: FreeSlot[];
  selectedIso: string | null;
  onSelect: (iso: string) => void;
}

function fmtTime(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function SlotChips({ slots, selectedIso, onSelect }: SlotChipsProps) {
  if (!slots.length) {
    return (
      <div className="text-xs text-[hsl(var(--muted-foreground))] py-2">
        No slot configured this day
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {slots.map((s) => {
        const selected = s.slot === selectedIso;
        const taken = !s.available;
        const disabled = taken;
        return (
          <button
            key={s.slot}
            type="button"
            disabled={disabled}
            onClick={() => onSelect(s.slot)}
            className={`text-xs px-2.5 py-1 rounded border transition-colors ${
              selected
                ? "border-[hsl(var(--primary))] text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10"
                : taken
                  ? "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] line-through opacity-60 cursor-not-allowed"
                  : "border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]"
            }`}
          >
            {fmtTime(s.slot)}
          </button>
        );
      })}
    </div>
  );
}
