# Planning System — Phase 2: Planning Modal Frontend

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Planning modal UI: a forward-looking week calendar across all accounts/platforms, with account + platform filters, click-event popover for cancel/reschedule.

**Architecture:** A new `frontend/src/components/planning/` directory hosting the modal, a ScheduleX week calendar wrapper, a popover, and platform-color tokens. The modal opens from a new "Planning" button in `LibraryHeader`, identical pattern to "Projects". State is local to the modal; data is fetched on open against the Phase 1 backend (`/api/scheduling/events`, `/api/scheduling/projects/.../platforms/...`).

**Tech Stack:** React 19, TypeScript, Tailwind, framer-motion, lucide-react (existing), `@schedule-x/react` + `@schedule-x/calendar` + `@schedule-x/theme-default` (new).

**Spec:** [docs/superpowers/specs/2026-05-07-planning-system-design.md](../specs/2026-05-07-planning-system-design.md)

**Prerequisite:** Phase 1 (backend) merged.

---

## File Structure

**New files:**
- `frontend/src/components/planning/PlanningModal.tsx` — main modal
- `frontend/src/components/planning/PlanningHeader.tsx` — top bar (account selector reuse + platform checkboxes)
- `frontend/src/components/planning/PlatformCheckboxes.tsx` — multi-select with select-all
- `frontend/src/components/planning/PlanningCalendar.tsx` — ScheduleX wrapper
- `frontend/src/components/planning/EventPopover.tsx` — detail popover with action buttons
- `frontend/src/components/planning/platformColors.ts` — color token helpers
- `frontend/src/components/planning/index.ts` — barrel export
- `frontend/e2e/planning.spec.ts` — Playwright

**Modified files:**
- `frontend/package.json` — add ScheduleX deps
- `frontend/src/index.css` — add platform CSS vars + ScheduleX theme overrides
- `frontend/src/types/index.ts` — add `Platform`, `PlanningEvent`, `FreeSlot` types
- `frontend/src/api/client.ts` — add scheduling API methods
- `frontend/src/components/library/LibraryHeader.tsx` — add Planning button
- `frontend/src/App.tsx` — add `<PlanningModal>` mount + open state

**Deferred to Phase 3:** the slot picker reused for "Reschedule whole project" — Phase 2 wires `Reschedule whole project` to a stub that shows a toast "Available in Phase 3" until Phase 3 lands. `Reschedule this slot` (single-platform) is fully implemented in Phase 2 since it does not require the TT-anchored picker.

---

## Conventions

- Run a single Playwright test: `cd frontend && npx playwright test e2e/planning.spec.ts -g "<name>"`
- Run all e2e: `cd frontend && npm run test`
- The Vite dev server (started by Playwright config) reuses `pixi run backend` mocks via `window.fetch` patching, identical to [frontend/e2e/project-manager-upload-queue.spec.ts](frontend/e2e/project-manager-upload-queue.spec.ts).

---

## Task 1: Install `@schedule-x/*` dependencies

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: Install**

```bash
cd frontend && npm install @schedule-x/react @schedule-x/calendar @schedule-x/theme-default
```

- [ ] **Step 2: Verify the dev build still compiles**

```bash
cd frontend && npm run build
```

Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "deps(frontend): add @schedule-x for the Planning week view"
```

---

## Task 2: Add types for Planning

**Files:**
- Modify: `frontend/src/types/index.ts`

- [ ] **Step 1: Locate the types module**

```bash
grep -rn "export type\|export interface" frontend/src/types/ | head
```

- [ ] **Step 2: Append the new types**

Add to `frontend/src/types/index.ts` (or whichever index re-exports types — match the existing convention):

```ts
export type Platform = "youtube" | "facebook" | "instagram" | "tiktok";

export const ALL_PLATFORMS: readonly Platform[] = [
  "youtube",
  "facebook",
  "instagram",
  "tiktok",
] as const;

export interface PlanningEvent {
  project_id: string;
  anime_title: string;
  account_id: string;
  account_avatar_url: string;
  account_name: string;
  platform: Platform;
  slot: string;            // ISO; clean time (no jitter), shown to user
  scheduled_at: string;    // ISO with jitter; hidden from UI
  drive_folder_url: string | null;
  status: "scheduled" | "running" | "complete";
}

export interface FreeSlot {
  slot: string;
  available: boolean;
  taken_by_project_id?: string;
}

export interface ResolveAnchorResolvedSlot {
  slot: string;
  scheduled_at: string;
  available: boolean;
}

