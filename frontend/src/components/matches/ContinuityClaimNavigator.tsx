import { useEffect, useMemo, useState } from "react";
import { ChevronUp, ChevronDown } from "lucide-react";

interface ContinuityClaimNavigatorProps {
  /** Claimed scene indices, ordered by scene position. */
  claimedSceneIndices: number[];
  /** Scene index -> position in the rendered scene list. */
  scenePositionByIndex: Map<number, number>;
  /** Scene index closest to the viewport centre, recomputed on scroll. */
  getAnchorSceneIndex: () => number | null;
  onJump: (sceneIndex: number) => void;
}

/**
 * Always-reachable twin of the arrows pinned to each claimed card: a small
 * rail floating in the left gutter that jumps to the previous/next dubious
 * scene relative to where the page is currently scrolled — so the jumps stay
 * available even when no claimed card is on screen.
 */
export function ContinuityClaimNavigator({
  claimedSceneIndices,
  scenePositionByIndex,
  getAnchorSceneIndex,
  onJump,
}: ContinuityClaimNavigatorProps) {
  const [anchorPosition, setAnchorPosition] = useState<number | null>(null);

  const claims = useMemo(() => {
    return claimedSceneIndices
      .map((sceneIndex) => ({
        sceneIndex,
        position: scenePositionByIndex.get(sceneIndex) ?? -1,
      }))
      .filter((claim) => claim.position >= 0)
      .sort((a, b) => a.position - b.position);
  }, [claimedSceneIndices, scenePositionByIndex]);

  useEffect(() => {
    if (claims.length === 0) return;

    let rafId: number | null = null;

    const sync = () => {
      rafId = null;
      const anchorSceneIndex = getAnchorSceneIndex();
      const position =
        anchorSceneIndex === null
          ? null
          : (scenePositionByIndex.get(anchorSceneIndex) ?? null);
      setAnchorPosition((previous) =>
        previous === position ? previous : position,
      );
    };

    const requestSync = () => {
      if (rafId !== null) return;
      rafId = window.requestAnimationFrame(sync);
    };

    requestSync();
    window.addEventListener("scroll", requestSync, { passive: true });
    window.addEventListener("resize", requestSync);

    return () => {
      if (rafId !== null) window.cancelAnimationFrame(rafId);
      window.removeEventListener("scroll", requestSync);
      window.removeEventListener("resize", requestSync);
    };
  }, [claims.length, getAnchorSceneIndex, scenePositionByIndex]);

  if (claims.length === 0) return null;

  // Strict comparisons: standing on a claim steps to its neighbours, and an
  // unknown anchor still offers the whole list forward.
  const previousClaim =
    anchorPosition === null
      ? null
      : ([...claims]
          .reverse()
          .find((claim) => claim.position < anchorPosition) ?? null);
  const nextClaim =
    anchorPosition === null
      ? claims[0]
      : (claims.find((claim) => claim.position > anchorPosition) ?? null);

  const buttonClass =
    "h-9 w-9 rounded-md border border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] flex items-center justify-center transition-colors enabled:hover:bg-[hsl(var(--secondary))] enabled:hover:text-[hsl(var(--foreground))] disabled:opacity-40 disabled:cursor-not-allowed";

  // Hugs the left edge of the max-w-4xl content column on wide screens,
  // clamped so it never leaves the viewport on narrow ones.
  return (
    <div
      data-continuity-claim-navigator
      className="fixed left-[max(0.5rem,calc(50%-32rem))] top-1/2 z-[70] flex -translate-y-1/2 flex-col items-center gap-1 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-1 shadow-lg"
    >
      <button
        type="button"
        aria-label="Previous claimed scene"
        title="Previous claimed scene (episode change / non-continuous)"
        disabled={!previousClaim}
        onClick={() => {
          if (previousClaim) onJump(previousClaim.sceneIndex);
        }}
        className={buttonClass}
      >
        <ChevronUp className="h-4 w-4" />
      </button>
      <button
        type="button"
        aria-label="Next claimed scene"
        title="Next claimed scene (episode change / non-continuous)"
        disabled={!nextClaim}
        onClick={() => {
          if (nextClaim) onJump(nextClaim.sceneIndex);
        }}
        className={buttonClass}
      >
        <ChevronDown className="h-4 w-4" />
      </button>
    </div>
  );
}
