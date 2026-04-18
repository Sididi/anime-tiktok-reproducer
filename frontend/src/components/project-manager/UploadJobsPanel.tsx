import { useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ChevronDown,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
} from "lucide-react";

import type { ProjectManagerRow, ProjectUploadJob } from "@/types";

const UPLOAD_JOBS_EXPANDED_STORAGE_KEY = "project-manager.upload-jobs-expanded";

const SESSION_START_MS = Date.now();

function readStoredExpandedState(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  try {
    const stored = window.sessionStorage.getItem(UPLOAD_JOBS_EXPANDED_STORAGE_KEY);
    return stored == null ? false : stored === "true";
  } catch {
    return false;
  }
}

interface UploadJobsPanelProps {
  jobs: ProjectUploadJob[];
  rowsByProjectId: Record<string, ProjectManagerRow>;
}

export function UploadJobsPanel({
  jobs,
  rowsByProjectId,
}: UploadJobsPanelProps) {
  const [expanded, setExpanded] = useState(readStoredExpandedState);

  const sortedJobs = useMemo(
    () =>
      [...jobs]
        .filter((job) => {
          if (job.status === "queued" || job.status === "running") return true;
          const updatedMs = new Date(job.updated_at).getTime();
          return Number.isFinite(updatedMs) && updatedMs >= SESSION_START_MS;
        })
        .sort((a, b) =>
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
    <div className="mx-6 mt-4 bg-[hsl(var(--card))] rounded-lg overflow-hidden border border-[hsl(var(--border))]">
      <button
        onClick={() =>
          setExpanded((value) => {
            const nextValue = !value;
            try {
              window.sessionStorage.setItem(
                UPLOAD_JOBS_EXPANDED_STORAGE_KEY,
                nextValue ? "true" : "false",
              );
            } catch {
              // Ignore storage failures and keep in-memory toggle behavior.
            }
            return nextValue;
          })
        }
        className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--secondary))]/50 transition-colors"
      >
        <div
          className={`w-2 h-2 rounded-full ${
            activeJobs.length > 0 ? "bg-blue-500 animate-pulse" : "bg-emerald-500"
          }`}
        />
        <span className="text-[hsl(var(--muted-foreground))]">
          {activeJobs.length} upload{activeJobs.length !== 1 ? "s" : ""} en cours
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
                {sortedJobs.map((job) => {
                  const row = rowsByProjectId[job.project_id];
                  const title = row?.anime_title || job.project_id;
                  return (
                    <motion.div
                      key={job.project_id}
                      initial={{ opacity: 0, y: -8 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, scale: 0.95 }}
                      transition={{ duration: 0.2 }}
                      className="flex flex-col gap-2 bg-[hsl(var(--background))] rounded px-3 py-2"
                    >
                      <div className="flex items-center gap-2">
                        <UploadStatusIcon status={job.status} />
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-medium truncate">{title}</div>
                          <div className="text-[11px] text-[hsl(var(--muted-foreground))] font-mono truncate">
                            {job.project_id}
                          </div>
                        </div>
                        <span className="text-xs text-[hsl(var(--muted-foreground))] shrink-0">
                          {uploadStatusLabel(job)}
                        </span>
                      </div>

                      {(job.message || job.error) && (
                        <div className="pl-6 text-[11px] text-[hsl(var(--muted-foreground))]">
                          {job.error || job.message}
                        </div>
                      )}
                    </motion.div>
                  );
                })}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function UploadStatusIcon({
  status,
}: {
  status: ProjectUploadJob["status"];
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

function uploadStatusLabel(job: ProjectUploadJob): string {
  if (job.status === "queued") {
    return "En attente";
  }
  if (job.status === "running") {
    if (job.phase === "prepare") return "Préparation";
    if (job.phase === "scheduled") return "Planification";
    if (job.phase === "download") return "Téléchargement";
    if (job.phase === "platform_upload") return "Upload";
    if (job.phase === "finalize") return "Finalisation";
    return "En cours";
  }
  if (job.status === "complete") {
    return "Terminé";
  }
  return "Erreur";
}
