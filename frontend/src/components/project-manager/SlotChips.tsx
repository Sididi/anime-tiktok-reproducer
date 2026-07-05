import type { FreeSlot } from "@/types";

interface SlotChipsProps {
  slots: FreeSlot[];
  selectedIso: string | null;
  onSelect: (iso: string) => void;
  onSelectTaken?: (slot: FreeSlot) => void;
  stolenIsos?: Set<string>;
  ownProjectId?: string;
}

const MIN_LEAD_MS = 30 * 60 * 1000;

function fmtTime(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function SlotChips({
  slots, selectedIso, onSelect, onSelectTaken, stolenIsos, ownProjectId,
}: SlotChipsProps) {
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
        const impossible = new Date(s.slot).getTime() < Date.now() + MIN_LEAD_MS;
        const mine = !!ownProjectId && s.taken_by_project_id === ownProjectId;
        const stealable =
          !s.available && !impossible && !mine && !!onSelectTaken;
        const stolen = stealable && !!stolenIsos?.has(s.slot);
        const taken = !s.available && !mine;
        const disabled = impossible || (taken && !stealable);

        const cls = selected || stolen
          ? stolen
            ? "border-amber-500 text-amber-500 bg-amber-500/10"
            : "border-[hsl(var(--primary))] text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10"
          : impossible
            ? "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] line-through opacity-60 cursor-not-allowed"
            : stealable
              ? "border-amber-500/60 text-amber-500 hover:bg-amber-500/10"
              : taken
                ? "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] line-through opacity-60 cursor-not-allowed"
                : "border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]";

        return (
          <button
            key={s.slot}
            type="button"
            disabled={disabled}
            title={
              impossible
                ? "Trop proche / passé"
                : stealable
                  ? `Occupé par « ${s.taken_by_title ?? s.taken_by_project_id} » — cliquer pour échanger`
                  : undefined
            }
            onClick={() => {
              if (stealable) onSelectTaken!(s);
              else onSelect(s.slot);
            }}
            className={`text-xs px-2.5 py-1 rounded border transition-colors ${cls}`}
          >
            {fmtTime(s.slot)}
          </button>
        );
      })}
    </div>
  );
}
