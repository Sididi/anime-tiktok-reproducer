import { useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ChevronDown,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  ExternalLink,
  RefreshCw,
} from "lucide-react";

import type { ProjectStartupJob } from "@/types";

interface StartupJobsPanelProps {
  jobs: ProjectStartupJob[];
  onOpen: (job: ProjectStartupJob) => void;
  onRetry: (job: ProjectStartupJob) => void;
}

export function StartupJobsPanel({
  jobs,
  onOpen,
  onRetry,
}: StartupJobsPanelProps) {
  const [expanded, setExpanded] = useState(true);

  const sortedJobs = useMemo(
    () =>
      [...jobs].sort((a, b) =>
        String(b.updated_at).localeCompare(String(a.updated_at)),
      ),
    [jobs],
  );
  const activeJobs = sortedJobs.filter(
    (job) => job.status === "queued" || job.status === "running",
  );

  if (sortedJobs.length === 0) {
    return null;
  }

  return (
    <div className="bg-[hsl(var(--card))] rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded((value) => !value)}
        className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--secondary))]/50 transition-colors"
      >
        <div
          className={`w-2 h-2 rounded-full ${
            activeJobs.length > 0 ? "bg-blue-500 animate-pulse" : "bg-emerald-500"
          }`}
        />
        <span className="text-[hsl(var(--muted-foreground))]">
          {activeJobs.length} startup{activeJobs.length !== 1 ? "s" : ""} en cours
        </span>
        <ChevronDown
          className={`ml-auto h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] transition-transform ${
            expanded ? "" : "-rotate-90"
          }`}
        />
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="flex flex-col gap-1 px-3 pb-2">
              <AnimatePresence mode="popLayout">
                {sortedJobs.map((job) => (
                  <motion.div
                    key={job.project_id}
                    initial={{ opacity: 0, y: -8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ duration: 0.2 }}
                    className="flex flex-col gap-2 bg-[hsl(var(--background))] rounded px-3 py-2"
                  >
                    <div className="flex items-center gap-2">
                      <StartupStatusIcon status={job.status} />
                      <span className="text-sm font-medium truncate min-w-0 flex-1">
                        {job.anime_name || job.project_id}
                      </span>
                      <span className="text-xs text-[hsl(var(--muted-foreground))] shrink-0">
                        {startupStatusLabel(job)}
                      </span>
                      <span className="text-xs text-[hsl(var(--muted-foreground))] w-9 text-right shrink-0">
                        {job.status === "error"
                          ? ""
                          : `${Math.round(job.progress * 100)}%`}
                      </span>
                    </div>

                    {(job.status === "queued" || job.status === "running") && (
                      <div className="pl-6">
                        <div className="h-1 bg-[hsl(var(--secondary))] rounded-full min-w-[60px]">
                          <div
                            className="h-full bg-blue-500 rounded-full transition-all duration-300"
                            style={{ width: `${Math.round(job.progress * 100)}%` }}
                          />
                        </div>
                      </div>
                    )}

                    {(job.message || job.error) && (
                      <div className="pl-6 text-[11px] text-[hsl(var(--muted-foreground))]">
                        {job.error || job.message}
                      </div>
                    )}

                    {(job.status === "error" || job.status === "complete") && (
                      <div className="pl-6 flex items-center gap-2">
                        {job.ready_url && (
                          <button
                            onClick={() => onOpen(job)}
                            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs bg-[hsl(var(--secondary))] hover:bg-[hsl(var(--secondary))]/80 transition-colors"
                          >
                            <ExternalLink className="h-3 w-3" />
                            Ouvrir
                          </button>
                        )}
                        {job.status === "error" && (
                          <button
                            onClick={() => onRetry(job)}
                            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs bg-[hsl(var(--secondary))] hover:bg-[hsl(var(--secondary))]/80 transition-colors"
                          >
                            <RefreshCw className="h-3 w-3" />
                            Relancer
                          </button>
                        )}
                      </div>
                    )}
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function StartupStatusIcon({
  status,
}: {
  status: ProjectStartupJob["status"];
}) {
  switch (status) {
    case "queued":
      return <Clock className="h-4 w-4 text-amber-400 shrink-0" />;
    case "running":
      return <Loader2 className="h-4 w-4 text-blue-500 animate-spin shrink-0" />;
    case "complete":
      return <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />;
    case "error":
      return <XCircle className="h-4 w-4 text-red-500 shrink-0" />;
  }
}

function startupStatusLabel(job: ProjectStartupJob): string {
  if (job.status === "queued") {
    return "En attente";
  }
  if (job.status === "running") {
    if (job.phase === "download") return "Téléchargement";
    if (job.phase === "scene_detection") return "Découpage";
    if (job.phase === "activation") return "Activation";
    return "Démarrage";
  }
  if (job.status === "complete") {
    return "Terminé";
  }
  return "Erreur";
}
