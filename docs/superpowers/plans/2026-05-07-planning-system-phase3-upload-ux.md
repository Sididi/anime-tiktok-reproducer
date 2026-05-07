# Planning System — Phase 3: Upload UX (Split Button + Picker + Urgent)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the existing single Upload button in the Project Manager with a split button offering three modes: auto (default, identical to today), schedule for a specific TT slot, and urgent with cascade. Build the slot picker popover (mini-cal + slot chips + override) and the urgent cascade preview modal. Wire both into the existing copyright/duration/enqueue flow.

**Architecture:** All new components live under `frontend/src/components/project-manager/`. The existing `startUploadWithChecks` in `ProjectManagerModal.tsx` is extended with a `mode: "auto" | "scheduled" | "urgent"` parameter that calls `reserveAnchor` or `cascadeApply` BEFORE the legacy copyright check. The Planning modal's `Reschedule whole project` button reuses `SlotPickerPopover`.

**Tech Stack:** React 19, TypeScript, Tailwind, framer-motion. No new libs.

**Spec:** [docs/superpowers/specs/2026-05-07-planning-system-design.md](../specs/2026-05-07-planning-system-design.md)

**Prerequisites:** Phase 1 (backend) and Phase 2 (Planning frontend) merged.

---

## File Structure

**New files:**
- `frontend/src/components/project-manager/UploadSplitButton.tsx`
- `frontend/src/components/project-manager/SlotPickerPopover.tsx`
- `frontend/src/components/project-manager/SlotPickerCalendar.tsx`
- `frontend/src/components/project-manager/SlotChips.tsx`
- `frontend/src/components/project-manager/PerPlatformOverride.tsx`
- `frontend/src/components/project-manager/UrgentCascadeModal.tsx`
- `frontend/e2e/upload-split-button.spec.ts`

**Modified files:**
- `frontend/src/components/project-manager/ProjectRow.tsx` — replace inline Upload button with `<UploadSplitButton>`
- `frontend/src/components/project-manager/ProjectManagerModal.tsx` — extend `startUploadWithChecks` + propagate `mode`
- `frontend/src/components/project-manager/types.ts` — add `UploadMode`
- `frontend/src/components/planning/PlanningModal.tsx` — wire `Reschedule whole project` to `SlotPickerPopover`

---

## Conventions

- All new pickers default to **Europe/Paris** for any user-facing time formatting.
- Slot ISOs sent to the backend are UTC.
- Component tests run via Playwright, mocking fetch like Phase 2.

---

## Task 1: Add `UploadMode` type and extend `startUploadWithChecks`

**Files:**
- Modify: `frontend/src/components/project-manager/types.ts`
- Modify: `frontend/src/components/project-manager/ProjectManagerModal.tsx`

- [ ] **Step 1: Add the type**

Append to [frontend/src/components/project-manager/types.ts](frontend/src/components/project-manager/types.ts):

```ts
export type UploadMode = "auto" | "scheduled" | "urgent";

export interface AnchorPayload {
  tiktok_slot: string;
  overrides?: Partial<Record<import("@/types").Platform, string>>;
}
```

- [ ] **Step 2: Extend `startUploadWithChecks`**

