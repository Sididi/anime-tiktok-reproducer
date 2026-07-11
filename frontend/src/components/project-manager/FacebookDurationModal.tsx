import { useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Scissors, Zap, X, Ban, Loader2 } from "lucide-react";
import { Button } from "@/components/ui";
import { useUploadSourcePreview } from "@/hooks/useUploadSourcePreview";
import type { UploadDurationStrategy } from "@/types";

interface FacebookDurationModalProps {
  open: boolean;
  projectId: string;
  projectTitle?: string | null;
  durationSeconds: number;
  speedFactor: number;
  spedUpAvailable: boolean;
  platform?: "Facebook" | "Instagram";
  maxDurationSeconds?: number;
  recommendationOnly?: boolean;
  onChoice: (strategy: UploadDurationStrategy) => void;
  onClose: () => void;
  stacked?: boolean;
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (h > 0) {
    return `${h}:${(m % 60).toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function FacebookDurationModal({
  open,
  projectId,
  projectTitle,
  durationSeconds,
  speedFactor,
  spedUpAvailable,
  platform = "Facebook",
  maxDurationSeconds = 90,
  recommendationOnly = false,
  onChoice,
  onClose,
  stacked = false,
}: FacebookDurationModalProps) {
  const cutVideoRef = useRef<HTMLVideoElement>(null);
  const spedUpVideoRef = useRef<HTMLVideoElement>(null);
  const maxDuration = maxDurationSeconds;
  const preview = useUploadSourcePreview(projectId, open);

  const handleCutTimeUpdate = useCallback(() => {
    const video = cutVideoRef.current;
    if (video && video.currentTime >= maxDuration) {
      video.pause();
      video.currentTime = maxDuration;
    }
  }, [maxDuration]);

  const handleSpedUpLoadedMetadata = useCallback(() => {
    const video = spedUpVideoRef.current;
    if (video) {
      video.playbackRate = speedFactor;
    }
  }, [speedFactor]);

  useEffect(() => {
    if (!open || stacked) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [open, onClose, stacked]);

  if (!open) {
    return null;
  }

  const accelPercent = ((speedFactor - 1) * 100).toFixed(0);

  const previewPlaceholder = (
    <div className="w-full h-full flex flex-col items-center justify-center gap-2 text-white/70">
      {preview.status === "loading" ? (
        <>
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="text-xs">Préparation de l'aperçu...</span>
        </>
      ) : (
        <span className="text-xs">Aperçu indisponible</span>
      )}
    </div>
  );

  const card = (
    <motion.div
      className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
      style={{
        maxWidth: spedUpAvailable ? "56rem" : "30rem",
        width: "100%",
      }}
      initial={{ scale: 0.95, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      exit={{ scale: 0.95, opacity: 0 }}
      transition={{ duration: 0.2 }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">
            {recommendationOnly
              ? `Portée Instagram réduite au-delà de 3:00`
              : `Vidéo trop longue pour ${platform}`}
          </h3>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1 font-mono">
            {projectTitle || "Projet"} · {projectId}
          </p>
          <p className="text-sm text-[hsl(var(--muted-foreground))] mt-2">
            Durée originale :{" "}
            <strong>{formatDuration(durationSeconds)}</strong> — {recommendationOnly ? (
              <>Instagram accepte cette durée, mais les Reels de plus de <strong>3:00</strong> peuvent ne pas être recommandés aux nouvelles audiences.</>
            ) : (
              <>{platform} limite ce compte à <strong>{formatDuration(maxDuration)}</strong>.</>
            )}
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          className="shrink-0"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {!recommendationOnly && <div
        className={`grid gap-4 ${spedUpAvailable ? "grid-cols-2" : "grid-cols-1"}`}
      >
        <div className="flex flex-col gap-3">
          <div className="relative bg-black rounded-lg overflow-hidden aspect-9/16 max-h-[55vh]">
            {preview.status === "ready" ? (
              <video
                ref={cutVideoRef}
                src={preview.url}
                className="w-full h-full object-contain"
                controls
                preload="metadata"
                onTimeUpdate={handleCutTimeUpdate}
              />
            ) : (
              previewPlaceholder
            )}
            <div className="absolute top-2 left-2 bg-black/70 text-white text-xs font-medium px-2 py-1 rounded-md flex items-center gap-1.5">
              <Scissors className="h-3 w-3" />
              Coupée à 1:30
            </div>
          </div>
          <div className="text-center">
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Les {formatDuration(durationSeconds - maxDuration)} restantes
              seront supprimées
            </p>
            <Button
              size="sm"
              className="mt-2 w-full active:scale-95 transition-transform"
              onClick={() => onChoice("cut")}
            >
              <Scissors className="h-4 w-4 mr-1.5" />
              Couper à 1:30
            </Button>
          </div>
        </div>

        {spedUpAvailable && (
          <div className="flex flex-col gap-3">
            <div className="relative bg-black rounded-lg overflow-hidden aspect-9/16 max-h-[55vh]">
              {preview.status === "ready" ? (
                <video
                  ref={spedUpVideoRef}
                  src={preview.url}
                  className="w-full h-full object-contain"
                  controls
                  preload="metadata"
                  onLoadedMetadata={handleSpedUpLoadedMetadata}
                />
              ) : (
                previewPlaceholder
              )}
              <div className="absolute top-2 left-2 bg-black/70 text-white text-xs font-medium px-2 py-1 rounded-md flex items-center gap-1.5">
                <Zap className="h-3 w-3" />
                Accélérée x{speedFactor.toFixed(2)} (+{accelPercent}%)
              </div>
            </div>
            <div className="text-center">
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Toute la vidéo est conservée, accélérée de +{accelPercent}
                %
              </p>
              <Button
                size="sm"
                className="mt-2 w-full active:scale-95 transition-transform"
                onClick={() => onChoice("sped_up")}
              >
                <Zap className="h-4 w-4 mr-1.5" />
                Accélérer x{speedFactor.toFixed(2)}
              </Button>
            </div>
          </div>
        )}
      </div>}

      <div className="flex justify-center pt-1">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onChoice(recommendationOnly ? "auto" : "skip")}
          className="active:scale-95 transition-transform text-[hsl(var(--muted-foreground))]"
        >
          <Ban className="h-4 w-4 mr-1.5" />
          {recommendationOnly ? "Continuer sur Instagram" : `Ne pas uploader sur ${platform}`}
        </Button>
        {recommendationOnly && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onChoice("skip")}
          >
            <Ban className="h-4 w-4 mr-1.5" />
            Ne pas uploader sur Instagram
          </Button>
        )}
      </div>
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
        onClick={onClose}
      >
        {card}
      </motion.div>
    </AnimatePresence>
  );
}
