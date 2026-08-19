import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Zap } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";
import type {
  Platform,
  UrgentCollisionItem,
  UrgentCollisionProject,
  UrgentPreview,
  UrgentShiftSpec,
} from "@/types";
import type { UrgentPlan } from "./types";
import { PLATFORM_SHORT } from "@/components/planning/platformColors";
import { SlotPickerPopover } from "./SlotPickerPopover";

interface UrgentImmediateModalProps {
  open: boolean;
  projectId: string;
  projectTitle: string;
  accountId: string;
  onClose: () => void;
  /** Nothing has been persisted when this fires — the plan is applied by the
   * caller at the very end of the flow (after the preflight modals). */
  onContinue: (plan: UrgentPlan) => void;
}

function fmt(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

function fmtTime(iso: string): string {
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
  }).format(new Date(iso));
}

function minutesFromNow(iso: string): number {
  return Math.round((new Date(iso).getTime() - Date.now()) / 60000);
}

function unmovableLabel(item: UrgentCollisionItem): string {
  if (item.reason === "unmovable_published") return "déjà publié";
  if (item.reason === "unmovable_processing")
    return `en cours de publication — publiera quand même vers ${fmtTime(item.scheduled_at)}`;
  return `créneau dépassé — publiera quand même (prévu ${fmtTime(item.scheduled_at)})`;
}

interface RetimeTarget {
  project: UrgentCollisionProject;
  item: UrgentCollisionItem;
  /** anchor (TikTok, phase 1) or single platform (phase 2) */
  kind: "anchor" | "platform";
}

/**
 * "Upload urgently (immediate)" confirmation: two-phase collision check
 * (TikTok first, then the other platforms) against posts publishing within
 * the next hour on the same channels. Every mutation is DEFERRED: this modal
 * only builds the UrgentPlan; closing at any point abandons the upload with
 * zero side effects.
 */
