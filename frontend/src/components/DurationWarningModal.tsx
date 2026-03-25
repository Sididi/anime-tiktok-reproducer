import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui";

interface DurationWarningModalProps {
  open: boolean;
  audioSeconds: number;
  emptyScenesSeconds: number;
  totalSeconds: number;
  onGoBack: () => void;
  onContinue: () => void;
}

const TIKTOK_MIN_SECONDS = 61;

export function DurationWarningModal({
  open,
  audioSeconds,
  emptyScenesSeconds,
  totalSeconds,
  onGoBack,
  onContinue,
}: DurationWarningModalProps) {
  const deficit = TIKTOK_MIN_SECONDS - totalSeconds;
  const hasEmptyScenes = emptyScenesSeconds > 0;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          <motion.div
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
            style={{ maxWidth: "30rem", width: "100%" }}
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-full bg-amber-500/15 flex items-center justify-center shrink-0">
                <AlertTriangle className="h-5 w-5 text-amber-500" />
              </div>
              <div>
                <h3 className="text-lg font-semibold">Durée insuffisante</h3>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mt-0.5">
                  TikTok requiert au moins 1min01 pour la monétisation
                </p>
              </div>
            </div>

            {/* Duration breakdown */}
            <div className="bg-[hsl(var(--muted))] rounded-lg p-4 space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-[hsl(var(--muted-foreground))]">
                  Audio TTS (après auto-editor)
                </span>
                <span className="font-medium">{audioSeconds.toFixed(1)}s</span>
              </div>
              {hasEmptyScenes && (
                <div className="flex justify-between text-sm">
                  <span className="text-[hsl(var(--muted-foreground))]">
                    Empty scenes
                  </span>
                  <span className="font-medium">
                    +{emptyScenesSeconds.toFixed(1)}s
                  </span>
                </div>
              )}
              <div className="border-t border-[hsl(var(--border))] pt-2 flex justify-between text-sm font-semibold">
                <span>Durée totale estimée</span>
                <span className="text-amber-500">
                  {totalSeconds.toFixed(1)}s
                </span>
              </div>
              <p className="text-xs text-[hsl(var(--muted-foreground))]">
                Il manque <strong>{deficit.toFixed(1)}s</strong> pour atteindre
                1min01.
              </p>
            </div>

            {/* Actions */}
            <div className="flex flex-col gap-2">
              <Button
                onClick={onGoBack}
                className="w-full active:scale-[0.98] transition-transform"
              >
                <ArrowLeft className="h-4 w-4 mr-2" />
                Retourner au Script
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onContinue}
                className="w-full text-[hsl(var(--muted-foreground))]"
              >
                Continuer quand même
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
