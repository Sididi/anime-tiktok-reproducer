import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, FolderOpen, Search, X } from "lucide-react";
import { Button } from "@/components/ui";

interface BatchFolderFixModalProps {
  open: boolean;
  folderName: string;
  originalPath: string;
  suggestedPath: string | null;
  onAcceptSuggestion: (path: string) => void;
  onBrowseManually: () => void;
  onSkip: () => void;
}

export function BatchFolderFixModal({
  open,
  folderName,
  originalPath,
  suggestedPath,
  onAcceptSuggestion,
  onBrowseManually,
  onSkip,
}: BatchFolderFixModalProps) {
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
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-4"
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
                <h3 className="text-base font-semibold">Aucune video trouvee</h3>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mt-0.5">
                  Le dossier <strong>{folderName}</strong> ne contient pas de
                  videos directement
                </p>
              </div>
            </div>

            {/* Original path */}
            <div className="bg-[hsl(var(--muted))] rounded-lg px-3 py-2 text-xs font-mono text-[hsl(var(--muted-foreground))] truncate">
              {originalPath}
            </div>

            {/* Suggested path */}
            {suggestedPath && (
              <div className="bg-[hsl(var(--muted))] rounded-lg p-3 space-y-2">
                <p className="text-sm text-[hsl(var(--muted-foreground))]">
                  Sous-dossier avec videos trouve :
                </p>
                <p className="text-xs font-mono truncate">{suggestedPath}</p>
                <Button
                  onClick={() => onAcceptSuggestion(suggestedPath)}
                  size="sm"
                  className="w-full"
                >
                  <FolderOpen className="h-3.5 w-3.5 mr-2" />
                  Utiliser ce dossier
                </Button>
              </div>
            )}

            {/* Actions */}
            <div className="flex flex-col gap-2">
              <Button variant="outline" onClick={onBrowseManually} className="w-full">
                <Search className="h-3.5 w-3.5 mr-2" />
                Parcourir manuellement
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onSkip}
                className="w-full text-[hsl(var(--muted-foreground))]"
              >
                <X className="h-3.5 w-3.5 mr-2" />
                Ignorer ce dossier
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