export interface ResolveAnchorResult {
  resolved: Partial<Record<Platform, ResolveAnchorResolvedSlot>>;
  conflicts: Array<{ platform: Platform; reason: string }>;
}

export interface CascadePreview {
  per_platform: Array<{
    platform: Platform;
    target_slot: string;
    target_scheduled_at: string;
    displaced: Array<{
      project_id: string;
      anime_title: string;
      from_slot: string;
      to_slot: string;
      requires_platform_notification: boolean;
    }>;
  }>;
  blockers: Array<{ platform: Platform; reason: string }>;
}
```

- [ ] **Step 3: Verify type-check passes**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts
git commit -m "feat(types): add Planning-related shared types"
```

---

## Task 3: Add scheduling API client methods

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the methods**

Locate the `api` object in [frontend/src/api/client.ts](frontend/src/api/client.ts) (search for `export const api = {`). Add inside it:

```ts
  async listPlanningEvents(params: {
    account_id?: string | null;
    platforms?: import("@/types").Platform[];
    range_start?: string;
    range_end?: string;
  } = {}): Promise<{ events: import("@/types").PlanningEvent[] }> {
    const usp = new URLSearchParams();
    if (params.account_id) usp.set("account_id", params.account_id);
    if (params.platforms?.length) usp.set("platforms", params.platforms.join(","));
    if (params.range_start) usp.set("range_start", params.range_start);
    if (params.range_end) usp.set("range_end", params.range_end);
    const qs = usp.toString();
    return request(`/scheduling/events${qs ? `?${qs}` : ""}`);
  },

  async listFreeSlots(params: {
    account_id: string;
    platform: import("@/types").Platform;
    after: string;
    limit?: number;
  }): Promise<{ slots: import("@/types").FreeSlot[] }> {
    const usp = new URLSearchParams({
      account_id: params.account_id,
      platform: params.platform,
      after: params.after,
      limit: String(params.limit ?? 20),
    });
    return request(`/scheduling/free-slots?${usp.toString()}`);
  },

  async resolveAnchor(payload: {
    project_id: string;
    account_id: string;
    tiktok_slot: string;
    overrides?: Partial<Record<import("@/types").Platform, string>>;
  }): Promise<import("@/types").ResolveAnchorResult> {
    return request(`/scheduling/resolve-anchor`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async reserveAnchor(
    project_id: string,
    payload: {
      account_id: string;
      tiktok_slot: string;
      overrides?: Partial<Record<import("@/types").Platform, string>>;
    },
  ): Promise<{ platform_schedules: Record<string, { slot: string; scheduled_at: string }> }> {
    return request(`/scheduling/projects/${project_id}/reserve-anchor`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async reschedulePlatform(
    project_id: string,
    platform: import("@/types").Platform,
    new_slot: string,
  ): Promise<{ slot: string; scheduled_at: string; notification_status: string }> {
    return request(`/scheduling/projects/${project_id}/platforms/${platform}`, {
      method: "PATCH",
      body: JSON.stringify({ new_slot }),
    });
  },

  async rescheduleAnchor(
    project_id: string,
    payload: {
      tiktok_slot: string;
      overrides?: Partial<Record<import("@/types").Platform, string>>;
    },
  ): Promise<{ platform_schedules: Record<string, { slot: string; scheduled_at: string }>; notification_status: Record<string, string> }> {
    return request(`/scheduling/projects/${project_id}/anchor`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async cancelPlatformSlot(
    project_id: string, platform: import("@/types").Platform,
  ): Promise<void> {
    await fetch(`${API_BASE}/scheduling/projects/${project_id}/platforms/${platform}`, {
      method: "DELETE",
    });
  },

  async cancelAllSlots(project_id: string): Promise<void> {
    await fetch(`${API_BASE}/scheduling/projects/${project_id}/all`, { method: "DELETE" });
  },

  async cascadePreview(
    project_id: string, account_id: string,
  ): Promise<import("@/types").CascadePreview> {
    return request(`/scheduling/projects/${project_id}/cascade-preview`, {
      method: "POST",
      body: JSON.stringify({ account_id }),
    });
  },

  async cascadeApply(
    project_id: string, account_id: string,
  ): Promise<import("@/types").CascadePreview & { notification_status: Record<string, Record<string, string>> }> {
    return request(`/scheduling/projects/${project_id}/cascade-apply`, {
      method: "POST",
      body: JSON.stringify({ account_id }),
    });
  },
```