Edit [ProjectManagerModal.tsx:541](frontend/src/components/project-manager/ProjectManagerModal.tsx#L541). Change the signature and add the pre-check phase:

```tsx
const startUploadWithChecks = useCallback(
  async (
    projectId: string,
    accountId?: string,
    mode: UploadMode = "auto",
    anchorPayload?: AnchorPayload,
  ) => {
    if (mode !== "auto" && !accountId) {
      setError("Manual scheduling requires an account selection");
      return;
    }

    if (mode === "scheduled" && anchorPayload) {
      try {
        await api.reserveAnchor(projectId, {
          account_id: accountId!,
          tiktok_slot: anchorPayload.tiktok_slot,
          overrides: anchorPayload.overrides,
        });
      } catch (err) {
        setError((err as Error).message);
        return;
      }
    }

    if (mode === "urgent") {
      try {
        await api.cascadeApply(projectId, accountId!);
      } catch (err) {
        setError((err as Error).message);
        return;
      }
    }

    const token = createUploadToken();
    const context: PendingUploadContext = { projectId, accountId };
    setUploadSession({
      token, context,
      status: "checking_copyright",
      message: "Vérification des droits musicaux...",
      startedAt: Date.now(), updatedAt: Date.now(),
    });
    setError(null);
    try {
      const result = await api.checkCopyright(projectId, accountId);
      if (!isSessionCurrent(projectId, token)) return;
      if (result.copyrighted) {
        patchUploadSession(projectId, token, {
          context,
          status: result.no_music_available
            ? "awaiting_copyright_music"
            : "awaiting_copyright_warning",
          message: null,
          copyrightResult: result,
        });
        return;
      }
    } catch (err) {
      console.warn("Copyright check failed, proceeding:", err);
      if (!isSessionCurrent(projectId, token)) return;
    }
    await continueUploadAfterCopyright(context, token);
  },
  [continueUploadAfterCopyright, isSessionCurrent, patchUploadSession, setUploadSession],
);
```

Update the `imports` at the top of the file:

```tsx
import type { UploadMode, AnchorPayload } from "./types";
```

- [ ] **Step 3: Type-check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/project-manager/types.ts frontend/src/components/project-manager/ProjectManagerModal.tsx
git commit -m "feat(upload): extend startUploadWithChecks with mode + anchorPayload"
```

---

## Task 2: Build `SlotChips`

**Files:**
- Create: `frontend/src/components/project-manager/SlotChips.tsx`

- [ ] **Step 1: Implement**

```tsx
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
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/project-manager/SlotChips.tsx
git commit -m "feat(upload): add SlotChips component"
```

---

## Task 3: Build `SlotPickerCalendar`

**Files:**
- Create: `frontend/src/components/project-manager/SlotPickerCalendar.tsx`

- [ ] **Step 1: Implement**

```tsx
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
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/project-manager/SlotPickerCalendar.tsx
git commit -m "feat(upload): add SlotPickerCalendar mini month grid"
```

---

## Task 4: Build `PerPlatformOverride`

**Files:**
- Create: `frontend/src/components/project-manager/PerPlatformOverride.tsx`

- [ ] **Step 1: Implement**

```tsx
import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { api } from "@/api/client";
import type { Platform, FreeSlot } from "@/types";
import { PLATFORM_LABELS } from "@/components/planning/platformColors";

interface PerPlatformOverrideProps {
  accountId: string;
  anchorIso: string;
  resolved: Partial<Record<Platform, { slot: string; available: boolean }>>;
  overrides: Partial<Record<Platform, string>>;
  onChangeOverride: (platform: Platform, slotIso: string | null) => void;
  platforms: Platform[]; // platforms the project will reserve
}

const NON_TT_PLATFORMS: Platform[] = ["youtube", "facebook", "instagram"];

export function PerPlatformOverride({
  accountId, anchorIso, resolved, overrides, onChangeOverride, platforms,
}: PerPlatformOverrideProps) {
  const [open, setOpen] = useState(false);
  const [slotsByPlatform, setSlotsByPlatform] = useState<Record<Platform, FreeSlot[]>>(
    {} as Record<Platform, FreeSlot[]>,
  );

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      const next: Record<Platform, FreeSlot[]> = {} as Record<Platform, FreeSlot[]>;
      for (const platform of NON_TT_PLATFORMS) {
        if (!platforms.includes(platform)) continue;
        const r = await api.listFreeSlots({
          account_id: accountId,
          platform,
          after: anchorIso,
          limit: 20,
        });
        next[platform] = r.slots;
      }
      if (!cancelled) setSlotsByPlatform(next);
    })();
    return () => { cancelled = true; };
  }, [open, accountId, anchorIso, platforms]);

  return (
    <div className="border-t border-[hsl(var(--border))] pt-2">
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        className="text-xs flex items-center gap-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        Override per-platform
      </button>
      {open && (
        <div className="mt-2 space-y-1.5">
          {NON_TT_PLATFORMS.filter((p) => platforms.includes(p)).map((p) => {
            const slots = slotsByPlatform[p] ?? [];
            const current = overrides[p] ?? resolved[p]?.slot ?? "";
            return (
              <div key={p} className="flex items-center gap-2 text-xs">
                <span className="w-20">{PLATFORM_LABELS[p]}</span>
                <select
                  className="flex-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-2 py-1 text-xs"
                  value={current}
                  onChange={(e) => onChangeOverride(p, e.target.value || null)}
                >
                  {slots.filter((s) => s.available || s.slot === current).map((s) => (
                    <option key={s.slot} value={s.slot}>
                      {new Intl.DateTimeFormat("fr-FR", {
                        weekday: "short", day: "2-digit", month: "short",
                        hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
                      }).format(new Date(s.slot))}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/project-manager/PerPlatformOverride.tsx
git commit -m "feat(upload): add PerPlatformOverride collapsible"
```

---

## Task 5: Build `SlotPickerPopover`

**Files:**
- Create: `frontend/src/components/project-manager/SlotPickerPopover.tsx`

- [ ] **Step 1: Implement**

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type { FreeSlot, Platform, ResolveAnchorResult } from "@/types";
import { PLATFORM_SHORT } from "@/components/planning/platformColors";
import { SlotPickerCalendar } from "./SlotPickerCalendar";
import { SlotChips } from "./SlotChips";
import { PerPlatformOverride } from "./PerPlatformOverride";

export type SlotPickerMode = "anchor" | "single-platform";

interface SlotPickerPopoverProps {
  open: boolean;
  onClose: () => void;
  mode: SlotPickerMode;
  projectId: string;
  accountId: string;
  platform?: Platform;            // required when mode = single-platform
  initialIso?: string;             // pre-fill (current slot when rescheduling)
  platformsForAnchor: Platform[]; // anchor mode: which platforms are in scope
  onConfirm: (
    payload: { tiktok_slot: string; overrides?: Partial<Record<Platform, string>> } | { slot: string },
  ) => Promise<void>;
}

function ymd(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function SlotPickerPopover(props: SlotPickerPopoverProps) {
  const {
    open, onClose, mode, projectId, accountId, platform,
    initialIso, platformsForAnchor, onConfirm,
  } = props;

  const initial = initialIso ? new Date(initialIso) : new Date();
  const [monthAnchor, setMonthAnchor] = useState(initial);
  const [selectedDate, setSelectedDate] = useState<Date | null>(initial);
  const [selectedSlotIso, setSelectedSlotIso] = useState<string | null>(initialIso ?? null);
  const [slotsForDay, setSlotsForDay] = useState<FreeSlot[]>([]);
  const [resolveResult, setResolveResult] = useState<ResolveAnchorResult | null>(null);
  const [overrides, setOverrides] = useState<Partial<Record<Platform, string>>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const platformForFetch: Platform = mode === "anchor" ? "tiktok" : platform!;

  // Fetch slots for selected day.
  useEffect(() => {
    if (!open || !selectedDate) return;
    let cancelled = false;
    const dayStart = new Date(selectedDate);
    dayStart.setHours(0, 0, 0, 0);
    const dayEnd = new Date(selectedDate);
    dayEnd.setHours(23, 59, 59, 0);
    (async () => {
      try {
        const r = await api.listFreeSlots({
          account_id: accountId,
          platform: platformForFetch,
          after: dayStart.toISOString(),
          limit: 50,
        });
        if (cancelled) return;
        const sameDay = r.slots.filter((s) => new Date(s.slot) <= dayEnd);
        setSlotsForDay(sameDay);
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    })();
    return () => { cancelled = true; };
  }, [open, selectedDate, accountId, platformForFetch]);

  // Live resolve preview in anchor mode.
  useEffect(() => {
    if (!open || mode !== "anchor" || !selectedSlotIso) {
      setResolveResult(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await api.resolveAnchor({
          project_id: projectId,
          account_id: accountId,
          tiktok_slot: selectedSlotIso,
          overrides,
        });
        if (!cancelled) setResolveResult(r);
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    })();
    return () => { cancelled = true; };
  }, [open, mode, projectId, accountId, selectedSlotIso, overrides]);

  const canSubmit = useMemo(() => {
    if (!selectedSlotIso) return false;
    if (mode === "anchor" && resolveResult?.conflicts.length) return false;
    return true;
  }, [selectedSlotIso, mode, resolveResult]);

  const handleSubmit = useCallback(async () => {
    if (!selectedSlotIso) return;
    setSubmitting(true); setError(null);
    try {
      if (mode === "anchor") {
        await onConfirm({ tiktok_slot: selectedSlotIso, overrides });
      } else {
        await onConfirm({ slot: selectedSlotIso });
      }
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [selectedSlotIso, mode, overrides, onConfirm, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[60] bg-black/40 flex items-center justify-center"
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96 }}
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-4 w-[320px] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold mb-2">
          {mode === "anchor" ? "Pick a slot" : `Pick ${PLATFORM_SHORT[platform!]} slot`}
        </h3>
        <SlotPickerCalendar
          monthAnchor={monthAnchor}
          onPrevMonth={() =>
            setMonthAnchor(new Date(monthAnchor.getFullYear(), monthAnchor.getMonth() - 1, 1))
          }
          onNextMonth={() =>
            setMonthAnchor(new Date(monthAnchor.getFullYear(), monthAnchor.getMonth() + 1, 1))
          }
          selectedDate={selectedDate}
          onSelectDate={(d) => {
            setSelectedDate(d);
            setSelectedSlotIso(null);
          }}
        />
        <div className="border-t border-[hsl(var(--border))] mt-3 pt-2">
          <div className="text-[11px] text-[hsl(var(--muted-foreground))] mb-1.5">
            {mode === "anchor" ? "TikTok slots" : `${PLATFORM_SHORT[platform!]} slots`}
            {selectedDate && ` · ${selectedDate.toLocaleDateString("fr-FR")}`}
          </div>
          <SlotChips
            slots={slotsForDay}
            selectedIso={selectedSlotIso}
            onSelect={setSelectedSlotIso}
          />
        </div>

        {mode === "anchor" && resolveResult && (
          <>
            <div className="border-t border-[hsl(var(--border))] mt-3 pt-2 text-[11px]">
              <div className="text-[hsl(var(--muted-foreground))] mb-1">Other platforms (auto)</div>
              <div className="font-mono text-xs">
                {Object.entries(resolveResult.resolved)
                  .filter(([p]) => p !== "tiktok")
                  .map(([p, info]) => (
                    <div key={p}>
                      {PLATFORM_SHORT[p as Platform]} {new Intl.DateTimeFormat("fr-FR", {
                        weekday: "short", day: "2-digit", month: "short",
                        hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
                      }).format(new Date(info!.slot))}
                    </div>
                  ))}
              </div>
              {resolveResult.conflicts.length > 0 && (
                <div className="mt-1 text-[hsl(var(--destructive))]">
                  Conflict: {resolveResult.conflicts.map((c) => `${c.platform}:${c.reason}`).join(", ")}
                </div>
              )}
            </div>
            <PerPlatformOverride
              accountId={accountId}
              anchorIso={selectedSlotIso ?? new Date().toISOString()}
              resolved={resolveResult.resolved}
              overrides={overrides}
              onChangeOverride={(p, iso) =>
                setOverrides((prev) => {
                  const next = { ...prev };
                  if (iso === null) delete next[p];
                  else next[p] = iso;
                  return next;
                })
              }
              platforms={platformsForAnchor}
            />
          </>
        )}

        {error && (
          <div className="mt-2 text-xs text-[hsl(var(--destructive))]">{error}</div>
        )}

        <div className="flex justify-end gap-2 mt-3">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={!canSubmit || submitting} onClick={handleSubmit}>
            {submitting ? "Saving…" : "Schedule"}
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
git add frontend/src/components/project-manager/SlotPickerPopover.tsx
git commit -m "feat(upload): add SlotPickerPopover (anchor + single-platform modes)"
```

---

## Task 6: Build `UrgentCascadeModal`

**Files:**
- Create: `frontend/src/components/project-manager/UrgentCascadeModal.tsx`

- [ ] **Step 1: Implement**

```tsx
import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type { CascadePreview, Platform } from "@/types";
import { PLATFORM_SHORT, platformBgHsl } from "@/components/planning/platformColors";

interface UrgentCascadeModalProps {
  open: boolean;
  projectId: string;
  projectTitle: string;
  accountId: string;
  onClose: () => void;
  onConfirmed: () => void;     // parent then continues the upload flow
}

function fmt(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

export function UrgentCascadeModal({
  open, projectId, projectTitle, accountId, onClose, onConfirmed,
}: UrgentCascadeModalProps) {
  const [preview, setPreview] = useState<CascadePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true); setError(null);
    api.cascadePreview(projectId, accountId)
      .then(setPreview)
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false));
  }, [open, projectId, accountId]);

  if (!open) return null;
  const blocked = (preview?.blockers.length ?? 0) > 0;
  const totalDisplaced = preview?.per_platform.reduce(
    (acc, p) => acc + p.displaced.length, 0,
  ) ?? 0;

  return (
    <div className="fixed inset-0 z-[60] bg-black/55 flex items-center justify-center" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-5 w-[480px] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-2">
          <AlertTriangle className="h-5 w-5 text-[hsl(var(--destructive))]" />
          <h3 className="text-sm font-semibold">Urgent upload — pushing others</h3>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mb-3">
          "{projectTitle}" will take the nearest slot for each platform; existing scheduled
          posts will be shifted forward.
        </p>

        {loading && <div className="text-xs">Computing cascade…</div>}
        {error && <div className="text-xs text-[hsl(var(--destructive))]">{error}</div>}

        {preview && (
          <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3 font-mono text-[11px] leading-relaxed">
            {preview.per_platform.map((p) => (
              <div key={p.platform} className="mb-2 last:mb-0">
                <div>
                  <span style={{ color: platformBgHsl(p.platform) }}>
                    {PLATFORM_SHORT[p.platform]}
                  </span>{" "}
                  · {fmt(p.target_slot)} ← <b>this video</b>
                </div>
                {p.displaced.map((d) => (
                  <div key={d.project_id} className="text-[hsl(var(--muted-foreground))] pl-4">
                    ↳ {d.anime_title} · {fmt(d.from_slot)} → {fmt(d.to_slot)}
                  </div>
                ))}
              </div>
            ))}
            {preview.blockers.map((b, i) => (
              <div key={i} className="text-[hsl(var(--destructive))]">
                ✗ {PLATFORM_SHORT[b.platform as Platform]}: {b.reason}
              </div>
            ))}
          </div>
        )}

        {preview && !blocked && (
          <div className="text-[11px] text-[hsl(var(--muted-foreground))] mt-2">
            {totalDisplaced} project{totalDisplaced === 1 ? "" : "s"} will be shifted.
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={!preview || blocked || submitting}
            onClick={async () => {
              setSubmitting(true);
              try {
                await api.cascadeApply(projectId, accountId);
                onClose();
                onConfirmed();
              } catch (err) {
                setError((err as Error).message);
              } finally {
                setSubmitting(false);
              }
            }}
          >
            {submitting ? "Applying…" : "Confirm urgent upload"}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/project-manager/UrgentCascadeModal.tsx
git commit -m "feat(upload): add UrgentCascadeModal preview + confirm"
```

---

## Task 7: Build `UploadSplitButton`

**Files:**
- Create: `frontend/src/components/project-manager/UploadSplitButton.tsx`

- [ ] **Step 1: Implement**

```tsx
import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Calendar, ChevronDown, Loader2, UploadCloud, Zap } from "lucide-react";
import type { Account, ProjectManagerRow } from "@/types";

interface UploadSplitButtonProps {
  row: ProjectManagerRow;
  selectedAccount: Account | null;
  uploadActive: boolean;
  uploadLabel: string | null;
  disabled: boolean;
  disabledReason?: string;
  onAuto: () => void;
  onSchedule: () => void;
  onUrgent: () => void;
}

export function UploadSplitButton({
  row, selectedAccount, uploadActive, uploadLabel,
  disabled, disabledReason,
  onAuto, onSchedule, onUrgent,
}: UploadSplitButtonProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  void row;

  const accountHasTikTok = !!selectedAccount?.slots_by_platform?.tiktok?.length;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div className="relative inline-flex" ref={ref} title={disabledReason}>
      <button
        type="button"
        onClick={onAuto}
        disabled={disabled || uploadActive}
        className="bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-sm px-3 py-1.5 rounded-l-md border-r border-[hsl(var(--primary-foreground))]/20 disabled:opacity-50 inline-flex items-center gap-1.5 active:scale-95 transition-transform"
      >
        {uploadActive ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            {uploadLabel || "Uploading"}
          </>
        ) : (
          <>
            <UploadCloud className="h-4 w-4" />
            Upload
          </>
        )}
      </button>
      <button
        type="button"
        onClick={() => !disabled && !uploadActive && setOpen((p) => !p)}
        disabled={disabled || uploadActive}
        className="bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-sm px-2 py-1.5 rounded-r-md disabled:opacity-50"
        aria-label="Upload options"
      >
        <ChevronDown className="h-4 w-4" />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -2 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -2 }}
            transition={{ duration: 0.12 }}
            className="absolute right-0 top-full mt-1 w-72 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-xl z-30 p-1.5 text-sm"
          >
            <button
              type="button"
              onClick={() => { setOpen(false); onAuto(); }}
              className="w-full text-left flex items-start gap-2.5 px-2.5 py-2 rounded hover:bg-[hsl(var(--muted))]"
            >
              <UploadCloud className="h-4 w-4 mt-0.5 text-[hsl(var(--primary))]" />
              <div>
                <div>Upload now</div>
                <div className="text-[11px] text-[hsl(var(--muted-foreground))]">Auto: next free slot</div>
              </div>
            </button>
            <button
              type="button"
              disabled={!accountHasTikTok}
              onClick={() => { setOpen(false); onSchedule(); }}
              title={accountHasTikTok ? undefined : "Manual scheduling requires a TikTok-enabled account"}
              className="w-full text-left flex items-start gap-2.5 px-2.5 py-2 rounded hover:bg-[hsl(var(--muted))] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Calendar className="h-4 w-4 mt-0.5 text-blue-500" />
              <div>
                <div>Schedule for specific slot…</div>
                <div className="text-[11px] text-[hsl(var(--muted-foreground))]">Pick a TikTok slot manually</div>
              </div>
            </button>
            <div className="border-t border-[hsl(var(--border))] my-1" />
            <button
              type="button"
              onClick={() => { setOpen(false); onUrgent(); }}
              className="w-full text-left flex items-start gap-2.5 px-2.5 py-2 rounded hover:bg-[hsl(var(--destructive))]/10 text-[hsl(var(--destructive))]"
            >
              <Zap className="h-4 w-4 mt-0.5" />
              <div>
                <div>Upload urgently (push others)</div>
                <div className="text-[11px] opacity-80">Take nearest slot · cascades existing</div>
              </div>
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/project-manager/UploadSplitButton.tsx
git commit -m "feat(upload): add UploadSplitButton with auto/schedule/urgent options"
```

---

## Task 8: Wire `UploadSplitButton` into `ProjectRow`

**Files:**
- Modify: `frontend/src/components/project-manager/ProjectRow.tsx`
- Modify: `frontend/src/components/project-manager/ProjectManagerModal.tsx`

- [ ] **Step 1: Replace the inline Upload button**

In `ProjectRow.tsx` around lines 74-93, replace the `uploadButton` JSX with:

```tsx
import { UploadSplitButton } from "./UploadSplitButton";

// ... in the props interface, add:
//   onUploadSchedule: (row: ProjectManagerRow) => void;
//   onUploadUrgent: (row: ProjectManagerRow) => void;
//   selectedAccount: Account | null;

const uploadButton = (
  <UploadSplitButton
    row={row}
    selectedAccount={selectedAccount ?? null}
    uploadActive={!!uploadState?.active}
    uploadLabel={uploadState?.label ?? null}
    disabled={!canUpload}
    disabledReason={uploadDisabledReason ?? undefined}
    onAuto={() => onUpload(row)}
    onSchedule={() => onUploadSchedule(row)}
    onUrgent={() => onUploadUrgent(row)}
  />
);
```

(Drop the existing tooltip wrapping; the SplitButton already handles the disabled state.)

- [ ] **Step 2: Propagate the new props from `ProjectTable` and `ProjectManagerModal`**

Edit `ProjectTable.tsx` to thread `onUploadSchedule`, `onUploadUrgent`, `selectedAccount` through to `ProjectRow`. Then in `ProjectManagerModal.tsx`, define handlers:

```tsx
const [schedulingForProject, setSchedulingForProject] = useState<{
  row: ProjectManagerRow; accountId: string;
} | null>(null);
const [urgentForProject, setUrgentForProject] = useState<{
  row: ProjectManagerRow; accountId: string;
} | null>(null);

const handleUploadSchedule = useCallback((row: ProjectManagerRow) => {
  const accountId = selectedAccountId
    ?? compatibleAccounts.find((a) => isAccountCompatibleWithProjectRow(a, row))?.id;
  if (!accountId) {
    setError("Pick an account before scheduling manually.");
    return;
  }
  setSchedulingForProject({ row, accountId });
}, [selectedAccountId, compatibleAccounts]);

const handleUploadUrgent = useCallback((row: ProjectManagerRow) => {
  const accountId = selectedAccountId
    ?? compatibleAccounts.find((a) => isAccountCompatibleWithProjectRow(a, row))?.id;
  if (!accountId) {
    setError("Pick an account before urgent upload.");
    return;
  }
  setUrgentForProject({ row, accountId });
}, [selectedAccountId, compatibleAccounts]);
```

Pass `handleUploadSchedule`, `handleUploadUrgent` into `ProjectTable`, and add SlotPickerPopover + UrgentCascadeModal mounts at the bottom of the modal:

```tsx
{schedulingForProject && (
  <SlotPickerPopover
    open
    mode="anchor"
    projectId={schedulingForProject.row.project_id}
    accountId={schedulingForProject.accountId}
    platformsForAnchor={["tiktok", "youtube", "facebook", "instagram"]}
    onClose={() => setSchedulingForProject(null)}
    onConfirm={async (payload) => {
      const anchor = payload as { tiktok_slot: string; overrides?: any };
      setSchedulingForProject(null);
      await startUploadWithChecks(
        schedulingForProject.row.project_id,
        schedulingForProject.accountId,
        "scheduled",
        anchor,
      );
    }}
  />
)}
{urgentForProject && (
  <UrgentCascadeModal
    open
    projectId={urgentForProject.row.project_id}
    projectTitle={urgentForProject.row.anime_title || "Project"}
    accountId={urgentForProject.accountId}
    onClose={() => setUrgentForProject(null)}
    onConfirmed={async () => {
      const ctx = urgentForProject;
      setUrgentForProject(null);
      // Cascade already applied — call upload flow with mode=auto so
      // it consumes the freshly-reserved slots via _try_reuse_platform_reservation.
      await startUploadWithChecks(ctx.row.project_id, ctx.accountId, "auto");
    }}
  />
)}
```

(Imports: `SlotPickerPopover`, `UrgentCascadeModal` from this directory.)

- [ ] **Step 3: Type-check + dev smoke**

```bash
cd frontend && npx tsc --noEmit
cd frontend && npm run dev
```

Open Project Manager → click ▾ on a row → confirm the three options appear. Click "Schedule" — popover opens. Click "Upload urgently" — modal opens.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/project-manager/
git commit -m "feat(upload): wire UploadSplitButton + slot picker + urgent modal"
```

---

## Task 9: Wire `Reschedule whole project` in PlanningModal

**Files:**
- Modify: `frontend/src/components/planning/PlanningModal.tsx`

- [ ] **Step 1: Replace the placeholder**

In `PlanningModal.tsx`, swap the `onRescheduleProject` placeholder for a state-driven mount of `SlotPickerPopover` (anchor mode):

```tsx
import { SlotPickerPopover } from "@/components/project-manager/SlotPickerPopover";

// ... new state:
const [reAnchoring, setReAnchoring] = useState<PlanningEvent | null>(null);

// ... in EventPopover:
onRescheduleProject={() => setReAnchoring(eventForPopover)}

// ... mount near the EventPopover:
{reAnchoring && (
  <SlotPickerPopover
    open
    mode="anchor"
    projectId={reAnchoring.project_id}
    accountId={reAnchoring.account_id}
    initialIso={
      events.find((e) => e.project_id === reAnchoring.project_id && e.platform === "tiktok")?.slot
    }
    platformsForAnchor={Array.from(new Set(
      events
        .filter((e) => e.project_id === reAnchoring.project_id)
        .map((e) => e.platform)
    ))}
    onClose={() => setReAnchoring(null)}
    onConfirm={async (payload) => {
      const anchor = payload as { tiktok_slot: string; overrides?: any };
      await api.rescheduleAnchor(reAnchoring.project_id, anchor);
      setReAnchoring(null);
      setPopover(null);
      await reload();
    }}
  />
)}
```

Also wire single-platform mode for `Reschedule slot`:

```tsx
const [reslottingSingle, setReslottingSingle] = useState<PlanningEvent | null>(null);

// in EventPopover:
onRescheduleSlot={() => setReslottingSingle(eventForPopover)}

// mount:
{reslottingSingle && (
  <SlotPickerPopover
    open
    mode="single-platform"
    projectId={reslottingSingle.project_id}
    accountId={reslottingSingle.account_id}
    platform={reslottingSingle.platform}
    platformsForAnchor={[reslottingSingle.platform]}
    initialIso={reslottingSingle.slot}
    onClose={() => setReslottingSingle(null)}
    onConfirm={async (payload) => {
      const single = payload as { slot: string };
      await api.reschedulePlatform(
        reslottingSingle.project_id,
        reslottingSingle.platform,
        single.slot,
      );
      setReslottingSingle(null);
      setPopover(null);
      await reload();
    }}
  />
)}
```

Drop the previous `window.prompt` flow.

- [ ] **Step 2: Type-check + smoke**

```bash
cd frontend && npx tsc --noEmit
cd frontend && npm run dev
```

In Planning, click an event → click "Reschedule slot" — popover opens. Click "Reschedule whole project" (only enabled if TT exists) — popover opens.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/planning/PlanningModal.tsx
git commit -m "feat(planning): replace prompts with SlotPickerPopover for both reschedule modes"
```

---

## Task 10: Playwright e2e — split button auto path unchanged

**Files:**
- Create: `frontend/e2e/upload-split-button.spec.ts`

- [ ] **Step 1: Write the test**

```ts
import { expect, test } from "@playwright/test";

const ACCOUNT = {
  id: "acc_a", name: "A", language: "fr",
  avatar_url: "/api/accounts/acc_a/avatar",
  supported_types: ["anime"],
  slots: ["14:00"],
  slots_by_platform: {
    youtube: ["14:00"], facebook: ["14:00"], instagram: ["14:00"],
    tiktok: ["14:00", "18:00"],
  },
};

const ROW = {
  project_id: "p1", anime_title: "Show Alpha", library_type: "anime", language: "fr",
  local_size_bytes: 1024, uploaded: false, uploaded_status: "red",
  can_upload_status: "green", can_upload_reasons: [], has_metadata: true,
  drive_video_count: 1, drive_video_name: "p1.mp4",
  drive_video_web_url: "https://drive.example/p1",
  drive_folder_id: "folder", drive_folder_url: "https://drive.example/folder",
  drive_video_id: "drive-1",
  created_at: "2026-04-12T09:00:00Z",
  scheduled_at: null, scheduled_account_id: null,
};

function installMocks() {
  return () => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.origin,
      );
      if (url.pathname === "/api/accounts") {
        return new Response(JSON.stringify({ accounts: [ACCOUNT] }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname === "/api/project-manager/projects") {
        return new Response(JSON.stringify({ projects: [ROW] }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname === "/api/project-manager/upload-jobs") {
        return new Response(JSON.stringify({ jobs: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname === "/api/project-manager/upload-jobs/stream") {
        return new Response(new ReadableStream({ start(c) { c.close(); } }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.pathname.endsWith("/copyright-check")) {
        return new Response(JSON.stringify({ copyrighted: false }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname.endsWith("/facebook-check") || url.pathname.endsWith("/youtube-check")) {
        return new Response(JSON.stringify({ needed: false, duration_seconds: 30,
          speed_factor: 1, sped_up_available: false }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname.endsWith("/upload") && init?.method === "POST") {
        // @ts-expect-error inject flag
        window.__uploadCalled = true;
        return new Response(JSON.stringify({
          job_id: "j1", project_id: "p1", account_id: "acc_a",
          status: "queued", phase: "prepare", message: null, error: null,
          platform_results: [], result: null,
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return orig(input, init);
    };
  };
}

test("Auto upload (single click on Upload) still works as before", async ({ page }) => {
  await page.addInitScript(installMocks());
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();
  await page.getByRole("button", { name: "Account A" }).nth(1).click();  // pick acc_a in dropdown
  await page.getByRole("button", { name: /^Upload$/ }).click();
  await page.waitForFunction(() => (window as any).__uploadCalled === true);
});
```

- [ ] **Step 2: Run**

```bash
cd frontend && npx playwright test e2e/upload-split-button.spec.ts -g "Auto upload" --reporter=line
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/upload-split-button.spec.ts
git commit -m "test(upload): playwright coverage for auto path"
```

---

## Task 11: Playwright e2e — schedule mode

**Files:**
- Modify: `frontend/e2e/upload-split-button.spec.ts`

- [ ] **Step 1: Append**

```ts
test("Schedule mode reserves anchor before upload", async ({ page }) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.origin,
      );
      if (url.pathname === "/api/scheduling/free-slots") {
        return new Response(JSON.stringify({
          slots: [{ slot: "2026-05-08T14:00:00Z", available: true },
                  { slot: "2026-05-08T18:00:00Z", available: true }],
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname === "/api/scheduling/resolve-anchor") {
        return new Response(JSON.stringify({
          resolved: {
            tiktok: { slot: "2026-05-08T14:00:00Z", scheduled_at: "2026-05-08T14:08:00Z", available: true },
            youtube: { slot: "2026-05-08T14:00:00Z", scheduled_at: "2026-05-08T14:09:00Z", available: true },
          },
          conflicts: [],
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname.includes("/reserve-anchor") && init?.method === "POST") {
        // @ts-expect-error inject flag
        window.__anchored = true;
        return new Response(JSON.stringify({
          platform_schedules: { tiktok: { slot: "2026-05-08T14:00:00Z", scheduled_at: "2026-05-08T14:08:00Z" } },
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return orig(input, init);
    };
  });
  await page.addInitScript(installMocks());
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();
  await page.getByRole("button", { name: "Account A" }).nth(1).click();
  await page.getByRole("button", { name: "Upload options" }).click();
  await page.getByRole("button", { name: /Schedule for specific slot/ }).click();
  await page.getByRole("button", { name: /^14:00$/ }).first().click();
  await page.getByRole("button", { name: "Schedule" }).click();
  await page.waitForFunction(() => (window as any).__anchored === true);
  await page.waitForFunction(() => (window as any).__uploadCalled === true);
});
```

- [ ] **Step 2: Run**

```bash
cd frontend && npx playwright test e2e/upload-split-button.spec.ts -g "Schedule mode" --reporter=line
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/upload-split-button.spec.ts
git commit -m "test(upload): playwright coverage for scheduled mode"
```

---

## Task 12: Playwright e2e — urgent mode

**Files:**
- Modify: `frontend/e2e/upload-split-button.spec.ts`

- [ ] **Step 1: Append**

```ts
test("Urgent mode previews and applies cascade", async ({ page }) => {
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.origin,
      );
      if (url.pathname.endsWith("/cascade-preview")) {
        return new Response(JSON.stringify({
          per_platform: [{
            platform: "tiktok",
            target_slot: "2026-05-07T14:00:00Z",
            target_scheduled_at: "2026-05-07T14:09:00Z",
            displaced: [{
              project_id: "x", anime_title: "Bumped",
              from_slot: "2026-05-07T14:00:00Z", to_slot: "2026-05-07T18:00:00Z",
              requires_platform_notification: true,
            }],
          }],
          blockers: [],
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.pathname.endsWith("/cascade-apply") && init?.method === "POST") {
        // @ts-expect-error inject flag
        window.__cascadeApplied = true;
        return new Response(JSON.stringify({
          per_platform: [], blockers: [], notification_status: {},
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return orig(input, init);
    };
  });
  await page.addInitScript(installMocks());
  await page.goto("/");
  await page.getByRole("button", { name: "Projects" }).click();
  await page.getByRole("button", { name: "Account A" }).click();
  await page.getByRole("button", { name: "Account A" }).nth(1).click();
  await page.getByRole("button", { name: "Upload options" }).click();
  await page.getByRole("button", { name: /Upload urgently/ }).click();
  await expect(page.getByText(/will be shifted/i)).toBeVisible();
  await page.getByRole("button", { name: /Confirm urgent upload/ }).click();
  await page.waitForFunction(() => (window as any).__cascadeApplied === true);
  await page.waitForFunction(() => (window as any).__uploadCalled === true);
});
```

- [ ] **Step 2: Run**

```bash
cd frontend && npx playwright test e2e/upload-split-button.spec.ts -g "Urgent mode" --reporter=line
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/upload-split-button.spec.ts
git commit -m "test(upload): playwright coverage for urgent mode"
```

---

## Task 13: Final regression

- [ ] **Step 1: Run all e2e**

```bash
cd frontend && npm run test
```

Expected: green.

- [ ] **Step 2: Manual smoke**

```bash
cd frontend && npm run dev
```

Run through:
- Project Manager → ▾ menu shows three options.
- "Upload" (left side) still does auto upload — no extra steps.
- "Schedule for specific slot…" → picker opens, pick slot, see "Other platforms (auto)" preview, confirm → upload starts.
- "Upload urgently" → preview modal shows displacement, confirm → upload starts.
- Planning Modal → click event → "Reschedule slot" picker opens (single-platform). "Reschedule whole project" picker opens (anchor mode).

- [ ] **Step 3: Commit any cleanup**

```bash
git status
```

---

**Phase 3 complete.** End-to-end Planning system is shipped: Planning view, manual scheduling at upload, urgent cascade mode with platform notifications, retry on failure.
