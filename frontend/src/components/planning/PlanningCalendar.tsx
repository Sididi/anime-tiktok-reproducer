import { createContext, useContext, useEffect, useMemo, useRef } from "react";
import "temporal-polyfill/global";
import { ScheduleXCalendar, useNextCalendarApp } from "@schedule-x/react";
import { createViewMonthGrid, createViewWeek } from "@schedule-x/calendar";
import { createCurrentTimePlugin } from "@schedule-x/current-time";
import "@schedule-x/theme-default/dist/index.css";
import { CalendarX2 } from "lucide-react";
import type { Platform, PlanningEvent } from "@/types";
import { ALL_PLATFORMS } from "@/types";
import { platformBgHsl, PLATFORM_SHORT } from "./platformColors";

interface PlanningCalendarProps {
  events: PlanningEvent[];
  onEventClick: (
    grouped: { project_id: string; slot: string; members: PlanningEvent[] },
    anchor: { x: number; y: number },
  ) => void;
}

const TZ = "Europe/Paris";

function isoToZdt(iso: string): Temporal.ZonedDateTime {
  return Temporal.Instant.from(iso).toZonedDateTimeISO(TZ);
}

function safeId(parts: string[]): string {
  return parts.join("-").replace(/[^a-zA-Z0-9_-]/g, "-");
}

interface SxGroupEvent {
  id: string;
  title: string;
  start: Temporal.ZonedDateTime;
  end: Temporal.ZonedDateTime;
  calendarId: string;
  _members: PlanningEvent[];
}

function groupEvents(events: PlanningEvent[]): Map<string, PlanningEvent[]> {
  const map = new Map<string, PlanningEvent[]>();
  for (const ev of events) {
    const key = `${ev.project_id}@${ev.slot}`;
    const list = map.get(key);
    if (list) list.push(ev);
    else map.set(key, [ev]);
  }
  return map;
}

type GroupStatus = "scheduled" | "running" | "complete" | "overdue";

function groupStatus(members: PlanningEvent[]): GroupStatus {
  if (members.some((m) => m.status === "running")) return "running";
  if (members.every((m) => m.status === "complete")) return "complete";
  const isPast = new Date(members[0].slot).getTime() < Date.now();
  return isPast ? "overdue" : "scheduled";
}

const PLATFORM_CALENDARS = Object.fromEntries(
  ALL_PLATFORMS.map((p) => [
    p,
    {
      colorName: p,
      lightColors: {
        main: platformBgHsl(p),
        container: platformBgHsl(p),
        onContainer: "#fff",
      },
      darkColors: {
        main: platformBgHsl(p),
        container: platformBgHsl(p),
        onContainer: "#fff",
      },
    },
  ]),
) as Record<Platform, {
  colorName: string;
  lightColors: { main: string; container: string; onContainer: string };
  darkColors: { main: string; container: string; onContainer: string };
}>;

interface TimeGridEventProps {
  calendarEvent: SxGroupEvent;
}

function fmtSlotTime(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: TZ,
  }).format(new Date(iso));
}

/** ScheduleX's onEventClick delegation can be flaky once we render a React
 *  custom component into the slot — bind onClick directly to our card and
 *  read the handler from this context. */
const EventClickContext = createContext<
  | ((
      g: { project_id: string; slot: string; members: PlanningEvent[] },
      anchor: { x: number; y: number },
    ) => void)
  | null
>(null);

function StatusDot({ status }: { status: GroupStatus }) {
  if (status === "running") {
    return (
      <span
        title="Upload en cours"
        className="planning-status-pulse"
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: "hsl(142 71% 45%)",
          flex: "0 0 6px",
        }}
      />
    );
  }
  if (status === "complete") {
    return (
      <span
        title="Publié"
        style={{ fontSize: 9, lineHeight: 1, color: "hsl(142 71% 45%)" }}
      >
        ✓
      </span>
    );
  }
  if (status === "overdue") {
    return (
      <span
        title="Créneau passé — upload non confirmé"
        style={{ fontSize: 9, lineHeight: 1, color: "hsl(38 92% 50%)", fontWeight: 700 }}
      >
        !
      </span>
    );
  }
  return null;
}

