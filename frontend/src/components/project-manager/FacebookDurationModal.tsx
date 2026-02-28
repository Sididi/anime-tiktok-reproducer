import { useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Scissors, Zap, X, Ban } from "lucide-react";
import { Button } from "@/components/ui";
import { api } from "@/api/client";

type FacebookStrategy = "cut" | "sped_up" | "skip";

interface FacebookDurationModalProps {
  open: boolean;
  projectId: string;
  durationSeconds: number;
  speedFactor: number;
  spedUpAvailable: boolean;
  onChoice: (strategy: FacebookStrategy) => void;
  onClose: () => void;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function FacebookDurationModal({
  open,
  projectId,
  durationSeconds,
  speedFactor,
  spedUpAvailable,
  onChoice,
  onClose,
}: FacebookDurationModalProps) {
  const cutVideoRef = useRef<HTMLVideoElement>(null);
  const spedUpVideoRef = useRef<HTMLVideoElement>(null);
  const maxDuration = 90;

  // Pause cut preview at 1:30
  const handleCutTimeUpdate = useCallback(() => {
    const video = cutVideoRef.current;
    if (video && video.currentTime >= maxDuration) {
      video.pause();
      video.currentTime = maxDuration;
    }
  }, []);

  // Escape key closes the modal
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [open, onClose]);

  const originalUrl = api.getFacebookPreviewUrl(projectId, "original");
  const spedUpUrl = api.getFacebookPreviewUrl(projectId, "sped_up");

  const accelPercent = ((speedFactor - 1) * 100).toFixed(0);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
            style={{ maxWidth: spedUpAvailable ? "56rem" : "30rem", width: "100%" }}
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-lg font-semibold">Vidéo trop longue pour Facebook</h3>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mt-1">
                  Durée originale : <strong>{formatDuration(durationSeconds)}</strong> — Facebook limite les Reels à <strong>1:30</strong>.
                </p>
              </div>
              <Button variant="ghost" size="icon" onClick={onClose} className="shrink-0">
                <X className="h-4 w-4" />
              </Button>
            </div>

            {/* Previews */}
            <div className={`grid gap-4 ${spedUpAvailable ? "grid-cols-2" : "grid-cols-1"}`}>
              {/* Cut preview */}
              <div className="flex flex-col gap-3">
                <div className="relative bg-black rounded-lg overflow-hidden aspect-9/16 max-h-[55vh]">
                  <video
                    ref={cutVideoRef}
                    src={originalUrl}
                    className="w-full h-full object-contain"
                    controls
                    preload="metadata"
                    onTimeUpdate={handleCutTimeUpdate}
                  />
                  {/* Badge */}
                  <div className="absolute top-2 left-2 bg-black/70 text-white text-xs font-medium px-2 py-1 rounded-md flex items-center gap-1.5">
                    <Scissors className="h-3 w-3" />
                    Coupée à 1:30
                  </div>
                </div>
                <div className="text-center">
                  <p className="text-sm text-[hsl(var(--muted-foreground))]">
                    Les {formatDuration(durationSeconds - maxDuration)} restantes seront supprimées
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

              {/* Sped up preview */}
              {spedUpAvailable && (
                <div className="flex flex-col gap-3">
                    <div className="relative bg-black rounded-lg overflow-hidden aspect-9/16 max-h-[55vh]">
                    <video
                      ref={spedUpVideoRef}
                      src={spedUpUrl}
                      className="w-full h-full object-contain"
                      controls
                      preload="metadata"
                    />
                    {/* Badge */}
                    <div className="absolute top-2 left-2 bg-black/70 text-white text-xs font-medium px-2 py-1 rounded-md flex items-center gap-1.5">
                      <Zap className="h-3 w-3" />
                      Accélérée x{speedFactor.toFixed(2)} (+{accelPercent}%)
                    </div>
                  </div>
                  <div className="text-center">
                    <p className="text-sm text-[hsl(var(--muted-foreground))]">
                      Toute la vidéo est conservée, accélérée de +{accelPercent}%
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
            </div>

            {/* Skip button */}
            <div className="flex justify-center pt-1">
              <Button
                variant="outline"
                size="sm"
                onClick={() => onChoice("skip")}
                className="active:scale-95 transition-transform text-[hsl(var(--muted-foreground))]"
              >
                <Ban className="h-4 w-4 mr-1.5" />
                Ne pas uploader sur Facebook
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
