import { useMemo } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

interface SlotPickerCalendarProps {
  monthAnchor: Date;       // any day inside the displayed month
  onPrevMonth: () => void;
  onNextMonth: () => void;
  selectedDate: Date | null;
  onSelectDate: (d: Date) => void;
  daysWithSlots?: Set<string>;  // ISO yyyy-mm-dd; if undefined, every day clickable
}

function fmtMonth(d: Date): string {
  return new Intl.DateTimeFormat("fr-FR", { month: "long", year: "numeric" }).format(d);
}

function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function SlotPickerCalendar({
  monthAnchor, onPrevMonth, onNextMonth,
  selectedDate, onSelectDate, daysWithSlots,
}: SlotPickerCalendarProps) {
  const grid = useMemo(() => {
    const first = new Date(monthAnchor.getFullYear(), monthAnchor.getMonth(), 1);
    const startOffset = (first.getDay() + 6) % 7; // Monday-first
    const start = new Date(first);
    start.setDate(first.getDate() - startOffset);
    return Array.from({ length: 42 }, (_, i) => {
      const d = new Date(start);
      d.setDate(start.getDate() + i);
      return d;
    });
  }, [monthAnchor]);

  const inMonth = (d: Date) => d.getMonth() === monthAnchor.getMonth();
  const isSelected = (d: Date) => !!selectedDate && ymd(d) === ymd(selectedDate);
  const hasSlots = (d: Date) =>
    daysWithSlots ? daysWithSlots.has(ymd(d)) : true;

  return (
    <div className="text-xs">
      <div className="flex items-center justify-between mb-2">
        <button onClick={onPrevMonth} className="p-1 rounded hover:bg-[hsl(var(--muted))]">
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="font-medium capitalize">{fmtMonth(monthAnchor)}</span>
        <button onClick={onNextMonth} className="p-1 rounded hover:bg-[hsl(var(--muted))]">
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
      <div className="grid grid-cols-7 gap-px text-center text-[10px] text-[hsl(var(--muted-foreground))] mb-1">
        {["L", "M", "M", "J", "V", "S", "D"].map((c, i) => <div key={i}>{c}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-px">
        {grid.map((d, i) => {
          const enabled = hasSlots(d);
          const dimmed = !inMonth(d) || !enabled;
          const sel = isSelected(d);
          return (
            <button
              key={i}
              type="button"
              disabled={!enabled}
              onClick={() => onSelectDate(d)}
              className={`h-7 rounded text-[11px] transition-colors ${
                sel
                  ? "bg-[hsl(var(--primary))] text-white"
                  : dimmed
                    ? "text-[hsl(var(--muted-foreground))]/40"
                    : "hover:bg-[hsl(var(--muted))]"
              }`}
            >
              {d.getDate()}
            </button>
          );
        })}
      </div>
    </div>
  );
}