function useGroupCardData(calendarEvent: SxGroupEvent) {
  const onClick = useContext(EventClickContext);
  const members = calendarEvent._members ?? [];
  const first = members[0];
  const ordered = [...members].sort(
    (a, b) =>
      ALL_PLATFORMS.indexOf(a.platform) - ALL_PLATFORMS.indexOf(b.platform),
  );
  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!first) return;
    onClick?.(
      { project_id: first.project_id, slot: first.slot, members },
      { x: e.clientX, y: e.clientY },
    );
  };
  return { members, first, ordered, handleClick };
}

function GroupEventCard({ calendarEvent }: TimeGridEventProps) {
  const { members, first, ordered, handleClick } =
    useGroupCardData(calendarEvent);
  if (!members.length) return null;
  const isManual = members.some((m) => m.manual);
  const status = groupStatus(members);
  return (
    <div
      style={{
        height: "100%",
        background: "hsl(var(--card))",
        border: isManual
          ? "1px dashed hsl(45 90% 55%)"
          : "1px solid hsl(var(--border))",
        borderRadius: 4,
        padding: "2px 4px 2px 4px",
        display: "flex",
        flexDirection: "column",
        gap: 1,
        overflow: "hidden",
        cursor: "pointer",
        opacity: status === "complete" ? 0.55 : 1,
      }}
      title={`${first.anime_title} — ${first.account_name}`}
      onClick={handleClick}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 3,
          flexWrap: "nowrap",
        }}
      >
        {first.account_avatar_url ? (
          <img
            src={first.account_avatar_url}
            alt=""
            style={{
              width: 12,
              height: 12,
              borderRadius: "50%",
              flex: "0 0 12px",
              objectFit: "cover",
              background: "rgba(255,255,255,0.1)",
            }}
          />
        ) : null}
        {ordered.map((m) => (
          <span
            key={m.platform}
            style={{
              display: "inline-flex",
              alignItems: "center",
              fontSize: 9,
              fontWeight: 600,
              padding: "1px 4px",
              borderRadius: 3,
              color: "#fff",
              background: platformBgHsl(m.platform),
              lineHeight: "1",
            }}
          >
            {PLATFORM_SHORT[m.platform]}
          </span>
        ))}
        {isManual && (
          <span
            title="Programmation manuelle"
            style={{
              fontSize: 9, fontWeight: 700, padding: "1px 4px",
              borderRadius: 3, color: "hsl(45 90% 55%)",
              border: "1px dashed hsl(45 90% 55%)", lineHeight: "1",
            }}
          >
            M
          </span>
        )}
      </div>
      <div
        style={{
          fontSize: 11,
          lineHeight: "1.15",
          color: "hsl(var(--foreground))",
          // Allow up to two lines so longer titles aren't cut on a single
          // line. Falls back to ellipsis only when even two lines overflow.
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
          wordBreak: "break-word",
          flex: "1 1 auto",
          minHeight: 0,
        }}
      >
        {first.anime_title}
      </div>
      <div
        style={{
          fontSize: 9,
          color: "hsl(var(--muted-foreground))",
          lineHeight: "1",
          display: "flex",
          alignItems: "center",
          gap: 3,
        }}
      >
        {fmtSlotTime(first.slot)}
        <StatusDot status={status} />
      </div>
    </div>
  );
}

