export interface SourceDetails {
  name: string;
  series_id: string;
  episode_count: number;
  local_episode_count: number;
  total_size_bytes: number;
  fps: number;
  is_fully_local: boolean;
  project_pin_count: number;
  permanent_pin: boolean;
  storage_release_id: string;
  torrent_count: number;
  hydration_status: string;
  updated_at: string;
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
  job_type: "index" | "update";
  source_name: string;
  library_type: import("./index").LibraryType;
  source_path: string;
  fps: number;
  status: "queued" | "indexing" | "complete" | "error";
  progress: number;
  phase: string | null;
  message: string | null;
  current_file: string | null;
  total_files: number;
  completed_files: number;
  current_file_progress: number | null;
  current_file_frames_processed: number | null;
  current_file_total_frames: number | null;
  current_file_batches_processed: number | null;
  error: string | null;
  warnings: string[];
  unmatched_files: string[];
  linked_torrents: number;
  series_id: string | null;
  storage_release_id: string | null;
  created_at: string;
}

export interface ProjectStartupJob {
  job_id: string;
  project_id: string;
  anime_name: string | null;
  series_id: string | null;
  library_type: import("./index").LibraryType;
  tiktok_url: string | null;
  status: "queued" | "running" | "complete" | "error";
  progress: number;
  phase: string | null;
  message: string | null;
  error: string | null;
  ready_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface LibraryActivationState {
  series_id: string | null;
  release_id: string | null;
  hydration_status: string;
  local_episode_count: number;
  expected_episode_count: number;
  is_fully_local: boolean;
  permanent_pin: boolean;
  project_pin_count: number;
  last_error: string | null;
  operation: {
    type: string;
    status: string;
    progress: number;
    error: string | null;
    updated_at: string;
  } | null;
  updated_at: string | null;
}

export interface StorageBoxEpisodeItem {
  episode_key: string;
  size_bytes: number;
  local: boolean;
  local_relative_path: string | null;
}

export interface EpisodeSourcesPayload {
  storage_box: {
    available: boolean;
    series_id: string;
    release_id: string;
    episode_count: number;
    local_episode_count: number;
    episodes: StorageBoxEpisodeItem[];
  };
  torrents: {
    torrent_count: number;
    items: TorrentEntry[];
  };
}

export interface PurgeResult {
  purged_sources: string[];
  freed_bytes: number;
  skipped_protected: string[];
}

export interface SeriesDeleteReferencingProject {
  project_id: string;
  anime_title: string | null;
  phase: import("./index").ProjectPhase;
  scheduled_at: string | null;
  upload_completed_at: string | null;
}

export interface DeleteSeriesResponse {
  status: "deleted";
  series_id: string;
  library_type: import("./index").LibraryType;
}

export interface SeriesDeleteConflictDetail {
  code: string;
  message: string;
  referencing_projects: SeriesDeleteReferencingProject[];
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
