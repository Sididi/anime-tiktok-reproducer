import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui";

interface DuplicateTikTokWarningProps {
  open: boolean;
  videoId: string;
  registeredAt: string | null;
  onCancel: () => void;
  onContinue: () => void;
}

export function DuplicateTikTokWarning({
  open,
  videoId,
  registeredAt,
  onCancel,
  onContinue,
}: DuplicateTikTokWarningProps) {
  const formattedDate = registeredAt
    ? new Date(registeredAt).toLocaleDateString("fr-FR", {
        day: "numeric",
        month: "long",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

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
            style={{ maxWidth: "28rem", width: "100%" }}
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
                <h3 className="text-lg font-semibold">TikTok deja reproduit</h3>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mt-0.5">
                  Ce TikTok a deja ete utilise dans un projet
                </p>
              </div>
            </div>

            {/* Details */}
            <div className="bg-[hsl(var(--muted))] rounded-lg p-4 space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-[hsl(var(--muted-foreground))]">
                  Video ID
                </span>
                <span className="font-mono font-medium">{videoId}</span>
              </div>
              {formattedDate && (
                <div className="flex justify-between text-sm">
                  <span className="text-[hsl(var(--muted-foreground))]">
                    Utilise le
                  </span>
                  <span className="font-medium">{formattedDate}</span>
                </div>
              )}
            </div>

            {/* Actions */}
            <div className="flex flex-col gap-2">
              <Button
                onClick={onCancel}
                className="w-full active:scale-[0.98] transition-transform"
              >
                Annuler
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onContinue}
                className="w-full text-[hsl(var(--muted-foreground))]"
              >
                Continuer quand meme
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