- [ ] **Step 2: Verify type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat(api): add scheduling client methods"
```

---

## Task 4: Add platform color tokens

**Files:**
- Create: `frontend/src/components/planning/platformColors.ts`
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Add CSS variables**

Append to `frontend/src/index.css` inside the `:root` block (or create `--platform-*` lines outside if no `:root`):

```css
@layer base {
  :root {
    --platform-youtube: 268 76% 58%;
    --platform-facebook: 220 76% 50%;
    --platform-instagram: 35 91% 55%;
    --platform-tiktok: 330 81% 60%;
  }
}
```

- [ ] **Step 2: Create the token helpers**

`frontend/src/components/planning/platformColors.ts`:

```ts
import type { Platform } from "@/types";

const HSL_TUPLES: Record<Platform, string> = {
  youtube: "var(--platform-youtube)",
  facebook: "var(--platform-facebook)",
  instagram: "var(--platform-instagram)",
  tiktok: "var(--platform-tiktok)",
};

export function platformBgHsl(platform: Platform): string {
  return `hsl(${HSL_TUPLES[platform]})`;
}

export function platformTranslucentHsl(platform: Platform, alpha = 0.18): string {
  return `hsl(${HSL_TUPLES[platform]} / ${alpha})`;
}

export const PLATFORM_LABELS: Record<Platform, string> = {
  youtube: "YouTube",
  facebook: "Facebook",
  instagram: "Instagram",
  tiktok: "TikTok",
};

export const PLATFORM_SHORT: Record<Platform, string> = {
  youtube: "YT",
  facebook: "FB",
  instagram: "IG",
  tiktok: "TT",
};
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/index.css frontend/src/components/planning/platformColors.ts
git commit -m "feat(planning): add platform color tokens"
```

---

## Task 5: Build `PlatformCheckboxes`

**Files:**
- Create: `frontend/src/components/planning/PlatformCheckboxes.tsx`

- [ ] **Step 1: Implement**

```tsx
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
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/planning/PlatformCheckboxes.tsx
git commit -m "feat(planning): add PlatformCheckboxes filter"
```

---

## Task 6: Build `PlanningCalendar` (ScheduleX wrapper)

**Files:**
- Create: `frontend/src/components/planning/PlanningCalendar.tsx`

- [ ] **Step 1: Implement**

```tsx
import { useEffect, useMemo, useRef } from "react";
import { ScheduleXCalendar, useNextCalendarApp } from "@schedule-x/react";
import { createViewWeek } from "@schedule-x/calendar";
import "@schedule-x/theme-default/dist/index.css";
import type { Platform, PlanningEvent } from "@/types";
import { platformBgHsl, PLATFORM_SHORT } from "./platformColors";

interface PlanningCalendarProps {
  events: PlanningEvent[];
  onEventClick: (event: PlanningEvent, anchor: { x: number; y: number }) => void;
}

function toScheduleXEvent(event: PlanningEvent, idx: number) {
  // ScheduleX expects "YYYY-MM-DD HH:mm" local strings.
  const dt = new Date(event.slot);
  const start = dt.toISOString().slice(0, 16).replace("T", " ");
  // Width: 30 minutes per event so adjacent slots don't visually overlap.
  const endDt = new Date(dt.getTime() + 30 * 60 * 1000);
  const end = endDt.toISOString().slice(0, 16).replace("T", " ");
  return {
    id: `${event.project_id}::${event.platform}::${idx}`,
    title: event.anime_title,
    start,
    end,
    calendarId: event.platform,
    _payload: event,
  };
}

