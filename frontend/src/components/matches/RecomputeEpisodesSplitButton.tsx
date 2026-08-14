import { ChevronDown, Sparkles } from "lucide-react";

interface RecomputeEpisodesSplitButtonProps {
  proposedCount: number;
  matching: boolean;
  onRecomputeProposed: () => void;
  onOpenModal: () => void;
}

const SEGMENT_BASE =
  "h-9 text-sm font-medium inline-flex items-center border border-[hsl(var(--border))] " +
  "bg-transparent hover:bg-[hsl(var(--secondary))] transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] " +
  "disabled:pointer-events-none disabled:opacity-50";

/**
 * Split button: primary click recomputes matches restricted to the
 * AI-proposed episodes; the caret opens the episode-selection modal.
 */
export function RecomputeEpisodesSplitButton({
  proposedCount,
  matching,
  onRecomputeProposed,
  onOpenModal,
}: RecomputeEpisodesSplitButtonProps) {
  const noProposal = proposedCount === 0;
  return (
    <div className="relative inline-flex">
      <button
        type="button"
        onClick={onRecomputeProposed}
        disabled={matching || noProposal}
        title={
          noProposal
            ? "No AI episode proposal yet — run matching first"
            : `Re-run matching using only the ${proposedCount} AI-proposed episode${proposedCount !== 1 ? "s" : ""}`
        }
        className={`${SEGMENT_BASE} gap-1 px-3 rounded-l-md border-r-0`}
      >
        <Sparkles className="h-4 w-4 text-amber-500" />
        Recompute ({proposedCount} ep.)
      </button>
      <button
        type="button"
        onClick={onOpenModal}
        disabled={matching}
        aria-label="Choose episodes"
        title="Choose which episodes to recompute with"
        className={`${SEGMENT_BASE} px-1.5 rounded-r-md border-l border-l-[hsl(var(--border))]`}
      >
        <ChevronDown className="h-4 w-4" />
      </button>
    </div>
  );
}
