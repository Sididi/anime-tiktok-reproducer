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
  unmatched_files: string[];
  linked_torrents: number;
  created_at: string;
}

export interface PurgeResult {
  purged_sources: string[];
  freed_bytes: number;
  skipped_protected: string[];
}

// --- Torrent management types ---

export interface TorrentFileMapping {
  torrent_file_index: number;
  torrent_filename: string;
  library_path: string;
  file_size: number;
}

export interface TorrentEntry {
  id: string;
  info_hash: string;
  magnet_uri: string;
  torrent_name: string;
  torrent_file_path: string | null;
  added_at: string;
  files: TorrentFileMapping[];
}

export interface SourceTorrentMetadata {
  torrents: TorrentEntry[];
  purge_protection: boolean;
}

export interface VerificationResult {
  torrent_id: string;
  status: "pass" | "warn" | "fail";
  match_rate: number;
  avg_similarity: number;
  offset_median: number;
  message: string;
}

export interface ReplacementProgressEvent {
  phase:
    | "downloading_verification"
    | "verifying"
    | "results"
    | "saving"
    | "downloading_reindex"
    | "removing_old_index"
    | "reindexing"
    | "cache_cleanup"
    | "complete"
    | "error"
    | "stalled";
  torrent_id: string | null;
  progress: number;
  message: string;
  verification_results?: VerificationResult[];
  error?: string | null;
}
