export interface SourceDetails {
  name: string;
  episode_count: number;
  total_size_bytes: number;
  fps: number;
  missing_episodes: number;
  purge_protected: boolean;
  original_index_path: string | null;
}

export interface BrowseEntry {
  name: string;
  path: string;
  is_dir: boolean;
  has_videos: boolean;
  mtime: number;
}

export interface BrowseResult {
  current_path: string;
  parent_path: string | null;
  entries: BrowseEntry[];
}

export interface IndexationJob {
  id: string;
  source_name: string;
  library_type: import("./index").LibraryType;
  source_path: string;
  fps: number;
  status: "queued" | "indexing" | "complete" | "error";
  progress: number;
  phase: string | null;
  message: string | null;
  error: string | null;
  created_at: string;
}

export interface PurgeResult {
  purged_sources: string[];
  freed_bytes: number;
  skipped_protected: string[];
}
