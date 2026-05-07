import { useEffect, useMemo, useRef } from "react";
import "temporal-polyfill/global";
import { ScheduleXCalendar, useNextCalendarApp } from "@schedule-x/react";
import { createViewWeek } from "@schedule-x/calendar";
import "@schedule-x/theme-default/dist/index.css";
import type { Account, Platform, PlanningEvent } from "@/types";
import { ALL_PLATFORMS } from "@/types";
import { platformBgHsl, PLATFORM_SHORT } from "./platformColors";

interface PlanningCalendarProps {
  events: PlanningEvent[];
  accounts: Account[];
  selectedAccountId: string | null;
  selectedPlatforms: Platform[];
  onEventClick: (
    event: PlanningEvent,
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

interface SxBaseEvent {
  id: string;
  title: string;
  start: Temporal.ZonedDateTime;
  end: Temporal.ZonedDateTime;
  calendarId: string;
  _kind: "group" | "background";
  _payload?: PlanningEvent;
  _members?: PlanningEvent[];
}

/** Group same-project events sharing the same slot (clean ISO) into one card. */
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

const ALL_CALENDARS = {
  ...PLATFORM_CALENDARS,
  group: {
    colorName: "group",
    lightColors: {
      main: "hsl(220 14% 35%)",
      container: "hsl(220 14% 35%)",
      onContainer: "#fff",
    },
    darkColors: {
      main: "hsl(220 14% 35%)",
      container: "hsl(220 14% 35%)",
      onContainer: "#fff",
    },
  },
  background: {
    colorName: "background",
    lightColors: {
      main: "transparent",
      container: "transparent",
      onContainer: "transparent",
    },
    darkColors: {
      main: "transparent",
      container: "transparent",
      onContainer: "transparent",
    },
  },
};

function slotForDay(
  ymd: string,
  hhmm: string,
): Temporal.ZonedDateTime | null {
  const [h, m] = hhmm.split(":").map((n) => Number.parseInt(n, 10));
  if (Number.isNaN(h)) return null;
  const [Y, M, D] = ymd.split("-").map((n) => Number.parseInt(n, 10));
  return Temporal.ZonedDateTime.from({
    timeZone: TZ,
    year: Y,
    month: M,
    day: D,
    hour: h,
    minute: Number.isFinite(m) ? m : 0,
  });
}

function ymdInTz(date: Date, tz: string): string {
  return new Intl.DateTimeFormat("fr-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function clearChildren(el: Element): void {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function PlanningCalendar({
  events,
  accounts,
  selectedAccountId,
  selectedPlatforms,
  onEventClick,
}: PlanningCalendarProps) {
  const eventsRef = useRef(events);
  eventsRef.current = events;

  const grouped = useMemo(() => groupEvents(events), [events]);

  const sxEvents = useMemo<SxBaseEvent[]>(() => {
    const out: SxBaseEvent[] = [];

    for (const [key, members] of grouped) {
      const first = members[0];
      const start = isoToZdt(first.slot);
      // 60 minutes gives the card enough vertical room for avatar + pills + title.
      const end = start.add({ minutes: 60 });
      const calendarId =
        members.find((m) => m.platform === "tiktok")?.platform ??
        members[0].platform;
      out.push({
        id: safeId(["g", key]),
        title: first.anime_title,
        start,
        end,
        calendarId,
        _kind: "group",
        _payload: first,
        _members: members,
      });
    }

    const visiblePlatforms = new Set(selectedPlatforms);
    const accountsToConsider = selectedAccountId
      ? accounts.filter((a) => a.id === selectedAccountId)
      : accounts;
    const todayParis = ymdInTz(new Date(), TZ);
    const days: string[] = [];
    {
      const [Y, M, D] = todayParis.split("-").map((n) => Number.parseInt(n, 10));
      for (let i = 0; i < 14; i++) {
        const d = new Date(Date.UTC(Y, M - 1, D + i));
        days.push(
          `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`,
        );
      }
    }
    const bookedKeys = new Set<string>();
    for (const ev of events) bookedKeys.add(`${ev.platform}@${ev.slot}`);

    for (const account of accountsToConsider) {
      const slotsByPlat = account.slots_by_platform ?? {};
      for (const platform of ALL_PLATFORMS) {
        if (!visiblePlatforms.has(platform)) continue;
        const slots = slotsByPlat[platform] ?? [];
        if (!slots.length) continue;
        for (const ymd of days) {
          for (const hhmm of slots) {
            const start = slotForDay(ymd, hhmm);
            if (!start) continue;
            const slotIso = start.toInstant().toString();
            if (bookedKeys.has(`${platform}@${slotIso}`)) continue;
            const end = start.add({ minutes: 30 });
            out.push({
              id: safeId(["bg", account.id, platform, ymd, hhmm]),
              title: "",
              start,
              end,
              calendarId: "background",
              _kind: "background",
            });
          }
        }
      }
    }

    return out;
  }, [grouped, events, accounts, selectedAccountId, selectedPlatforms]);

  const calendar = useNextCalendarApp({
    views: [createViewWeek()],
    defaultView: "week",
    locale: "fr-FR",
    firstDayOfWeek: 1,
    timezone: TZ,
    isDark: true,
    events: sxEvents,
    calendars: ALL_CALENDARS,
    callbacks: {
      onEventClick(sxEvent, uiEvent) {
        const internal = sxEvent as unknown as SxBaseEvent;
        if (internal._kind !== "group") return;
        const payload = internal._payload;
        if (!payload) return;
        const mouseEvent = uiEvent as MouseEvent;
        onEventClick(payload, {
          x: mouseEvent?.clientX ?? 0,
          y: mouseEvent?.clientY ?? 0,
        });
      },
    },
  });

  useEffect(() => {
    if (!calendar) return;
    calendar.events.set(sxEvents);
  }, [calendar, sxEvents]);

  // After ScheduleX renders, decorate group/background events with our pills.
  const containerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;

    const decorate = () => {
      const rendered = root.querySelectorAll<HTMLElement>(".sx__time-grid-event");
      for (const el of rendered) {
        const id = el.getAttribute("data-event-id") ?? "";
        if (id.startsWith("bg-")) {
          el.classList.add("atr-bg-slot");
          continue;
        }
        if (!id.startsWith("g-")) continue;
        if (el.dataset.atrDecorated === "1") continue;

        const match = sxEvents.find((e) => e.id === id);
        if (!match || match._kind !== "group" || !match._members) continue;

        el.classList.add("atr-group");
        el.dataset.atrDecorated = "1";

        const inner = el.querySelector<HTMLElement>(
          ".sx__time-grid-event-inner",
        );
        if (!inner) continue;
        clearChildren(inner);

        const first = match._members[0];

        const pillRow = document.createElement("div");
        pillRow.style.cssText =
          "display:flex;gap:3px;align-items:center;flex-wrap:wrap;";

        if (first.account_avatar_url) {
          const img = document.createElement("img");
          img.src = first.account_avatar_url;
          img.alt = "";
          img.style.cssText =
            "width:14px;height:14px;border-radius:50%;flex:0 0 14px;object-fit:cover;background:rgba(0,0,0,0.3);";
          pillRow.appendChild(img);
        }

        for (const m of match._members) {
          const pill = document.createElement("span");
          pill.textContent = PLATFORM_SHORT[m.platform];
          pill.style.cssText = `display:inline-flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;color:#fff;background:${platformBgHsl(m.platform)};`;
          pillRow.appendChild(pill);
        }
        inner.appendChild(pillRow);

        const title = document.createElement("div");
        title.textContent = first.anime_title;
        title.style.cssText =
          "font-size:11px;line-height:1.2;margin-top:2px;color:#fff;text-shadow:0 1px 1px rgba(0,0,0,0.4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        inner.appendChild(title);

        const t = document.createElement("div");
        const slotLocal = isoToZdt(first.slot)
          .toPlainTime()
          .toString({ smallestUnit: "minute" });
        t.textContent = slotLocal;
        t.style.cssText = "font-size:9px;opacity:0.8;color:#fff;";
        inner.appendChild(t);
      }
    };

    decorate();
    const mo = new MutationObserver(() => decorate());
    mo.observe(root, { childList: true, subtree: true });
    return () => mo.disconnect();
  }, [sxEvents]);

  // Center the grid on the typical posting window after mount.
  useEffect(() => {
    if (!calendar) return;
    let cancelled = false;
    let attempts = 0;
    const tick = () => {
      if (cancelled) return;
      attempts++;
      const root = containerRef.current;
      // ScheduleX week view has the scroll on `.sx__view-container`.
      const scroller = root?.querySelector<HTMLElement>(".sx__view-container");
      if (!scroller || scroller.scrollHeight <= scroller.clientHeight) {
        if (attempts < 60) requestAnimationFrame(tick);
        return;
      }
      const px = (11 / 24) * scroller.scrollHeight;
      scroller.scrollTop = Math.max(0, px - 80);
    };
    requestAnimationFrame(tick);
    return () => {
      cancelled = true;
    };
  }, [calendar]);

  return (
    <div className="planning-calendar h-full" ref={containerRef}>
      <ScheduleXCalendar calendarApp={calendar} />
    </div>
  );
}
