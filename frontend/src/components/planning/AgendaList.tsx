import { relativeDayLabel } from "@/utils/parisTime";
import { EventCard } from "./EventCard";
import type { EventGroup } from "./grouping";

interface AgendaListProps {
  groups: EventGroup[];
  selectedProjectId: string | null;
  onSelect: (group: EventGroup) => void;
}

/** Chronological "up next" list, grouped by Paris day. */
export function AgendaList({ groups, selectedProjectId, onSelect }: AgendaListProps) {
  const days: { dayKey: string; label: string; groups: EventGroup[] }[] = [];
  for (const g of groups) {
    const last = days[days.length - 1];
    if (last && last.dayKey === g.dayKey) {
      last.groups.push(g);
    } else {
      days.push({ dayKey: g.dayKey, label: relativeDayLabel(g.slot), groups: [g] });
    }
  }

  if (days.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-[hsl(var(--muted-foreground))]">
        Aucune publication à venir sur cette période.
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto pb-6">
      <div className="mx-auto flex max-w-xl flex-col">
        {days.map((day) => (
          <section key={day.dayKey} className="mb-4">
            <h3 className="sticky top-0 z-10 mb-2 bg-[hsl(var(--background))] py-1 text-xs font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
              {day.label}
            </h3>
            <div className="flex flex-col gap-2">
              {day.groups.map((g) => (
                <EventCard
                  key={g.key}
                  group={g}
                  variant="agenda"
                  selected={g.projectId === selectedProjectId}
                  onClick={onSelect}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
