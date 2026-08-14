import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, Users } from "lucide-react";
import clsx from "clsx";
import { ALL_PLATFORMS, type Account, type Platform } from "@/types";
import { platformBgHsl, platformTranslucentHsl, PLATFORM_SHORT } from "./platformColors";

export type PlanningView = "week" | "month" | "agenda";

const VIEW_LABELS: Record<PlanningView, string> = {
  week: "Semaine",
  month: "Mois",
  agenda: "Agenda",
};

interface PlanningToolbarProps {
  view: PlanningView;
  onViewChange: (view: PlanningView) => void;
  periodLabel: string;
  onPrev: () => void;
  onNext: () => void;
  onToday: () => void;
  accounts: Account[];
  accountId: string | null;
  onAccountChange: (accountId: string | null) => void;
  platforms: Platform[];
  onPlatformsChange: (platforms: Platform[]) => void;
}

function AccountAvatar({ account, size = "h-5 w-5" }: { account: Account; size?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span
        className={clsx(
          size,
          "flex shrink-0 items-center justify-center rounded-full bg-[hsl(var(--secondary))] text-[9px] font-semibold uppercase text-[hsl(var(--muted-foreground))]",
        )}
      >
        {account.name.charAt(0)}
      </span>
    );
  }
  return (
    <img
      src={account.avatar_url}
      alt=""
      className={clsx(size, "shrink-0 rounded-full bg-[hsl(var(--secondary))] object-cover")}
      onError={() => setFailed(true)}
    />
  );
}

function AccountDropdown({
  accounts,
  accountId,
  onAccountChange,
}: {
  accounts: Account[];
  accountId: string | null;
  onAccountChange: (accountId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = accounts.find((a) => a.id === accountId) ?? null;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const pick = (id: string | null) => {
    onAccountChange(id);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-label="Filtrer par compte"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded bg-[hsl(var(--secondary))] px-2 py-1.5 text-xs transition-colors hover:bg-[hsl(var(--secondary))]/80"
      >
        {selected ? (
          <AccountAvatar account={selected} />
        ) : (
          <Users className="h-4 w-4 text-[hsl(var(--muted-foreground))]" />
        )}
        <span className="max-w-36 truncate">{selected ? selected.name : "Tous les comptes"}</span>
        <ChevronDown
          className={clsx(
            "h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 max-h-[60vh] min-w-52 overflow-y-auto rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] py-1 shadow-lg">
          <button
            type="button"
            onClick={() => pick(null)}
            className={clsx(
              "flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-[hsl(var(--muted))]",
              !selected && "text-[hsl(var(--primary))]",
            )}
          >
            <Users className="h-5 w-5 text-[hsl(var(--muted-foreground))]" />
            Tous les comptes
          </button>
          {accounts.map((acc) => (
            <button
              key={acc.id}
              type="button"
              onClick={() => pick(acc.id)}
              className={clsx(
                "flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-[hsl(var(--muted))]",
                acc.id === accountId && "bg-[hsl(var(--muted))]/60",
              )}
            >
              <AccountAvatar account={acc} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs">{acc.name}</span>
                <span className="block truncate text-[10px] uppercase text-[hsl(var(--muted-foreground))]">
                  {acc.language}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function PlanningToolbar({
  view,
  onViewChange,
  periodLabel,
  onPrev,
  onNext,
  onToday,
  accounts,
  accountId,
  onAccountChange,
  platforms,
  onPlatformsChange,
}: PlanningToolbarProps) {
  const togglePlatform = (p: Platform) => {
    onPlatformsChange(
      platforms.includes(p) ? platforms.filter((x) => x !== p) : [...platforms, p],
    );
  };
  const allSelected = platforms.length === ALL_PLATFORMS.length;

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-[hsl(var(--border))] pb-3">
      {/* Period navigation */}
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={onPrev}
          aria-label="Période précédente"
          className="rounded p-1 hover:bg-[hsl(var(--secondary))]"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={onToday}
          className="rounded bg-[hsl(var(--secondary))] px-2.5 py-1 text-xs font-medium hover:bg-[hsl(var(--secondary))]/80"
        >
          Aujourd'hui
        </button>
        <button
          type="button"
          onClick={onNext}
          aria-label="Période suivante"
          className="rounded p-1 hover:bg-[hsl(var(--secondary))]"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
      <span className="min-w-32 text-sm font-semibold capitalize tabular-nums">{periodLabel}</span>

      <div className="flex-1" />

      {/* Account filter */}
      <AccountDropdown
        accounts={accounts}
        accountId={accountId}
        onAccountChange={onAccountChange}
      />

      {/* Platform filter */}
      <div className="flex items-center gap-1" role="group" aria-label="Filtrer par plateforme">
        <button
          type="button"
          onClick={() => onPlatformsChange(allSelected ? [] : [...ALL_PLATFORMS])}
          className={clsx(
            "rounded px-1.5 py-1 text-[10px] font-bold transition-colors",
            allSelected
              ? "bg-[hsl(var(--secondary))] text-[hsl(var(--foreground))]"
              : "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--secondary))]/60",
          )}
        >
          Tout
        </button>
        {ALL_PLATFORMS.map((p) => {
          const active = platforms.includes(p);
          return (
            <button
              key={p}
              type="button"
              onClick={() => togglePlatform(p)}
              aria-pressed={active}
              className={clsx(
                "rounded px-1.5 py-1 text-[10px] font-bold transition-colors",
                !active && "opacity-35",
              )}
              style={{
                backgroundColor: platformTranslucentHsl(p, active ? 0.18 : 0.08),
                color: platformBgHsl(p),
              }}
            >
              {PLATFORM_SHORT[p]}
            </button>
          );
        })}
      </div>

      {/* View switch */}
      <div className="flex rounded-md bg-[hsl(var(--secondary))] p-0.5" role="tablist">
        {(Object.keys(VIEW_LABELS) as PlanningView[]).map((v) => (
          <button
            key={v}
            type="button"
            role="tab"
            aria-selected={view === v}
            onClick={() => onViewChange(v)}
            className={clsx(
              "rounded px-2.5 py-1 text-xs font-medium transition-colors",
              view === v
                ? "bg-[hsl(var(--card))] text-[hsl(var(--foreground))] shadow-sm"
                : "text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]",
            )}
          >
            {VIEW_LABELS[v]}
          </button>
        ))}
      </div>
    </div>
  );
}
