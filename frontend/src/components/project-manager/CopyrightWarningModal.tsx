import { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui";

interface CopyrightWarningModalProps {
  open: boolean;
  musicDisplayName: string;
  projectTitle?: string | null;
  projectId?: string | null;
  onContinueWithOriginal: () => void;
  onCancel: () => void;
  stacked?: boolean;
}

export function CopyrightWarningModal({
  open,
  musicDisplayName,
  projectTitle,
  projectId,
  onContinueWithOriginal,
  onCancel,
  stacked = false,
}: CopyrightWarningModalProps) {
  useEffect(() => {
    if (!open || stacked) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [open, onCancel, stacked]);

  if (!open) {
    return null;
  }

  const card = (
    <motion.div
      className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
      style={{ maxWidth: "28rem", width: "100%" }}
      initial={{ scale: 0.95, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      exit={{ scale: 0.95, opacity: 0 }}
      transition={{ duration: 0.2 }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <AlertTriangle className="h-6 w-6 text-[hsl(var(--destructive))] shrink-0" />
          <div>
            <h3 className="text-lg font-semibold">
              Musique sous copyright
            </h3>
            {projectId && (
              <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1 font-mono">
                {projectTitle || "Projet"} · {projectId}
              </p>
            )}
          </div>
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

      <p className="text-sm text-[hsl(var(--muted-foreground))]">
        La musique &ldquo;{musicDisplayName}&rdquo; est sous copyright. Le
        fichier <strong>output_no_music.wav</strong> est absent du dossier
        Google Drive du projet. Le remplacement automatique de la musique
        n&apos;est pas possible.
      </p>

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
  );

  if (stacked) {
    return <div className="w-full max-w-md">{card}</div>;
  }

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onCancel}
      >
        {card}
      </motion.div>
    </AnimatePresence>
  );
}
