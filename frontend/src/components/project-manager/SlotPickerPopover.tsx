import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type { FreeSlot, Platform, ResolveAnchorResult, StealSpec, SwitchMode, SwitchPreview } from "@/types";
import { PLATFORM_SHORT } from "@/components/planning/platformColors";
import { SlotPickerCalendar } from "./SlotPickerCalendar";
import { SlotChips } from "./SlotChips";
import { PerPlatformOverride } from "./PerPlatformOverride";
import { SwitchSlotConfirmModal } from "./SwitchSlotConfirmModal";

export type SlotPickerMode = "anchor" | "single-platform";

export type SlotPickerConfirmPayload =
  | {
      tiktok_slot: string;
      overrides?: Partial<Record<Platform, string>>;
      steals?: Partial<Record<Platform, StealSpec>>;
    } // anchor
  | { slot: string; steal?: StealSpec } // single-platform
  | { manual_at: string }; // manual custom time

interface SlotPickerPopoverProps {
  open: boolean;
  onClose: () => void;
  mode: SlotPickerMode;
  projectId: string;
  accountId: string;
  platform?: Platform;            // required when mode = single-platform
  initialIso?: string;             // pre-fill (current slot when rescheduling)
  platformsForAnchor: Platform[]; // anchor mode: which platforms are in scope
  allowManual?: boolean;           // default true in anchor mode
  initialManual?: boolean;         // open with custom time active
  onConfirm: (payload: SlotPickerConfirmPayload) => Promise<void>;
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
  const [customTime, setCustomTime] = useState<string>("");       // "HH:MM"
  const [customActive, setCustomActive] = useState(!!props.initialManual);
  const [steals, setSteals] = useState<Partial<Record<Platform, StealSpec>>>({});
  const [switchTarget, setSwitchTarget] = useState<{ platform: Platform; slotIso: string } | null>(null);

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

