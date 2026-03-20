import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui";

interface ConflictDetails {
  new_episodes: string[];
  removed_episodes: string[];
  existing_episode_count: number;
  existing_torrent_count: number;
}

interface BatchConflictModalProps {
  open: boolean;
  sourceName: string;
  conflictDetails: ConflictDetails;
  onAccept: () => void;
  onSkip: () => void;
}

export function BatchConflictModal({
  open,
  sourceName,
  conflictDetails,
  onAccept,
  onSkip,
}: BatchConflictModalProps) {
  const { new_episodes, removed_episodes, existing_episode_count, existing_torrent_count } =
    conflictDetails;

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
            style={{ maxWidth: "32rem", width: "100%" }}
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
                <h3 className="text-base font-semibold">Source deja indexee</h3>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mt-0.5">
                  <strong>{sourceName}</strong> existe deja avec des differences
                </p>
              </div>
            </div>

            {/* Summary */}
            <div className="bg-[hsl(var(--muted))] rounded-lg p-4 space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-[hsl(var(--muted-foreground))]">
                  Episodes indexes
                </span>
                <span className="font-medium">{existing_episode_count}</span>
              </div>
              {existing_torrent_count > 0 && (
                <div className="flex justify-between text-sm">
                  <span className="text-[hsl(var(--muted-foreground))]">
                    Torrents lies
                  </span>
                  <span className="font-medium">{existing_torrent_count}</span>
                </div>
              )}
            </div>

            {/* Diff details */}
            <div className="space-y-3 max-h-48 overflow-y-auto">
              {new_episodes.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-green-400 mb-1">
                    + {new_episodes.length} nouveaux episode{new_episodes.length > 1 ? "s" : ""}
                  </p>
                  <div className="bg-[hsl(var(--muted))] rounded-lg p-2 space-y-0.5">
                    {new_episodes.map((ep) => (
                      <p
                        key={ep}
                        className="text-xs font-mono text-[hsl(var(--muted-foreground))] truncate"
                      >
                        {ep}
                      </p>
                    ))}
                  </div>
                </div>
              )}
              {removed_episodes.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-red-400 mb-1">
                    - {removed_episodes.length} episode{removed_episodes.length > 1 ? "s" : ""} absent{removed_episodes.length > 1 ? "s" : ""}
                  </p>
                  <div className="bg-[hsl(var(--muted))] rounded-lg p-2 space-y-0.5">
                    {removed_episodes.map((ep) => (
                      <p
                        key={ep}
                        className="text-xs font-mono text-[hsl(var(--muted-foreground))] truncate"
                      >
                        {ep}
                      </p>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Actions */}
            <div className="flex flex-col gap-2">
              <Button onClick={onAccept} className="w-full active:scale-[0.98] transition-transform">
                <RefreshCw className="h-3.5 w-3.5 mr-2" />
                Mettre a jour
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onSkip}
                className="w-full text-[hsl(var(--muted-foreground))]"
              >
                <X className="h-3.5 w-3.5 mr-2" />
                Ignorer
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
