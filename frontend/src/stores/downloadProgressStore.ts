import { create } from "zustand";

export interface DownloadProgressEntry {
  state: "in_progress" | "done";
  bytesDone?: number;
  bytesTotal?: number;
  title?: string | null;
}

interface DownloadProgressState {
  downloads: Record<string, DownloadProgressEntry>;
  report: (projectId: string, entry: DownloadProgressEntry) => void;
  clear: (projectId: string) => void;
}

export const useDownloadProgressStore = create<DownloadProgressState>((set) => ({
  downloads: {},
  report: (projectId, entry) =>
    set((s) => ({ downloads: { ...s.downloads, [projectId]: entry } })),
  clear: (projectId) =>
    set((s) => {
      const next = { ...s.downloads };
      delete next[projectId];
      return { downloads: next };
    }),
}));
