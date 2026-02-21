export type ProjectPhase =
  | "setup"
  | "downloading"
  | "scene_detection"
  | "scene_validation"
  | "matching"
  | "match_validation"
  | "transcription"
  | "script_restructure"
  | "processing"
  | "complete";

export interface Project {
  id: string;
  tiktok_url: string | null;
  source_paths: string[];
  phase: ProjectPhase;
  created_at: string;
  updated_at: string;
  video_path: string | null;
  video_duration: number | null;
  video_fps: number | null;
  anime_name: string | null;
  output_language: string | null;
  drive_folder_id: string | null;
  drive_folder_url: string | null;
  generation_discord_message_id: string | null;
  final_upload_discord_message_id: string | null;
  upload_completed_at: string | null;
  upload_last_result: Record<string, unknown> | null;
}

export interface PlatformMetadata {
  facebook: {
    title: string;
    description: string;
    tags: string[];
  };
  instagram: {
    caption: string;
  };
  youtube: {
    title: string;
    description: string;
    tags: string[];
  };
  tiktok: {
    description: string;
  };
}

export interface ProjectManagerRow {
  project_id: string;
  anime_title: string | null;
  language: string | null;
  local_size_bytes: number;
  uploaded: boolean;
  uploaded_status: "green" | "orange" | "red";
  can_upload_status: "green" | "orange" | "red";
  can_upload_reasons: string[];
  has_metadata: boolean;
  drive_video_count: number;
  drive_video_name: string | null;
  drive_video_web_url: string | null;
  drive_folder_id: string | null;
  drive_folder_url: string | null;
  scheduled_at: string | null;
  scheduled_account_id: string | null;
}

export interface Account {
  id: string;
  name: string;
  language: string;
  avatar_url: string;
  slots: string[];
}

export interface Scene {
  index: number;
  start_time: number;
  end_time: number;
  duration: number;
}

export interface VideoInfo {
  duration: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  path: string | null;
}

export interface MatchCandidate {
  episode: string;
  timestamp: number;
  similarity: number;
  series: string;
}

export interface AlternativeMatch {
  episode: string;
  start_time: number;
  end_time: number;
  confidence: number;
  speed_ratio: number;
  vote_count: number;
  algorithm?: string; // 'weighted_avg' | 'best_frame' | 'union_topk'
}

export interface SceneMatch {
  scene_index: number;
  episode: string;
  start_time: number;
  end_time: number;
  confidence: number;
  speed_ratio: number;
  confirmed: boolean;
  alternatives: AlternativeMatch[];
  start_candidates: MatchCandidate[];
  middle_candidates: MatchCandidate[];
  end_candidates: MatchCandidate[];
  was_no_match?: boolean; // Track if this scene was initially "no match found"
  merged_from?: number[] | null; // Original scene indices before merge
}

export interface Word {
  text: string;
  start: number;
  end: number;
  confidence: number;
}

export interface SceneTranscription {
  scene_index: number;
  text: string;
  words: Word[];
  start_time: number;
  end_time: number;
}

export interface Transcription {
  language: string;
  scenes: SceneTranscription[];
}
