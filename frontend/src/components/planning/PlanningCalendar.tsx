import { useEffect, useMemo, useRef } from "react";
import "temporal-polyfill/global";
import { ScheduleXCalendar, useNextCalendarApp } from "@schedule-x/react";
import { createViewWeek } from "@schedule-x/calendar";
import "@schedule-x/theme-default/dist/index.css";
import type { Platform, PlanningEvent } from "@/types";
import { platformBgHsl } from "./platformColors";

interface PlanningCalendarProps {
  events: PlanningEvent[];
  onEventClick: (event: PlanningEvent, anchor: { x: number; y: number }) => void;
}

const TZ = "Europe/Paris";

// ScheduleX 4 expects Temporal.ZonedDateTime|PlainDate for start/end. We anchor
// every event to Europe/Paris so the calendar renders the user-facing slot.
function isoToZdt(iso: string): Temporal.ZonedDateTime {
  return Temporal.Instant.from(iso).toZonedDateTimeISO(TZ);
}

// ScheduleX requires event ids to match /^[a-zA-Z0-9_-]*$/ — keep the project
// id and platform but replace any other characters with `-`.
function safeEventId(event: PlanningEvent, idx: number): string {
  const base = `${event.project_id}-${event.platform}-${idx}`;
  return base.replace(/[^a-zA-Z0-9_-]/g, "-");
}

interface SxEvent {
  id: string;
  title: string;
  start: Temporal.ZonedDateTime;
  end: Temporal.ZonedDateTime;
  calendarId: Platform;
  _payload: PlanningEvent;
}

function toScheduleXEvent(event: PlanningEvent, idx: number): SxEvent {
  const start = isoToZdt(event.slot);
  // Width: 30 minutes per event so adjacent slots don't visually overlap.
  const end = start.add({ minutes: 30 });
  return {
    id: safeEventId(event, idx),
    title: event.anime_title,
    start,
    end,
    calendarId: event.platform,
    _payload: event,
  };
}

const CALENDARS_BY_PLATFORM: Record<
  Platform,
  {
    colorName: string;
    lightColors: { main: string; container: string; onContainer: string };
    darkColors: { main: string; container: string; onContainer: string };
  }
> = {
  youtube: {
    colorName: "youtube",
    lightColors: {
      main: platformBgHsl("youtube"),
      container: platformBgHsl("youtube"),
      onContainer: "#fff",
    },
    darkColors: {
      main: platformBgHsl("youtube"),
      container: platformBgHsl("youtube"),
      onContainer: "#fff",
    },
  },
  facebook: {
    colorName: "facebook",
    lightColors: {
      main: platformBgHsl("facebook"),
      container: platformBgHsl("facebook"),
      onContainer: "#fff",
    },
    darkColors: {
      main: platformBgHsl("facebook"),
      container: platformBgHsl("facebook"),
      onContainer: "#fff",
    },
  },
  instagram: {
    colorName: "instagram",
    lightColors: {
      main: platformBgHsl("instagram"),
      container: platformBgHsl("instagram"),
      onContainer: "#fff",
    },
    darkColors: {
      main: platformBgHsl("instagram"),
      container: platformBgHsl("instagram"),
      onContainer: "#fff",
    },
  },
  tiktok: {
    colorName: "tiktok",
    lightColors: {
      main: platformBgHsl("tiktok"),
      container: platformBgHsl("tiktok"),
      onContainer: "#fff",
    },
    darkColors: {
      main: platformBgHsl("tiktok"),
      container: platformBgHsl("tiktok"),
      onContainer: "#fff",
    },
  },
};

export function PlanningCalendar({ events, onEventClick }: PlanningCalendarProps) {
  const eventsRef = useRef(events);
  eventsRef.current = events;

  const sxEvents = useMemo(() => events.map(toScheduleXEvent), [events]);

  const calendar = useNextCalendarApp({
    views: [createViewWeek()],
    defaultView: "week",
    locale: "fr-FR",
    firstDayOfWeek: 1,
    timezone: TZ,
    isDark: true,
    events: sxEvents,
    calendars: CALENDARS_BY_PLATFORM,
    callbacks: {
      // ScheduleX 4: onEventClick receives the external event + the UIEvent.
      onEventClick(sxEvent, uiEvent) {
        const payload =
          (sxEvent as unknown as SxEvent)._payload ??
          eventsRef.current.find(
            (ev, idx) => safeEventId(ev, idx) === String(sxEvent.id),
          );
        if (!payload) return;
        const mouseEvent = uiEvent as MouseEvent;
        onEventClick(payload, {
          x: mouseEvent?.clientX ?? 0,
          y: mouseEvent?.clientY ?? 0,
        });
      },
    },
  });

  // Keep events in sync as the parent reloads them.
  useEffect(() => {
    if (!calendar) return;
    calendar.events.set(sxEvents);
  }, [calendar, sxEvents]);

  return (
    <div className="planning-calendar h-full">
      <ScheduleXCalendar calendarApp={calendar} />
    </div>
  );
}
