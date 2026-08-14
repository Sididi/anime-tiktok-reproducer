import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Check, Image as ImageIcon, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui";
import { useThumbnailCandidates } from "@/hooks/useThumbnailCandidates";
import type { ThumbnailCandidate } from "@/types";

interface ThumbnailSelectionModalProps {
  open: boolean;
  projectId: string;
  projectTitle?: string | null;
  /** Resolves the step: a candidate timestamp (ms) + its index, or (null, null) for "no thumbnail". */
  onChoice: (timestampMs: number | null, candidateIndex: number | null) => void;
  stacked?: boolean;
}

export function ThumbnailSelectionModal({
  open,
  projectId,
  projectTitle,
  onChoice,
  stacked = false,
}: ThumbnailSelectionModalProps) {
  const { status, candidates, detail } = useThumbnailCandidates(
    projectId,
    open,
    projectTitle,
  );
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);

  const isReady = (c: ThumbnailCandidate) => c.source !== "pending";

  // Derived selection: the user's pick if that tile is ready, else candidate
  // 0 if ready, else the lowest-index ready tile. Never a pending tile.
  const userPick =
    selectedIndex !== null
      ? candidates.find((c) => c.index === selectedIndex)
      : undefined;
  const candidateZero = candidates.find((c) => c.index === 0);
  const lowestReady = [...candidates]
    .filter(isReady)
    .sort((a, b) => a.index - b.index)[0];
  const selected: ThumbnailCandidate | undefined =
    userPick && isReady(userPick)
      ? userPick
      : candidateZero && isReady(candidateZero)
        ? candidateZero
        : lowestReady;

  // Skip/close always falls back to the derived selection: the upload never
  // blocks on this step (approved design decision).
  const resolveWithDefault = () =>
    onChoice(selected?.timestamp_ms ?? null, selected?.index ?? null);

  useEffect(() => {
    if (!open || stacked) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        resolveWithDefault();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  });

  if (!open) {
    return null;
  }

  const card = (
    <motion.div
      className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
      style={{ maxWidth: "64rem", width: "100%" }}
      initial={{ scale: 0.95, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      exit={{ scale: 0.95, opacity: 0 }}
      transition={{ duration: 0.2 }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">Choisir la miniature</h3>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1 font-mono">
            {projectTitle || "Projet"} · {projectId}
          </p>
          <p className="text-sm text-[hsl(var(--muted-foreground))] mt-2">
            Appliquée sur TikTok, Instagram, YouTube et Facebook (selon support).
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={resolveWithDefault}
          className="shrink-0"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {status === "loading" && (
        <div className="flex flex-col items-center justify-center gap-2 py-16 text-[hsl(var(--muted-foreground))]">
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="text-xs">Extraction des miniatures...</span>
        </div>
      )}

      {status === "error" && (
        <div className="flex flex-col items-center justify-center gap-3 py-12 text-[hsl(var(--muted-foreground))]">
          <ImageIcon className="h-6 w-6" />
          <span className="text-sm">
            Miniatures indisponibles{detail ? ` : ${detail}` : ""}
          </span>
          <Button size="sm" onClick={() => onChoice(null, null)}>
            Continuer sans miniature
          </Button>
        </div>
      )}

      {(status === "ready" || status === "partial") && (
        <>
          <div
            className={`grid gap-3 ${
              candidates.length > 6
                ? "grid-cols-6"
                : candidates.length >= 5
                  ? "grid-cols-5"
                  : "grid-cols-3"
            }`}
          >
            {candidates.map((candidate) => {
              const pending = candidate.source === "pending";
              return (
                <button
                  key={candidate.index}
                  type="button"
                  disabled={pending}
                  onClick={() => setSelectedIndex(candidate.index)}
                  className={`relative rounded-lg overflow-hidden aspect-9/16 bg-black border-2 transition-colors ${
                    pending ? "cursor-default" : ""
                  } ${
                    selected?.index === candidate.index
                      ? "border-[hsl(var(--primary))]"
                      : "border-transparent hover:border-[hsl(var(--border))]"
                  }`}
                >
                  {pending ? (
                    <div className="w-full h-full flex items-center justify-center bg-white/5">
                      <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--muted-foreground))]" />
                    </div>
                  ) : (
                    <img
                      src={candidate.image_url}
                      alt={candidate.label}
                      className="w-full h-full object-cover"
                    />
                  )}
                  {selected?.index === candidate.index && (
                    <div className="absolute top-1.5 right-1.5 bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] rounded-full p-1">
                      <Check className="h-3 w-3" />
                    </div>
                  )}
                  {candidate.source === "output" && (
                    <div className="absolute bottom-1 left-1 bg-amber-500/80 text-black text-[9px] px-1 rounded">
                      aperçu sortie
                    </div>
                  )}
                  <div className="absolute bottom-0 inset-x-0 bg-black/70 text-white text-[10px] px-1.5 py-1 text-center">
                    {candidate.label}
                  </div>
                </button>
              );
            })}
          </div>
          {status === "partial" && (
            <p className="text-xs text-[hsl(var(--muted-foreground))] text-center -mt-2">
              Certaines vignettes arrivent encore…
            </p>
          )}
          <div className="flex items-center justify-center gap-3 pt-1">
            <Button
              size="sm"
              className="active:scale-95 transition-transform"
              disabled={!selected}
              onClick={() => onChoice(selected?.timestamp_ms ?? null, selected?.index ?? null)}
            >
              <Check className="h-4 w-4 mr-1.5" />
              Utiliser cette miniature
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="active:scale-95 transition-transform text-[hsl(var(--muted-foreground))]"
              onClick={() => onChoice(null, null)}
            >
              Continuer sans miniature
            </Button>
          </div>
        </>
      )}
    </motion.div>
  );

  if (stacked) {
    return <div className="w-full max-w-5xl">{card}</div>;
  }

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={resolveWithDefault}
      >
        {card}
      </motion.div>
    </AnimatePresence>
  );
}