export function UrgentImmediateModal({
  open, projectId, projectTitle, accountId, onClose, onContinue,
}: UrgentImmediateModalProps) {
  const [preview, setPreview] = useState<UrgentPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tiktokOnly, setTiktokOnly] = useState(false);
  const [shifts, setShifts] = useState<Record<string, UrgentShiftSpec>>({});
  const [retiming, setRetiming] = useState<RetimeTarget | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true); setError(null);
    api
      .urgentPreview(projectId, { account_id: accountId, tiktok_only: tiktokOnly })
      .then((p) => { if (!cancelled) setPreview(p); })
      .catch((err) => { if (!cancelled) setError((err as Error).message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, projectId, accountId, tiktokOnly]);

  // Reset recorded shifts when the mode changes (phase-2 shifts may vanish).
  useEffect(() => { setShifts({}); }, [tiktokOnly]);

  const shiftKey = (projectIdOf: string, platform: Platform) =>
    `${projectIdOf}:${platform}`;

  /** A phase-2 item is auto-covered when its project's TikTok shift is an
   * anchor re-timing (the backend re-anchors every platform TikTok-first). */
  const coveredByAnchor = useCallback(
    (p: UrgentCollisionProject) =>
      Object.values(shifts).some(
        (s) => s.project_id === p.project_id && s.kind === "anchor",
      ),
    [shifts],
  );

  const pendingMovable = useMemo(() => {
    if (!preview) return 0;
    let pending = 0;
    for (const proj of preview.phase1) {
      for (const item of proj.items) {
        if (!item.movable) continue;
        if (!shifts[shiftKey(proj.project_id, item.platform)]) pending += 1;
      }
    }
    for (const proj of preview.phase2) {
      if (coveredByAnchor(proj)) continue;
      for (const item of proj.items) {
        if (!item.movable) continue;
        if (!shifts[shiftKey(proj.project_id, item.platform)]) pending += 1;
      }
    }
    return pending;
  }, [preview, shifts, coveredByAnchor]);

  const hasCollisions =
    (preview?.phase1.length ?? 0) + (preview?.phase2.length ?? 0) > 0;

  const recordShift = useCallback(
    (target: RetimeTarget, spec: UrgentShiftSpec) => {
      setShifts((prev) => {
        const next = { ...prev };
        next[shiftKey(target.project.project_id, target.item.platform)] = spec;
        if (spec.kind === "anchor") {
          // The anchor shift covers the project's other platforms: drop any
          // per-platform shift previously recorded for the same project.
          for (const key of Object.keys(next)) {
            if (
              next[key].project_id === target.project.project_id &&
              next[key].kind === "platform"
            ) {
              delete next[key];
            }
          }
        }
        return next;
      });
    },
    [],
  );

  const shiftFor = (proj: UrgentCollisionProject, item: UrgentCollisionItem) =>
    shifts[shiftKey(proj.project_id, item.platform)];

  const renderItem = (
    proj: UrgentCollisionProject,
    item: UrgentCollisionItem,
    kind: "anchor" | "platform",
    covered: boolean,
  ) => {
    const recorded = shiftFor(proj, item);
    const mins = minutesFromNow(item.scheduled_at);
    return (
      <div
        key={`${proj.project_id}:${item.platform}`}
        className="flex items-center gap-2 py-1 text-[12px]"
      >
        <span className="font-mono text-[11px] w-7 shrink-0">
          {PLATFORM_SHORT[item.platform]}
        </span>
        <span className="truncate flex-1">
          {proj.anime_title} · {fmt(item.scheduled_at)}
          {mins >= 0 && (
            <span className="text-[hsl(var(--muted-foreground))]"> (dans {mins} min)</span>
          )}
        </span>
        {!item.movable ? (
          <span className="text-amber-500 text-[11px]">⚠ {unmovableLabel(item)}</span>
        ) : covered ? (
          <span className="text-[hsl(var(--muted-foreground))] text-[11px]">
            suit TikTok (auto)
          </span>
        ) : recorded ? (
          <span className="text-emerald-500 text-[11px] font-mono">
            → {fmt(
              recorded.manual_at ?? recorded.tiktok_slot ?? recorded.slot ?? item.scheduled_at,
            )}
          </span>
        ) : (
          <span className="text-[hsl(var(--muted-foreground))] text-[11px] font-mono">
            {item.suggested_slot ? `proposé → ${fmt(item.suggested_slot)}` : ""}
          </span>
        )}
        {item.movable && !covered && (
          <Button
            size="sm"
            variant="outline"
            className="h-6 px-2 text-[11px]"
            onClick={() => setRetiming({ project: proj, item, kind })}
          >
            {recorded ? "Modifier" : "Re-planifier"}
          </Button>
        )}
        {item.best_effort && item.movable && (
          <span
            className="text-amber-500 text-[11px]"
            title="Publication TikTok dans moins de 15 min : le déplacement sera tenté au mieux (best-effort)"
          >
            ⚠
          </span>
        )}
      </div>
    );
  };

  const handleContinue = () => {
    onContinue({
      tiktokOnly,
      shifts: Object.values(shifts),
      // ownReservations is filled by the follow-up picker in TikTok-only mode.
    });
  };

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[60] bg-black/55 flex items-center justify-center"
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-5 w-[560px] max-h-[80vh] overflow-y-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-1">
          <Zap className="h-5 w-5 text-[hsl(var(--destructive))]" />
          <h3 className="text-sm font-semibold">Upload urgent (immédiat)</h3>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mb-3">
          «{projectTitle}» sera publié <strong>immédiatement</strong>
          {tiktokOnly ? " sur TikTok uniquement" : " sur toutes les plateformes disponibles"}
          , hors système de créneaux. Rien n'est appliqué avant la confirmation finale.
        </p>

        <label className="flex items-center gap-2 text-xs mb-3">
          <input
            type="checkbox"
            checked={tiktokOnly}
            onChange={(e) => setTiktokOnly(e.target.checked)}
          />
          TikTok immédiat uniquement — planifier les autres plateformes
        </label>

        {loading && <div className="text-xs">Analyse des collisions…</div>}
        {error && (
          <div className="text-xs text-[hsl(var(--destructive))] mb-2">{error}</div>
        )}

        {preview && !loading && (
          <div className="space-y-3">
            {!hasCollisions && (
              <div className="text-xs text-emerald-500">
                Aucun upload prévu dans l'heure à venir sur ces chaînes.
              </div>
            )}
            {preview.phase1.length > 0 && (
              <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
                <div className="text-[11px] font-semibold mb-1">
                  1. TikTok — uploads à moins de {preview.window_minutes} min
                </div>
                {preview.phase1.map((proj) =>
                  proj.items.map((item) => renderItem(proj, item, "anchor", false)),
                )}
              </div>
            )}
            {preview.phase2.length > 0 && (
              <div className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
                <div className="text-[11px] font-semibold mb-1">
                  2. Autres plateformes — uploads à moins de {preview.window_minutes} min
                </div>
                {preview.phase2.map((proj) =>
                  proj.items.map((item) =>
                    renderItem(proj, item, "platform", coveredByAnchor(proj)),
                  ),
                )}
              </div>
            )}
            {pendingMovable > 0 && (
              <div className="text-[11px] text-amber-500">
                Re-planifiez les {pendingMovable} upload{pendingMovable > 1 ? "s" : ""} en
                collision avant de continuer.
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={!preview || loading || pendingMovable > 0}
            onClick={handleContinue}
          >
            Continuer
          </Button>
        </div>

        {retiming && (
          <SlotPickerPopover
            open
            mode={retiming.kind === "anchor" ? "anchor" : "single-platform"}
            projectId={retiming.project.project_id}
            accountId={retiming.project.account_id}
            platform={retiming.kind === "platform" ? retiming.item.platform : undefined}
            platformsForAnchor={
              ["tiktok", "youtube", "facebook", "instagram"] as Platform[]
            }
            initialIso={retiming.item.suggested_slot ?? undefined}
            onClose={() => setRetiming(null)}
            onConfirm={async (payload) => {
              // DEFERRED: record the spec only — applied at final confirm.
              const target = retiming;
              const expected = {
                [target.item.platform]: target.item.scheduled_at,
              };
              if ("manual_at" in payload) {
                recordShift(target, {
                  project_id: target.project.project_id,
                  kind: target.kind,
                  platform:
                    target.kind === "platform" ? target.item.platform : undefined,
                  manual_at: payload.manual_at,
                  expected_scheduled_at: expected,
                });
              } else if ("tiktok_slot" in payload) {
                recordShift(target, {
                  project_id: target.project.project_id,
                  kind: "anchor",
                  tiktok_slot: payload.tiktok_slot,
                  overrides: payload.overrides,
                  steals: payload.steals,
                  expected_scheduled_at: expected,
                });
              } else if ("slot" in payload) {
                recordShift(target, {
                  project_id: target.project.project_id,
                  kind: "platform",
                  platform: target.item.platform,
                  slot: payload.slot,
                  steals: payload.steal
                    ? { [target.item.platform]: payload.steal }
                    : undefined,
                  expected_scheduled_at: expected,
                });
              }
              setRetiming(null);
            }}
          />
        )}
      </motion.div>
    </div>
  );
}