/** Compact single-line chip for the month grid. */
function MonthGridEventCard({ calendarEvent }: TimeGridEventProps) {
  const { members, first, ordered, handleClick } =
    useGroupCardData(calendarEvent);
  if (!members.length) return null;
  const status = groupStatus(members);
  const isManual = members.some((m) => m.manual);
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 3,
        padding: "1px 4px",
        borderRadius: 3,
        background: "hsl(var(--card))",
        border: isManual
          ? "1px dashed hsl(45 90% 55%)"
          : "1px solid hsl(var(--border))",
        cursor: "pointer",
        overflow: "hidden",
        opacity: status === "complete" ? 0.55 : 1,
      }}
      title={`${first.anime_title} — ${first.account_name} · ${fmtSlotTime(first.slot)}`}
      onClick={handleClick}
    >
      <span
        style={{
          fontSize: 9,
          color: "hsl(var(--muted-foreground))",
          flex: "0 0 auto",
        }}
      >
        {fmtSlotTime(first.slot)}
      </span>
      {ordered.map((m) => (
        <span
          key={m.platform}
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: platformBgHsl(m.platform),
            flex: "0 0 6px",
          }}
          title={m.platform}
        />
      ))}
      <span
        style={{
          fontSize: 10,
          color: "hsl(var(--foreground))",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          minWidth: 0,
        }}
      >
        {first.anime_title}
      </span>
      <StatusDot status={status} />
    </div>
  );
}

export function PlanningCalendar({
  events,
  onEventClick,
}: PlanningCalendarProps) {
  const grouped = useMemo(() => groupEvents(events), [events]);

  const sxEvents = useMemo<SxGroupEvent[]>(() => {
    const out: SxGroupEvent[] = [];
    for (const [key, members] of grouped) {
      const first = members[0];
      const start = isoToZdt(first.slot);
      // 90 minutes of visual height (~45px at gridHeight=720) so the card
      // fits the avatar + platform pills + title + time legibly.
      const end = start.add({ minutes: 90 });
      const calendarId =
        members.find((m) => m.platform === "tiktok")?.platform ??
        members[0].platform;
      out.push({
        id: safeId(["g", key]),
        title: first.anime_title,
        start,
        end,
        calendarId,
        _members: members,
      });
    }
    return out;
  }, [grouped]);

  const onEventClickRef = useRef(onEventClick);
  onEventClickRef.current = onEventClick;
  // Stable callback for the context — always reads the latest prop via ref.
  const stableOnClick = useMemo(
    () =>
      (
        g: { project_id: string; slot: string; members: PlanningEvent[] },
        anchor: { x: number; y: number },
      ) => onEventClickRef.current(g, anchor),
    [],
  );

  const calendar = useNextCalendarApp({
    views: [createViewWeek(), createViewMonthGrid()],
    defaultView: "week",
    locale: "fr-FR",
    firstDayOfWeek: 1,
    timezone: TZ,
    isDark: true,
    events: sxEvents,
    calendars: PLATFORM_CALENDARS,
    plugins: [createCurrentTimePlugin()],
    weekOptions: {
      // Compact: ~30px/hour → 720px for 24h. With the trimmed header strip
      // the whole grid fits inside the 92vh modal body without scroll, and
      // a 90-min event card is tall enough for avatar + pills + 2-line
      // title + slot time.
      gridHeight: 720,
    },
  });

  useEffect(() => {
    if (!calendar) return;
    calendar.events.set(sxEvents);
  }, [calendar, sxEvents]);

  // ScheduleX's React wrapper destroys + re-renders the whole calendar
  // whenever `customComponents` is a new reference. Memoize so that doesn't
  // happen on every parent render (which would cause the visible flash and
  // wipe the click handlers between renders).
  const customComponents = useMemo(
    () => ({
      timeGridEvent: GroupEventCard,
      monthGridEvent: MonthGridEventCard,
    }),
    [],
  );

  return (
    <EventClickContext.Provider value={stableOnClick}>
      <div className="planning-calendar h-full relative">
        <ScheduleXCalendar
          calendarApp={calendar}
          customComponents={customComponents}
        />
        {events.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <div className="flex flex-col items-center gap-2 text-[hsl(var(--muted-foreground))] bg-[hsl(var(--card))]/80 rounded-lg px-6 py-4">
              <CalendarX2 className="h-8 w-8 opacity-50" />
              <span className="text-sm">Aucun upload planifié</span>
            </div>
          </div>
        )}
      </div>
    </EventClickContext.Provider>
  );
}
