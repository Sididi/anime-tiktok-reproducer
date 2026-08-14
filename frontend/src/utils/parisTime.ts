import { TZDate } from "@date-fns/tz";
import {
  addDays,
  addMonths,
  format,
  isBefore,
  isSameDay,
  startOfDay,
  startOfMonth,
  startOfWeek,
} from "date-fns";

/**
 * Single source of truth for time in the planning UI.
 * All computations and display are pinned to Europe/Paris regardless of the
 * browser's timezone; instants cross the API as UTC ISO strings.
 */
export const PARIS_TZ = "Europe/Paris";

export function toParis(d: string | Date): TZDate {
  return new TZDate(typeof d === "string" ? new Date(d) : d, PARIS_TZ);
}

export function nowParis(): TZDate {
  return TZDate.tz(PARIS_TZ);
}

/** "yyyy-MM-dd" key of the Paris calendar day containing the instant. */
export function parisDayKey(d: string | Date): string {
  return format(toParis(d), "yyyy-MM-dd");
}

export function startOfDayParis(d: string | Date): TZDate {
  return startOfDay(toParis(d));
}

/** Monday 00:00 (Paris) of the week containing the instant. */
export function startOfWeekParis(d: string | Date): TZDate {
  return startOfWeek(toParis(d), { weekStartsOn: 1 });
}

export function addDaysParis(d: string | Date, days: number): TZDate {
  return addDays(toParis(d), days);
}

export function addMonthsParis(d: string | Date, months: number): TZDate {
  return addMonths(toParis(d), months);
}

export function startOfMonthParis(d: string | Date): TZDate {
  return startOfMonth(toParis(d));
}

/** The 7 Paris days (Mon–Sun) of the week containing `anchor`. */
export function weekDaysParis(anchor: string | Date): TZDate[] {
  const start = startOfWeekParis(anchor);
  return Array.from({ length: 7 }, (_, i) => addDays(start, i));
}

/** Monday-first 6-week (42 cell) grid covering the month containing `anchor`. */
export function monthGridParis(anchor: string | Date): TZDate[] {
  const start = startOfWeek(startOfMonth(toParis(anchor)), { weekStartsOn: 1 });
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

/**
 * Interpret a wall-clock time on a Paris calendar day and return the UTC
 * instant as ISO. This is the correct way to build an instant from user
 * input like ("2026-08-14", "18:30").
 */
export function parisWallTimeToUtcIso(dayKey: string, hhmm: string): string {
  const [y, m, d] = dayKey.split("-").map(Number);
  const [h, min] = hhmm.split(":").map(Number);
  return new TZDate(y, m - 1, d, h, min, 0, PARIS_TZ).toISOString();
}

export function isSameParisDay(a: string | Date, b: string | Date): boolean {
  return isSameDay(toParis(a), toParis(b));
}

export function isPastParis(d: string | Date): boolean {
  return isBefore(new Date(typeof d === "string" ? d : d.getTime()), new Date());
}

const timeFmt = new Intl.DateTimeFormat("fr-FR", {
  hour: "2-digit",
  minute: "2-digit",
  timeZone: PARIS_TZ,
});
const dayShortFmt = new Intl.DateTimeFormat("fr-FR", {
  weekday: "short",
  day: "numeric",
  timeZone: PARIS_TZ,
});
const dayLongFmt = new Intl.DateTimeFormat("fr-FR", {
  weekday: "long",
  day: "numeric",
  month: "long",
  timeZone: PARIS_TZ,
});
const dayMonthFmt = new Intl.DateTimeFormat("fr-FR", {
  day: "numeric",
  month: "short",
  timeZone: PARIS_TZ,
});
const monthYearFmt = new Intl.DateTimeFormat("fr-FR", {
  month: "long",
  year: "numeric",
  timeZone: PARIS_TZ,
});

/** "18:30" in Paris time. */
export function fmtTime(d: string | Date): string {
  return timeFmt.format(typeof d === "string" ? new Date(d) : d);
}

/** "lun. 24" */
export function fmtDayShort(d: string | Date): string {
  return dayShortFmt.format(typeof d === "string" ? new Date(d) : d);
}

/** "lundi 24 août" */
export function fmtDayLong(d: string | Date): string {
  return dayLongFmt.format(typeof d === "string" ? new Date(d) : d);
}

/** "août 2026" */
export function fmtMonthYear(d: string | Date): string {
  return monthYearFmt.format(typeof d === "string" ? new Date(d) : d);
}

/** "24 août – 30 août" for a week starting at `weekStart`. */
export function fmtWeekRange(weekStart: string | Date): string {
  const start = toParis(weekStart);
  const end = addDays(start, 6);
  return `${dayMonthFmt.format(start)} – ${dayMonthFmt.format(end)}`;
}

/** "Aujourd'hui" / "Demain" / "lundi 24 août". */
export function relativeDayLabel(d: string | Date): string {
  const key = parisDayKey(d);
  const today = nowParis();
  if (key === format(today, "yyyy-MM-dd")) return "Aujourd'hui";
  if (key === format(addDays(today, 1), "yyyy-MM-dd")) return "Demain";
  const label = fmtDayLong(d);
  return label.charAt(0).toUpperCase() + label.slice(1);
}