  // Fetch a wide window of slots for the displayed month so the calendar can
  // strike days that have no configured slot or no free slot.
  const [monthSlots, setMonthSlots] = useState<FreeSlot[]>([]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const monthStart = new Date(
      monthAnchor.getFullYear(),
      monthAnchor.getMonth(),
      1,
    );
    monthStart.setHours(0, 0, 0, 0);
    (async () => {
      try {
        // Backend caps `limit` at 200. Even a 1-slot/day platform (YT) gets
        // ~6 months of coverage with 200, well past the visible month.
        const r = await api.listFreeSlots({
          account_id: accountId,
          platform: platformForFetch,
          after: monthStart.toISOString(),
          limit: 200,
        });
        if (cancelled) return;
        setMonthSlots(r.slots);
      } catch {
        if (!cancelled) setMonthSlots([]);
      }
    })();
    return () => { cancelled = true; };
  }, [open, monthAnchor, accountId, platformForFetch]);

  const { daysWithSlots, daysWithFreeSlots } = useMemo(() => {
    const all = new Set<string>();
    const free = new Set<string>();
    for (const s of monthSlots) {
      const d = new Date(s.slot);
      const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      all.add(ymd);
      if (s.available) free.add(ymd);
    }
    return { daysWithSlots: all, daysWithFreeSlots: free };
  }, [monthSlots]);

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

  const customIso = useMemo(() => {
    if (!customActive || !selectedDate || !/^\d{2}:\d{2}$/.test(customTime)) return null;
    const [h, m] = customTime.split(":").map(Number);
    const d = new Date(selectedDate);
    d.setHours(h, m, 0, 0);
    return d.toISOString();
  }, [customActive, selectedDate, customTime]);

  const customTooClose =
    customIso !== null && new Date(customIso).getTime() < Date.now() + 30 * 60 * 1000;

  const proximityWarning = useMemo(() => {
    if (!customIso) return null;
    const t = new Date(customIso).getTime();
    const near = slotsForDay.find(
      (s) => !s.available && Math.abs(new Date(s.slot).getTime() - t) <= 60 * 60 * 1000,
    );
    return near
      ? `Un upload (« ${near.taken_by_title ?? "?"} ») est déjà programmé vers ${new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris" }).format(new Date(near.slot))} ce jour-là.`
      : null;
  }, [customIso, slotsForDay]);

  const effectiveConflicts = useMemo(
    () =>
      (resolveResult?.conflicts ?? []).filter(
        (c) => !(c.reason === "slot_taken" && steals[c.platform]),
      ),
    [resolveResult, steals],
  );

  const canSubmit = useMemo(() => {
    if (customActive) return !!customIso && !customTooClose;
    if (!selectedSlotIso) return false;
    if (mode === "anchor" && effectiveConflicts.length) return false;
    return true;
  }, [customActive, customIso, customTooClose, selectedSlotIso, mode, effectiveConflicts]);

  const handleSubmit = useCallback(async () => {
    setSubmitting(true); setError(null);
    try {
      if (customActive && customIso) {
        await onConfirm({ manual_at: customIso });
      } else if (mode === "anchor" && selectedSlotIso) {
        await onConfirm({
          tiktok_slot: selectedSlotIso,
          overrides,
          steals: Object.keys(steals).length ? steals : undefined,
        });
      } else if (selectedSlotIso) {
        await onConfirm({ slot: selectedSlotIso, steal: steals[platform!] });
      } else {
        return;
      }
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [customActive, customIso, selectedSlotIso, mode, overrides, steals, platform, onConfirm, onClose]);

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
          daysWithSlots={daysWithSlots}
          daysWithFreeSlots={daysWithFreeSlots}
        />
        <div className="border-t border-[hsl(var(--border))] mt-3 pt-2">
          <div className="text-[11px] text-[hsl(var(--muted-foreground))] mb-1.5">
            {mode === "anchor" ? "TikTok slots" : `${PLATFORM_SHORT[platform!]} slots`}
            {selectedDate && ` · ${selectedDate.toLocaleDateString("fr-FR")}`}
          </div>
          <SlotChips
            slots={slotsForDay}
            selectedIso={customActive ? null : selectedSlotIso}
            onSelect={(iso) => {
              setCustomActive(false);
              setSelectedSlotIso(iso);
              setSteals((prev) => {
                const next = { ...prev };
                delete next[platformForFetch];
                return next;
              });
            }}
            onSelectTaken={(s) =>
              setSwitchTarget({ platform: platformForFetch, slotIso: s.slot })
            }
            stolenIsos={
              new Set(
                steals[platformForFetch] && selectedSlotIso ? [selectedSlotIso] : [],
              )
            }
            ownProjectId={projectId}
          />
        </div>

        {mode === "anchor" && props.allowManual !== false && (
          <div className="border-t border-[hsl(var(--border))] mt-3 pt-2">
            <label className="flex items-center gap-2 text-[11px] text-[hsl(var(--muted-foreground))]">
              <input
                type="checkbox"
                checked={customActive}
                onChange={(e) => {
                  setCustomActive(e.target.checked);
                  if (e.target.checked) { setSelectedSlotIso(null); setSteals({}); }
                }}
              />
              Heure personnalisée (hors slots, toutes plateformes)
            </label>
            {customActive && (
              <div className="mt-1.5">
                <input
                  type="time"
                  value={customTime}
                  onChange={(e) => setCustomTime(e.target.value)}
                  className="text-xs bg-transparent border border-[hsl(var(--border))] rounded px-2 py-1"
                />
                {customTooClose && (
                  <div className="text-[11px] text-[hsl(var(--destructive))] mt-1">
                    Minimum 30 minutes dans le futur.
                  </div>
                )}
                {proximityWarning && (
                  <div className="text-[11px] text-amber-500 mt-1">⚠ {proximityWarning}</div>
                )}
              </div>
            )}
          </div>
        )}

        {!customActive && mode === "anchor" && resolveResult && (
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
              {effectiveConflicts.length > 0 && (
                <div className="mt-1 text-[hsl(var(--destructive))]">
                  Conflict: {effectiveConflicts.map((c) => `${c.platform}:${c.reason}`).join(", ")}
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
              onStealRequest={(p, iso) => setSwitchTarget({ platform: p, slotIso: iso })}
            />
          </>
        )}

        {error && (
          <div className="mt-2 text-xs text-[hsl(var(--destructive))]">{error}</div>
        )}

        <div className="flex justify-end gap-2 mt-3">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={!canSubmit || submitting} onClick={handleSubmit}>
            {submitting ? "Saving…" : customActive ? "Programmer (manuel)" : "Schedule"}
          </Button>
        </div>

        {switchTarget && (
          <SwitchSlotConfirmModal
            open
            projectId={projectId}
            accountId={accountId}
            platform={switchTarget.platform}
            slotIso={switchTarget.slotIso}
            onClose={() => setSwitchTarget(null)}
            onChoose={(chosenMode: SwitchMode, preview: SwitchPreview) => {
              if (!switchTarget) return;
              const tgt = switchTarget;
              setSteals((prev) => ({
                ...prev,
                [tgt.platform]: {
                  mode: chosenMode,
                  expected_occupant_id: preview.occupant_project_id,
                },
              }));
              if (tgt.platform === platformForFetch) {
                setSelectedSlotIso(tgt.slotIso);
                setCustomActive(false);
              } else {
                setOverrides((prev) => ({ ...prev, [tgt.platform]: tgt.slotIso }));
              }
            }}
          />
        )}
      </motion.div>
    </div>
  );
}
