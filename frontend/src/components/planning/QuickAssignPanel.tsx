import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import clsx from "clsx";
import { api } from "@/api/client";
import type { Account, Platform } from "@/types";
import { fmtTime, relativeDayLabel } from "@/utils/parisTime";
import { SlotPickerPopover } from "@/components/project-manager/SlotPickerPopover";
import { planningKeys, useEligibleProjects } from "./data/queries";
import { PLATFORM_SHORT, platformBgHsl, platformTranslucentHsl } from "./platformColors";

interface QuickAssignPanelProps {
  slot: string;
  account: Account;
  onClose: () => void;
  onReserveAnchor: (
    projectId: string,
    payload: { account_id: string; tiktok_slot: string },
  ) => Promise<boolean>;
  onReserveManual: (
    projectId: string,
    payload: { account_id: string; at: string },
  ) => Promise<boolean>;
  notify: (message: string) => void;
}

/**
 * Panel opened by clicking a free TikTok ghost: pick a ready project and
 * reserve the whole anchor there. Reserve-only: the upload itself is still
 * launched from the Project Manager.
 */
export function QuickAssignPanel({
  slot,
  account,
  onClose,
  onReserveAnchor,
  onReserveManual,
  notify,
}: QuickAssignPanelProps) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fullPickerOpen, setFullPickerOpen] = useState(false);

  const eligible = useEligibleProjects(true);
  const projects = useMemo(
    () =>
      (eligible.data ?? []).filter((p) =>
        (p.anime_title ?? p.project_id).toLowerCase().includes(query.toLowerCase()),
      ),
    [eligible.data, query],
  );

  const restrictions = useQuery({
    queryKey: [...planningKeys.all, "restrictions", selectedId],
    queryFn: () => api.getUploadRestrictions(selectedId!),
    enabled: !!selectedId,
  });

  const blockedWindow = useMemo(() => {
    if (!restrictions.data) return null;
    const t = new Date(slot).getTime();
    return (
      restrictions.data.blocked_windows.find(
        (w) => new Date(w.start).getTime() <= t && t <= new Date(w.end).getTime(),
      ) ?? null
    );
  }, [restrictions.data, slot]);

  const accountBlocked = useMemo(() => {
    if (!restrictions.data) return null;
    return (
      restrictions.data.blocked_accounts.find((b) => b.account_id === account.id) ?? null
    );
  }, [restrictions.data, account.id]);

  const preview = useQuery({
    queryKey: [...planningKeys.all, "resolvePreview", selectedId, slot, account.id],
    queryFn: () =>
      api.resolveAnchor({
        project_id: selectedId!,
        account_id: account.id,
        tiktok_slot: slot,
      }),
    enabled: !!selectedId,
  });
  const conflicts = preview.data?.conflicts ?? [];

  const confirmDisabled =
    !selectedId ||
    submitting ||
    !!blockedWindow ||
    !!accountBlocked ||
    preview.isLoading ||
    conflicts.length > 0;

  const doReserve = async () => {
    if (!selectedId) return;
    setSubmitting(true);
    setError(null);
    try {
      const done = await onReserveAnchor(selectedId, {
        account_id: account.id,
        tiktok_slot: slot,
      });
      if (done) {
        notify("Créneau réservé — lancez l'upload depuis le Project Manager avant l'heure prévue.");
        onClose();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <aside
      data-testid="planning-quick-assign-panel"
      className="flex w-[340px] shrink-0 flex-col border-l border-[hsl(var(--border))] bg-[hsl(var(--card))]"
    >
      <div className="border-b border-[hsl(var(--border))] p-4">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold">Planifier un projet</h2>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
              {relativeDayLabel(slot)} à {fmtTime(slot)} · {account.name}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Fermer le panneau"
            className="rounded p-1 hover:bg-[hsl(var(--secondary))]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="mt-3 flex items-center gap-2 rounded-md bg-[hsl(var(--background))] px-2.5 py-1.5">
          <Search className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Rechercher un projet prêt…"
            className="w-full bg-transparent text-xs outline-none placeholder:text-[hsl(var(--muted-foreground))]"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {eligible.isLoading ? (
          <p className="mt-4 text-center text-xs text-[hsl(var(--muted-foreground))]">
            Chargement des projets prêts…
          </p>
        ) : eligible.isError ? (
          <p className="mt-4 text-center text-xs text-[hsl(var(--destructive))]">
            {(eligible.error as Error).message}
          </p>
        ) : projects.length === 0 ? (
          <p className="mt-4 text-center text-xs text-[hsl(var(--muted-foreground))]">
            Aucun projet prêt à planifier{query ? " ne correspond" : ""}.
          </p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {projects.map((p) => {
              const langMismatch =
                !!p.language && p.language.toLowerCase() !== account.language.toLowerCase();
              return (
                <button
                  key={p.project_id}
                  type="button"
                  onClick={() => setSelectedId(p.project_id)}
                  className={clsx(
                    "rounded-md border p-2.5 text-left transition-colors hover:border-[hsl(var(--primary))]/60",
                    selectedId === p.project_id
                      ? "border-[hsl(var(--primary))] ring-1 ring-[hsl(var(--ring))]"
                      : "border-[hsl(var(--border))]",
                  )}
                >
                  <span className="line-clamp-2 text-xs leading-snug">
                    {p.anime_title ?? p.project_id}
                  </span>
                  <span className="mt-1 flex items-center gap-2 text-[10px] text-[hsl(var(--muted-foreground))]">
                    {p.language && <span className="uppercase">{p.language}</span>}
                    {langMismatch && (
                      <span className="rounded bg-amber-400/15 px-1 py-px font-medium text-amber-400">
                        Langue ≠ compte
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {selectedId && (
        <div className="border-t border-[hsl(var(--border))] px-4 py-3 text-xs">
          {accountBlocked ? (
            <p className="text-[hsl(var(--destructive))]">
              Ce compte a déjà publié un projet de la même famille de duplication.
            </p>
          ) : blockedWindow ? (
            <p className="text-[hsl(var(--destructive))]">
              Créneau bloqué : à moins de {restrictions.data?.min_spacing_days ?? 30} jours
              d'un duplicata de même langue.
            </p>
          ) : preview.isLoading ? (
            <p className="text-[hsl(var(--muted-foreground))]">Résolution des créneaux…</p>
          ) : conflicts.length > 0 ? (
            <div>
              <p className="text-amber-400">
                Conflits : {conflicts.map((c) => `${PLATFORM_SHORT[c.platform as Platform] ?? c.platform} (${c.reason})`).join(", ")}
              </p>
              <button
                type="button"
                onClick={() => setFullPickerOpen(true)}
                className="mt-1.5 rounded bg-[hsl(var(--secondary))] px-2 py-1 text-[11px] hover:bg-[hsl(var(--secondary))]/80"
              >
                Ouvrir le sélecteur complet
              </button>
            </div>
          ) : preview.data ? (
            <div className="flex flex-col gap-1">
              {Object.entries(preview.data.resolved).map(([platform, r]) => (
                <div key={platform} className="flex items-center gap-2">
                  <span
                    className="rounded px-1 py-px text-[9px] font-bold"
                    style={{
                      backgroundColor: platformTranslucentHsl(platform as Platform),
                      color: platformBgHsl(platform as Platform),
                    }}
                  >
                    {PLATFORM_SHORT[platform as Platform] ?? platform}
                  </span>
                  <span className="tabular-nums text-[hsl(var(--muted-foreground))]">
                    {relativeDayLabel(r.slot)} à {fmtTime(r.slot)}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}

      <div className="border-t border-[hsl(var(--border))] p-4">
        {error && (
          <p className="mb-2 text-xs text-[hsl(var(--destructive))]">{error}</p>
        )}
        <button
          type="button"
          disabled={confirmDisabled}
          onClick={() => void doReserve()}
          className="w-full rounded bg-[hsl(var(--primary))] px-3 py-2 text-xs font-semibold text-[hsl(var(--primary-foreground))] hover:bg-[hsl(var(--primary))]/90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? "Réservation…" : "Réserver ce créneau"}
        </button>
        <p className="mt-2 text-[10px] leading-snug text-[hsl(var(--muted-foreground))]">
          Réserve les créneaux (TikTok en ancre). L'upload se lance ensuite depuis le
          Project Manager.
        </p>
      </div>

      {fullPickerOpen && selectedId && (
        <SlotPickerPopover
          open
          mode="anchor"
          projectId={selectedId}
          accountId={account.id}
          initialIso={slot}
          platformsForAnchor={["tiktok", "youtube", "facebook", "instagram"]}
          onClose={() => setFullPickerOpen(false)}
          onConfirm={async (payload) => {
            let done = false;
            if ("manual_at" in payload) {
              done = await onReserveManual(selectedId, {
                account_id: account.id,
                at: payload.manual_at,
              });
            } else if ("tiktok_slot" in payload) {
              done = await onReserveAnchor(selectedId, {
                account_id: account.id,
                ...payload,
              });
            }
            if (done) {
              setFullPickerOpen(false);
              notify(
                "Créneau réservé — lancez l'upload depuis le Project Manager avant l'heure prévue.",
              );
              onClose();
            }
          }}
        />
      )}
    </aside>
  );
}
