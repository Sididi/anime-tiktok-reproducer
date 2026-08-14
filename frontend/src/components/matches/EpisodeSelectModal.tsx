import { useEffect, useState } from "react";
import { Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui";

interface EpisodeSelectModalProps {
  open: boolean;
  episodes: string[];
  proposed: string[];
  matching: boolean;
  onClose: () => void;
  onLaunch: (selected: string[]) => void;
}

/**
 * Manual configuration for the episode-subset recompute: every episode of
 * the series, AI-proposed ones pre-selected and badged, recompute runs on
 * the checked subset only.
 */
export function EpisodeSelectModal({
  open,
  episodes,
  proposed,
  matching,
  onClose,
  onLaunch,
}: EpisodeSelectModalProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (open) setSelected(new Set(proposed));
    // Re-seed from the AI proposal each time the modal opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const proposedSet = new Set(proposed);
  const toggle = (episode: string) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(episode)) next.delete(episode);
      else next.add(episode);
      return next;
    });
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl w-full max-w-md p-6 flex flex-col gap-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-amber-500" />
            <h2 className="text-base font-semibold">
              Recompute with selected episodes
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          Matching will only search the checked episodes. AI-proposed episodes
          (from the current results) are pre-selected.
        </p>

        <div className="flex items-center gap-3 text-xs">
          <button
            type="button"
            className="text-[hsl(var(--primary))] hover:underline"
            onClick={() => setSelected(new Set(episodes))}
          >
            All
          </button>
          <button
            type="button"
            className="text-[hsl(var(--primary))] hover:underline"
            onClick={() => setSelected(new Set())}
          >
            None
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1 text-[hsl(var(--primary))] hover:underline disabled:opacity-50 disabled:no-underline"
            onClick={() => setSelected(new Set(proposed))}
            disabled={proposed.length === 0}
          >
            <Sparkles className="h-3 w-3 text-amber-500" />
            Reset to AI
          </button>
        </div>

        <div className="max-h-72 overflow-y-auto border border-[hsl(var(--border))] rounded-md divide-y divide-[hsl(var(--border))]">
          {episodes.length === 0 && (
            <div className="p-3 text-sm text-[hsl(var(--muted-foreground))]">
              No episodes available for this series.
            </div>
          )}
          {episodes.map((episode) => {
            const label = episode.split("/").pop() || episode;
            const isProposed = proposedSet.has(episode);
            return (
              <label
                key={episode}
                className="flex items-center gap-2 px-3 py-2 text-sm cursor-pointer hover:bg-[hsl(var(--muted))]"
              >
                <input
                  type="checkbox"
                  className="rounded"
                  checked={selected.has(episode)}
                  onChange={() => toggle(episode)}
                />
                <span className="truncate" title={label}>
                  {label}
                </span>
                {isProposed && (
                  <Sparkles
                    className="h-3.5 w-3.5 text-amber-500 shrink-0 ml-auto"
                    aria-label="AI proposed"
                  />
                )}
              </label>
            );
          })}
        </div>

        <div className="flex items-center justify-between">
          <span className="text-xs text-[hsl(var(--muted-foreground))]">
            {selected.size} of {episodes.length} selected
          </span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={selected.size === 0 || matching}
              onClick={() =>
                onLaunch(episodes.filter((episode) => selected.has(episode)))
              }
            >
              Recompute
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
