import { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui";

interface CopyrightWarningModalProps {
  open: boolean;
  musicDisplayName: string;
  onContinueWithOriginal: () => void;
  onCancel: () => void;
}

export function CopyrightWarningModal({
  open,
  musicDisplayName,
  onContinueWithOriginal,
  onCancel,
}: CopyrightWarningModalProps) {
  // Escape key closes the modal
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [open, onCancel]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onCancel}
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
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <AlertTriangle className="h-6 w-6 text-[hsl(var(--destructive))] shrink-0" />
                <h3 className="text-lg font-semibold">
                  Musique sous copyright
                </h3>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={onCancel}
                className="shrink-0"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>

            {/* Body */}
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              La musique &ldquo;{musicDisplayName}&rdquo; est sous copyright. Le
              fichier <strong>output_no_music.wav</strong> est absent du dossier
              Google Drive du projet. Le remplacement automatique de la musique
              n&apos;est pas possible.
            </p>

            {/* Actions */}
            <div className="flex flex-col gap-2">
              <Button
                variant="outline"
                onClick={onContinueWithOriginal}
                className="w-full active:scale-95 transition-transform"
              >
                Continuer avec l&apos;audio original
              </Button>
              <Button
                variant="ghost"
                onClick={onCancel}
                className="w-full active:scale-95 transition-transform"
              >
                Annuler l&apos;upload
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
