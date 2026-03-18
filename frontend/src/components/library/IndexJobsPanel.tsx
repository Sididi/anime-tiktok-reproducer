import { useState, useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ChevronDown,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  AlertTriangle,
} from "lucide-react";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";
import type { IndexationJob } from "@/types";

interface IndexJobsPanelProps {
  onJobComplete?: () => void;
}

export function IndexJobsPanel({ onJobComplete }: IndexJobsPanelProps) {
  const [jobs, setJobs] = useState<IndexationJob[]>([]);
  const [expanded, setExpanded] = useState(true);
  const abortRef = useRef<AbortController | null>(null);
  const completedTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  const connectSSE = useCallback(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    api.streamIndexationJobs().then((resp) => {
      readSSEStream<IndexationJob>(
        resp,
        (job) => {
          setJobs((prev) => {
            const idx = prev.findIndex((j) => j.id === job.id);
            if (idx >= 0) {
              const next = [...prev];
              next[idx] = job;
              return next;
            }
            return [...prev, job];
          });

          // Auto-remove completed jobs after 5s
          if (job.status === "complete" && !completedTimers.current.has(job.id)) {
            completedTimers.current.set(
              job.id,
              setTimeout(() => {
                setJobs((prev) => prev.filter((j) => j.id !== job.id));
                completedTimers.current.delete(job.id);
              }, 5000),
            );
            onJobComplete?.();
          }
        },
        { signal: controller.signal },
      ).catch(() => {
        // SSE connection closed or aborted — reconnect after delay
        if (!controller.signal.aborted) {
          setTimeout(connectSSE, 3000);
        }
      });
    }).catch(() => {
      // fetch itself failed — retry
      if (!controller.signal.aborted) {
        setTimeout(connectSSE, 3000);
      }
    });
  }, [onJobComplete]);

  useEffect(() => {
    connectSSE();
    return () => {
      abortRef.current?.abort();
      completedTimers.current.forEach((t) => clearTimeout(t));
      completedTimers.current.clear();
    };
  }, [connectSSE]);

  const activeJobs = jobs.filter(
    (j) => j.status === "queued" || j.status === "indexing",
  );

  if (jobs.length === 0) return null;

  return (
    <div className="bg-[hsl(var(--card))] rounded-lg overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--secondary))]/50 transition-colors"
      >
        <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
        <span className="text-[hsl(var(--muted-foreground))]">
          {activeJobs.length} indexation{activeJobs.length !== 1 ? "s" : ""} en
          cours
        </span>
        <ChevronDown
          className={`ml-auto h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] transition-transform ${
            expanded ? "" : "-rotate-90"
          }`}
        />
      </button>

      {/* Expanded job list */}
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
                {jobs.map((job) => (
                  <motion.div
                    key={job.id}
                    initial={{ opacity: 0, y: -8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ duration: 0.2 }}
                    className="flex items-center gap-2 bg-[hsl(var(--background))] rounded px-3 py-2"
                  >
                    <StatusIcon status={job.status} />
                    <span className="text-sm font-medium truncate min-w-0 flex-shrink">
                      {job.source_name}
                    </span>
                    <span className="text-xs text-[hsl(var(--muted-foreground))] shrink-0">
                      {statusLabel(job)}
                    </span>
                    {(job.status === "indexing" || job.status === "queued") && (
                      <div className="flex-1 h-1 bg-[hsl(var(--secondary))] rounded-full min-w-[60px]">
                        <div
                          className="h-full bg-green-500 rounded-full transition-all duration-300"
                          style={{
                            width: `${Math.round(job.progress * 100)}%`,
                          }}
                        />
                      </div>
                    )}
                    <span className="text-xs text-[hsl(var(--muted-foreground))] w-9 text-right shrink-0">
                      {job.status === "complete"
                        ? "100%"
                        : job.status === "error"
                          ? ""
                          : `${Math.round(job.progress * 100)}%`}
                    </span>
                    {job.status === "complete" &&
                      job.unmatched_files?.length > 0 && (
                        <AlertTriangle
                          className="h-3.5 w-3.5 text-amber-500 shrink-0"
                          title={`${job.unmatched_files.length} fichier(s) non lié(s) à un torrent`}
                        />
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

function StatusIcon({ status }: { status: IndexationJob["status"] }) {
  switch (status) {
    case "indexing":
      return (
        <Loader2 className="h-4 w-4 text-green-500 animate-spin shrink-0" />
      );
    case "complete":
      return <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0" />;
    case "error":
      return <XCircle className="h-4 w-4 text-red-500 shrink-0" />;
    case "queued":
      return <Clock className="h-4 w-4 text-amber-400 shrink-0" />;
  }
}

function statusLabel(job: IndexationJob): string {
  switch (job.status) {
    case "queued":
      return "En attente";
    case "indexing":
      return job.message || "Indexation...";
    case "complete": {
      if (job.unmatched_files?.length > 0) {
        return `Terminé — ${job.unmatched_files.length} fichier(s) sans torrent`;
      }
      if (job.linked_torrents > 0) {
        return `Terminé — ${job.linked_torrents} torrent(s) lié(s)`;
      }
      return "Terminé";
    }
    case "error":
      return job.error || "Erreur";
  }
}
