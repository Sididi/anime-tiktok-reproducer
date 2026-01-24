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