const CALENDARS_BY_PLATFORM: Record<Platform, { lightColors: object; darkColors: object }> = {
  youtube: {
    lightColors: { main: platformBgHsl("youtube"), container: platformBgHsl("youtube"), onContainer: "#fff" },
    darkColors: { main: platformBgHsl("youtube"), container: platformBgHsl("youtube"), onContainer: "#fff" },
  },
  facebook: {
    lightColors: { main: platformBgHsl("facebook"), container: platformBgHsl("facebook"), onContainer: "#fff" },
    darkColors: { main: platformBgHsl("facebook"), container: platformBgHsl("facebook"), onContainer: "#fff" },
  },
  instagram: {
    lightColors: { main: platformBgHsl("instagram"), container: platformBgHsl("instagram"), onContainer: "#fff" },
    darkColors: { main: platformBgHsl("instagram"), container: platformBgHsl("instagram"), onContainer: "#fff" },
  },
  tiktok: {
    lightColors: { main: platformBgHsl("tiktok"), container: platformBgHsl("tiktok"), onContainer: "#fff" },
    darkColors: { main: platformBgHsl("tiktok"), container: platformBgHsl("tiktok"), onContainer: "#fff" },
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
    timezone: "Europe/Paris",
    isDark: true,
    events: sxEvents,
    calendars: CALENDARS_BY_PLATFORM,
    callbacks: {
      onEventClick(event: { id: string; _payload?: PlanningEvent }, e: MouseEvent) {
        const payload = event._payload ?? eventsRef.current.find(
          (ev) => `${ev.project_id}::${ev.platform}` === event.id.split("::").slice(0, 2).join("::"),
        );
        if (payload) {
          onEventClick(payload, { x: e?.clientX ?? 0, y: e?.clientY ?? 0 });
        }
      },
    },
  });

  // Keep events in sync as parent reloads.
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
```

- [ ] **Step 2: Add ScheduleX theme overrides for dark mode in `index.css`**

Append:

```css
.planning-calendar {
  /* Match the app dark theme */
  --sx-color-background: hsl(var(--card));
  --sx-color-on-background: hsl(var(--foreground));
  --sx-color-surface: hsl(var(--card));
  --sx-color-on-surface: hsl(var(--foreground));
  --sx-internal-color-text: hsl(var(--foreground));
  --sx-color-outline: hsl(var(--border));
}
```

- [ ] **Step 3: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/planning/PlanningCalendar.tsx frontend/src/index.css
git commit -m "feat(planning): add ScheduleX week calendar wrapper"
```

---

## Task 7: Build `EventPopover`

**Files:**
- Create: `frontend/src/components/planning/EventPopover.tsx`

- [ ] **Step 1: Implement**

```tsx
import { motion } from "framer-motion";
import { ExternalLink, X, Trash2, RotateCcw, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui";
import type { PlanningEvent } from "@/types";
import { PLATFORM_LABELS, platformBgHsl } from "./platformColors";

interface EventPopoverProps {
  event: PlanningEvent;
  anchor: { x: number; y: number };
  onClose: () => void;
  onRescheduleSlot: () => void;
  onRescheduleProject: () => void;
  rescheduleProjectDisabled?: boolean;
  rescheduleProjectDisabledReason?: string;
  onCancelSlot: () => void;
  onCancelAll: () => void;
}

function formatSlot(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit",
    timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function EventPopover({
  event, anchor, onClose,
  onRescheduleSlot, onRescheduleProject, rescheduleProjectDisabled,
  rescheduleProjectDisabledReason,
  onCancelSlot, onCancelAll,
}: EventPopoverProps) {
  const left = Math.min(window.innerWidth - 320, Math.max(8, anchor.x + 8));
  const top = Math.min(window.innerHeight - 280, Math.max(8, anchor.y + 8));

  return (
    <div
      className="fixed inset-0 z-[55]"
      onClick={onClose}
      role="dialog"
      aria-label="Event details"
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96 }}
        transition={{ duration: 0.12 }}
        className="absolute w-80 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-xl p-4"
        style={{ left, top }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-2">
          <div className="min-w-0">
            <div className="font-semibold truncate">{event.anime_title}</div>
            <div className="text-[11px] font-mono text-[hsl(var(--muted-foreground))] truncate">
              {event.project_id}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[hsl(var(--muted))]"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex items-center gap-2 mb-3">
          <img
            src={event.account_avatar_url}
            alt=""
            className="h-6 w-6 rounded-full bg-[hsl(var(--muted))]"
          />
          <span className="text-sm">{event.account_name}</span>
          <span
            className="ml-auto text-[10px] font-bold px-2 py-0.5 rounded text-white"
            style={{ backgroundColor: platformBgHsl(event.platform) }}
          >
            {PLATFORM_LABELS[event.platform]}
          </span>
        </div>

        <div className="text-sm text-[hsl(var(--muted-foreground))] mb-3">
          {formatSlot(event.slot)}
        </div>

        {event.drive_folder_url && (
          <a
            href={event.drive_folder_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-[hsl(var(--primary))] hover:underline mb-3"
          >
            Drive folder <ExternalLink className="h-3 w-3" />
          </a>
        )}

        <div className="grid grid-cols-2 gap-2">
          <Button size="sm" variant="outline" onClick={onRescheduleSlot}>
            <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Reschedule slot
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={onRescheduleProject}
            disabled={rescheduleProjectDisabled}
            title={rescheduleProjectDisabledReason}
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1.5" /> Reschedule project
          </Button>
          <Button size="sm" variant="ghost" onClick={onCancelSlot} className="text-[hsl(var(--destructive))]">
            <Trash2 className="h-3.5 w-3.5 mr-1.5" /> Cancel slot
          </Button>
          <Button size="sm" variant="ghost" onClick={onCancelAll} className="text-[hsl(var(--destructive))]">
            <Trash2 className="h-3.5 w-3.5 mr-1.5" /> Cancel all
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/planning/EventPopover.tsx
git commit -m "feat(planning): add EventPopover with reschedule/cancel actions"
```

