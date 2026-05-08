import { useMemo } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

interface SlotPickerCalendarProps {
  monthAnchor: Date;       // any day inside the displayed month
  onPrevMonth: () => void;
  onNextMonth: () => void;
  selectedDate: Date | null;
  onSelectDate: (d: Date) => void;
  /** ISO yyyy-mm-dd of days with at least one slot configured for the platform.
   *  When undefined, every day is treated as configured. */
  daysWithSlots?: Set<string>;
  /** ISO yyyy-mm-dd of days with at least one FREE slot still bookable. When
   *  undefined we don't strike anything based on availability. */
  daysWithFreeSlots?: Set<string>;
}

function fmtMonth(d: Date): string {
  return new Intl.DateTimeFormat("fr-FR", {
    month: "long",
    year: "numeric",
  }).format(d);
}

function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function startOfDay(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  return out;
}

export function SlotPickerCalendar({
  monthAnchor,
  onPrevMonth,
  onNextMonth,
  selectedDate,
  onSelectDate,
  daysWithSlots,
  daysWithFreeSlots,
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

  const today = startOfDay(new Date());
  const inMonth = (d: Date) => d.getMonth() === monthAnchor.getMonth();
  const isSelected = (d: Date) =>
    !!selectedDate && ymd(d) === ymd(selectedDate);
  const isPast = (d: Date) => startOfDay(d).getTime() < today.getTime();
  const isUnconfigured = (d: Date) =>
    daysWithSlots ? !daysWithSlots.has(ymd(d)) : false;
  const isFull = (d: Date) =>
    daysWithFreeSlots ? !daysWithFreeSlots.has(ymd(d)) : false;

  return (
    <div className="text-xs">
      <div className="flex items-center justify-between mb-2">
        <button
          onClick={onPrevMonth}
          className="p-1 rounded hover:bg-[hsl(var(--muted))]"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="font-medium capitalize">{fmtMonth(monthAnchor)}</span>
        <button
          onClick={onNextMonth}
          className="p-1 rounded hover:bg-[hsl(var(--muted))]"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
      <div className="grid grid-cols-7 gap-px text-center text-[10px] text-[hsl(var(--muted-foreground))] mb-1">
        {["L", "M", "M", "J", "V", "S", "D"].map((c, i) => (
          <div key={i}>{c}</div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-px">
        {grid.map((d, i) => {
          const past = isPast(d);
          const unconfigured = isUnconfigured(d);
          const full = isFull(d);
          const blocked = past || unconfigured || full;
          const offMonth = !inMonth(d);
          const sel = isSelected(d);
          const title = past
            ? "Date passée"
            : unconfigured
              ? "Aucun slot configuré ce jour"
              : full
                ? "Tous les slots sont déjà pris"
                : undefined;
          return (
            <button
              key={i}
              type="button"
              disabled={blocked}
              onClick={() => onSelectDate(d)}
              title={title}
              className={`relative h-7 rounded text-[11px] transition-colors ${
                sel
                  ? "bg-[hsl(var(--primary))] text-white"
                  : blocked
                    ? "text-[hsl(var(--muted-foreground))]/40 cursor-not-allowed"
                    : offMonth
                      ? "text-[hsl(var(--muted-foreground))]/60 hover:bg-[hsl(var(--muted))]"
                      : "hover:bg-[hsl(var(--muted))]"
              }`}
            >
              <span
                className={
                  blocked
                    ? "line-through decoration-[hsl(var(--muted-foreground))]/60"
                    : ""
                }
              >
                {d.getDate()}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