---

## Task 8: Build `PlanningHeader`

**Files:**
- Create: `frontend/src/components/planning/PlanningHeader.tsx`

- [ ] **Step 1: Implement**

```tsx
import { useState } from "react";
import { Loader2, RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui";
import { AccountSelectorDropdown } from "@/components/project-manager/AccountSelectorDropdown";
import type { Account, Platform } from "@/types";
import { PlatformCheckboxes } from "./PlatformCheckboxes";

interface PlanningHeaderProps {
  accounts: Account[];
  selectedAccount: Account | null;
  onSelectAccount: (id: string | null) => void;
  selectedPlatforms: Platform[];
  onChangePlatforms: (next: Platform[]) => void;
  loading: boolean;
  onRefresh: () => void;
  onClose: () => void;
}

export function PlanningHeader({
  accounts, selectedAccount, onSelectAccount,
  selectedPlatforms, onChangePlatforms,
  loading, onRefresh, onClose,
}: PlanningHeaderProps) {
  const [accountDropdownOpen, setAccountDropdownOpen] = useState(false);

  return (
    <header className="px-6 py-4 border-b border-[hsl(var(--border))] flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-4 min-w-0">
        <AccountSelectorDropdown
          accounts={accounts}
          selectedAccount={selectedAccount}
          isOpen={accountDropdownOpen}
          onToggle={() => setAccountDropdownOpen((p) => !p)}
          onSelect={(id) => {
            onSelectAccount(id);
            setAccountDropdownOpen(false);
          }}
        />
        <div className="min-w-0">
          <h2 className="text-xl font-semibold">Planning</h2>
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            Forward upload schedule (Europe/Paris)
          </p>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <PlatformCheckboxes
          selected={selectedPlatforms}
          onChange={onChangePlatforms}
        />
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
        <Button variant="ghost" size="sm" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>
    </header>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/planning/PlanningHeader.tsx
git commit -m "feat(planning): add PlanningHeader with account + platform filters"
```

---

## Task 9: Build `PlanningModal`

**Files:**
- Create: `frontend/src/components/planning/PlanningModal.tsx`
- Create: `frontend/src/components/planning/index.ts`

- [ ] **Step 1: Implement the modal**

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { api } from "@/api/client";
import { ALL_PLATFORMS, type Account, type Platform, type PlanningEvent } from "@/types";
import { PlanningHeader } from "./PlanningHeader";
import { PlanningCalendar } from "./PlanningCalendar";
import { EventPopover } from "./EventPopover";

const LS_ACCOUNT = "atr.planning.account_id";
const LS_PLATFORMS = "atr.planning.platforms";

function readPersistedPlatforms(): Platform[] {
  try {
    const raw = localStorage.getItem(LS_PLATFORMS);
    if (!raw) return [...ALL_PLATFORMS];
    const arr = JSON.parse(raw) as Platform[];
    return arr.length ? arr : [...ALL_PLATFORMS];
  } catch {
    return [...ALL_PLATFORMS];
  }
}

function readPersistedAccount(): string | null {
  try {
    const raw = localStorage.getItem(LS_ACCOUNT);
    return raw && raw !== "null" ? raw : null;
  } catch {
    return null;
  }
}

interface PlanningModalProps {
  open: boolean;
  onClose: () => void;
}

export function PlanningModal({ open, onClose }: PlanningModalProps) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [events, setEvents] = useState<PlanningEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(readPersistedAccount());
  const [selectedPlatforms, setSelectedPlatforms] = useState<Platform[]>(readPersistedPlatforms());
  const [popover, setPopover] = useState<{ event: PlanningEvent; anchor: { x: number; y: number } } | null>(null);

  const selectedAccount = useMemo(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [accountsRes, eventsRes] = await Promise.all([
        api.listAccounts(),
        api.listPlanningEvents({
          account_id: selectedAccountId,
          platforms: selectedPlatforms,
        }),
      ]);
      setAccounts(accountsRes.accounts);
      setEvents(eventsRes.events);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [selectedAccountId, selectedPlatforms]);

  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  useEffect(() => {
    localStorage.setItem(LS_ACCOUNT, selectedAccountId ?? "null");
  }, [selectedAccountId]);

  useEffect(() => {
    localStorage.setItem(LS_PLATFORMS, JSON.stringify(selectedPlatforms));
  }, [selectedPlatforms]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (popover) setPopover(null);
        else onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, popover, onClose]);

  if (!open) return null;

  const eventForPopover = popover?.event;
  const projectHasTikTok = !!events.find(
    (e) => e.project_id === eventForPopover?.project_id && e.platform === "tiktok"
  );

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={() => {
            if (popover) return;
            onClose();
          }}
        >
          <motion.div
            className="w-full max-w-7xl h-[88vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl flex flex-col overflow-hidden"
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => e.stopPropagation()}
          >
            <PlanningHeader
              accounts={accounts}
              selectedAccount={selectedAccount}
              onSelectAccount={setSelectedAccountId}
              selectedPlatforms={selectedPlatforms}
              onChangePlatforms={setSelectedPlatforms}
              loading={loading}
              onRefresh={() => void reload()}
              onClose={onClose}
            />
            {error && (
              <div className="mx-6 mt-4 p-3 rounded-md bg-[hsl(var(--destructive))]/10 text-sm text-[hsl(var(--destructive))]">
                {error}
              </div>
            )}
            <div className="flex-1 overflow-hidden p-4">
              <PlanningCalendar
                events={events}
                onEventClick={(event, anchor) => setPopover({ event, anchor })}
              />
            </div>
          </motion.div>

          {popover && eventForPopover && (
            <EventPopover
              event={eventForPopover}
              anchor={popover.anchor}
              onClose={() => setPopover(null)}
              onRescheduleSlot={async () => {
                const newSlotIso = window.prompt(
                  `New slot for ${eventForPopover.platform} (ISO 8601, e.g. 2026-05-08T14:00:00Z):`,
                );
                if (!newSlotIso) return;
                try {
                  await api.reschedulePlatform(
                    eventForPopover.project_id,
                    eventForPopover.platform,
                    newSlotIso,
                  );
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
              onRescheduleProject={() => {
                // Phase 3 wires the TT-anchored picker; show a placeholder for now.
                window.alert("Reschedule whole project: available in Phase 3.");
              }}
              rescheduleProjectDisabled={!projectHasTikTok}
              rescheduleProjectDisabledReason={
                projectHasTikTok ? undefined : "This project has no TikTok reservation"
              }
              onCancelSlot={async () => {
                if (!confirm(`Cancel ${eventForPopover.platform} slot for ${eventForPopover.anime_title}?`)) return;
                try {
                  await api.cancelPlatformSlot(eventForPopover.project_id, eventForPopover.platform);
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
              onCancelAll={async () => {
                if (!confirm(`Cancel ALL slots for ${eventForPopover.anime_title}?`)) return;
                try {
                  await api.cancelAllSlots(eventForPopover.project_id);
                  setPopover(null);
                  await reload();
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
            />
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
```

- [ ] **Step 2: Add the barrel export**

`frontend/src/components/planning/index.ts`:

```ts
export { PlanningModal } from "./PlanningModal";
```

- [ ] **Step 3: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/planning/
git commit -m "feat(planning): wire PlanningModal with calendar + popover actions"
```

---

## Task 10: Add Planning button to LibraryHeader and mount the modal

**Files:**
- Modify: `frontend/src/components/library/LibraryHeader.tsx`
- Modify: `frontend/src/App.tsx` (or wherever ProjectManagerModal is mounted)

- [ ] **Step 1: Extend `LibraryHeader` to expose `onOpenPlanning`**

Edit [frontend/src/components/library/LibraryHeader.tsx](frontend/src/components/library/LibraryHeader.tsx). Update the props and JSX:

```tsx
import { CalendarDays, FolderKanban, Eraser } from "lucide-react";
import type { LibraryType } from "@/types";
import { LIBRARY_TYPE_OPTIONS } from "@/utils/libraryTypes";

interface LibraryHeaderProps {
  selectedType: LibraryType;
  onTypeChange: (type: LibraryType) => void;
  onOpenProjectManager: () => void;
  onOpenPlanning: () => void;
  onOpenPurge: () => void;
}

export function LibraryHeader({
  selectedType,
  onTypeChange,
  onOpenProjectManager,
  onOpenPlanning,
  onOpenPurge,
}: LibraryHeaderProps) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-[hsl(var(--card))] px-4 py-3">
      <span className="font-bold text-lg text-[hsl(var(--primary))]">
        Anime TikTok Reproducer
      </span>
      <div className="w-px h-6 bg-[hsl(var(--border))]" />
      <select
        value={selectedType}
        onChange={(e) => onTypeChange(e.target.value as LibraryType)}
        className="bg-[hsl(var(--secondary))] text-[hsl(var(--primary))] rounded px-2 py-1 text-sm border-none outline-none cursor-pointer"
      >
        {LIBRARY_TYPE_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
      <div className="flex-1" />
      <button
        onClick={onOpenProjectManager}
        className="flex items-center gap-1.5 bg-[hsl(var(--secondary))] rounded px-3 py-1.5 text-sm hover:bg-[hsl(var(--secondary))]/80 transition-colors"
      >
        <FolderKanban className="h-4 w-4" />
        <span>Projects</span>
      </button>
      <button
        onClick={onOpenPlanning}
        className="flex items-center gap-1.5 bg-[hsl(var(--secondary))] rounded px-3 py-1.5 text-sm hover:bg-[hsl(var(--secondary))]/80 transition-colors"
      >
        <CalendarDays className="h-4 w-4" />
        <span>Planning</span>
      </button>
      <button
        onClick={onOpenPurge}
        className="p-1.5 rounded text-[hsl(var(--destructive))] hover:bg-[hsl(var(--destructive))]/15 transition-colors"
        title="Purger la librairie"
      >
        <Eraser className="h-4 w-4" />
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Mount `PlanningModal` next to `ProjectManagerModal`**

Find where `ProjectManagerModal` is mounted (usually in `App.tsx`). Pattern:

```tsx
const [projectManagerOpen, setProjectManagerOpen] = useState(false);
const [planningOpen, setPlanningOpen] = useState(false);

// ... in JSX:
<LibraryHeader
  selectedType={selectedType}
  onTypeChange={setSelectedType}
  onOpenProjectManager={() => setProjectManagerOpen(true)}
  onOpenPlanning={() => setPlanningOpen(true)}
  onOpenPurge={...}
/>

<ProjectManagerModal open={projectManagerOpen} onClose={() => setProjectManagerOpen(false)} />
<PlanningModal open={planningOpen} onClose={() => setPlanningOpen(false)} />
```

(Use `import { PlanningModal } from "@/components/planning";`)

- [ ] **Step 3: Type-check + dev smoke**

```bash
cd frontend && npx tsc --noEmit
cd frontend && npm run dev
```

Open `http://localhost:5173`, click "Planning", confirm the modal opens with the week calendar empty (no events yet).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/library/LibraryHeader.tsx frontend/src/App.tsx
git commit -m "feat(library): add Planning button and mount PlanningModal"
```

---

## Task 11: Playwright e2e — modal opens, events render with platform colors

**Files:**
- Create: `frontend/e2e/planning.spec.ts`

- [ ] **Step 1: Write the test**

```ts
import { expect, test } from "@playwright/test";

const ACCOUNTS = [
  { id: "acc_a", name: "Account A", language: "fr",
    avatar_url: "/api/accounts/acc_a/avatar",
    supported_types: ["anime"], slots: ["14:00", "18:00"],
    slots_by_platform: { youtube: ["14:00"], facebook: ["14:00"],
                         instagram: ["14:00"], tiktok: ["14:00", "18:00"] } },
];

const EVENTS = [
  {
    project_id: "p1", anime_title: "Show Alpha",
    account_id: "acc_a", account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "youtube",
    slot: "2026-05-07T14:00:00Z", scheduled_at: "2026-05-07T14:08:00Z",
    drive_folder_url: "https://drive.example/p1",
    status: "scheduled",
  },
  {
    project_id: "p2", anime_title: "Show Beta",
    account_id: "acc_a", account_avatar_url: "/api/accounts/acc_a/avatar",
    account_name: "Account A",
    platform: "tiktok",
    slot: "2026-05-08T18:00:00Z", scheduled_at: "2026-05-08T18:14:00Z",
    drive_folder_url: null,
    status: "scheduled",
  },
];

function installMocks(events: unknown[]) {
  return async () => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.origin,
      );
      const path = url.pathname;
      if (path === "/api/accounts") {
        return new Response(JSON.stringify({ accounts: ACCOUNTS }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      if (path === "/api/scheduling/events") {
        return new Response(JSON.stringify({ events }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      if (path === "/api/sources/anime/details") {
        return new Response(JSON.stringify({ sources: [] }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      if (path === "/api/sources" || path === "/api/sources/anime") {
        return new Response(JSON.stringify({ sources: [] }), {
          status: 200, headers: { "Content-Type": "application/json" },
        });
      }
      return orig(input, init);
    };
  };
}

test("Planning modal opens and shows mocked events", async ({ page }) => {
  await page.addInitScript(installMocks(EVENTS));
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();
  // ScheduleX renders event titles in the time-grid cells.
  await expect(page.getByText("Show Alpha")).toBeVisible();
  await expect(page.getByText("Show Beta")).toBeVisible();
});

test("Planning modal filters by platform", async ({ page }) => {
  await page.addInitScript(installMocks(EVENTS));
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  // Toggle YT off via aria-pressed click.
  await page.getByRole("button", { name: "YouTube" }).click();
  // After toggling, the modal re-fetches with the new platforms set.
  // We don't assert content here since the mock returns the same payload regardless;
  // assert that a request was made with platforms not containing "youtube".
  await page.waitForRequest(
    (req) => req.url().includes("/api/scheduling/events") && !req.url().includes("youtube"),
  );
});
```

- [ ] **Step 2: Run**

```bash
cd frontend && npx playwright test e2e/planning.spec.ts -g "Planning modal" --reporter=line
```

Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/planning.spec.ts
git commit -m "test(planning): playwright coverage for modal open + platform filter"
```

---

## Task 12: Playwright e2e — reschedule single slot + cancel

**Files:**
- Modify: `frontend/e2e/planning.spec.ts`

- [ ] **Step 1: Append tests**

```ts
test("Reschedule slot triggers PATCH and reloads", async ({ page }) => {
  let patched = false;
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.origin,
      );
      if (url.pathname.endsWith("/platforms/youtube") && init?.method === "PATCH") {
        // @ts-expect-error inject flag
        window.__patched = true;
        return new Response(JSON.stringify({
          slot: "2026-05-08T14:00:00Z",
          scheduled_at: "2026-05-08T14:11:00Z",
          notification_status: "ok",
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return orig(input, init);
    };
  });
  await page.addInitScript(installMocks(EVENTS));
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();

  page.on("dialog", async (dlg) => {
    if (dlg.type() === "prompt") await dlg.accept("2026-05-08T14:00:00Z");
    else await dlg.dismiss();
  });
  await page.getByText("Show Alpha").click();
  await page.getByRole("button", { name: "Reschedule slot" }).click();
  await page.waitForFunction(() => (window as any).__patched === true);
  patched = true;
  expect(patched).toBe(true);
});

test("Cancel slot triggers DELETE", async ({ page }) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.origin,
      );
      if (url.pathname.endsWith("/platforms/tiktok") && init?.method === "DELETE") {
        // @ts-expect-error inject flag
        window.__deleted = true;
        return new Response(null, { status: 204 });
      }
      return orig(input, init);
    };
  });
  await page.addInitScript(installMocks(EVENTS));
  await page.goto("/");
  await page.getByRole("button", { name: "Planning" }).click();
  page.on("dialog", (d) => d.accept());
  await page.getByText("Show Beta").click();
  await page.getByRole("button", { name: "Cancel slot" }).click();
  await page.waitForFunction(() => (window as any).__deleted === true);
});
```

- [ ] **Step 2: Run**

```bash
cd frontend && npx playwright test e2e/planning.spec.ts --reporter=line
```

Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/planning.spec.ts
git commit -m "test(planning): playwright coverage for reschedule + cancel"
```

---

## Task 13: Final regression + visual smoke

- [ ] **Step 1: Run all e2e tests**

```bash
cd frontend && npm run test
```

Expected: all green.

- [ ] **Step 2: Run dev server and inspect**

```bash
cd frontend && npm run dev
```

Manual checks:
- Click "Planning" — modal opens with the week view.
- Each platform is filterable via the chip buttons (verify aria-pressed flips).
- "All / None" toggle.
- Account selector shows "All Projects" by default.
- ScheduleX week grid spans Mon–Sun, FR locale, 24h format.
- Click an event → popover with the four action buttons.

- [ ] **Step 3: Commit any cleanup**

```bash
git status
```

---

**Phase 2 complete.** The Planning modal is fully usable for viewing and managing existing reservations. The "Reschedule whole project" button surfaces a placeholder until Phase 3 wires the TT-anchored picker.
