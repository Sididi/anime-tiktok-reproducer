import type { Project } from '@/types';

export interface FacecamBlock { id: string; kind: 'hook' | 'body' | 'raw' | 'opinion' | 'ending'; text: string; scene_indices: number[] }
export interface Finding { quote: string; start: number; end: number; kind: 'contradiction' | 'uncertain'; explanation: string; correction: string; sources: {url: string; title: string; kind?: string; text?: string; language?: string}[] }
export interface FacecamStory {
  id: string; title: string; premise: string; ending: string; steps: string[]; omitted: string[];
  clips: {scene_index: number; start_word: number | null; end_word: number | null}[];
  raw_duration_seconds: number;
  estimated_duration_seconds?: number | null;
}
export const FULL_STORY_ID = 'full-story';
export interface FacecamAutoEditorSettings {
  noise_threshold_db: number;
  min_silence_ms: number;
  margin_before_ms: number;
  margin_after_ms: number;
  speed: number;
}
export interface RecordingCut {id: string; start: number; end: number; enabled: boolean; origin: 'manual' | 'ai'}
export interface RecordingSuggestion extends RecordingCut {confidence: 'high' | 'review'; reason: string; removed_text: string; retained_text: string}
export interface RecordingEdit {
  id: string; version: number; duration: number; applied: boolean;
  analysis_status: 'analyzing' | 'ready' | 'error'; analysis_error: string | null;
  suggestions: RecordingSuggestion[]; cuts: RecordingCut[];
}
export interface RecordingCutData {id: string; duration: number; peaks: number[]; words: {text: string; start: number; end: number}[]}
export const DEFAULT_FACECAM_AUTO_EDITOR: FacecamAutoEditorSettings = {
  noise_threshold_db: -26, min_silence_ms: 120, margin_before_ms: 30, margin_after_ms: 50, speed: 1.0,
};
export function sameAutoEditorSettings(a?: FacecamAutoEditorSettings, b?: FacecamAutoEditorSettings): boolean {
  return !!a && !!b && (Object.keys(DEFAULT_FACECAM_AUTO_EDITOR) as (keyof FacecamAutoEditorSettings)[])
    .every(key => (a[key] ?? DEFAULT_FACECAM_AUTO_EDITOR[key]) === (b[key] ?? DEFAULT_FACECAM_AUTO_EDITOR[key]));
}
export interface FacecamState {
  stories?: FacecamStory[]; full_story?: FacecamStory | null; selected_story_id?: string | null; estimated_duration_seconds?: number | null;
  revision: number; hooks: {id: string; text: string; original: boolean}[]; opinions: {id: string; text: string; original: boolean}[];
  selected_hook: string; selected_opinion: string; blocks: FacecamBlock[]; approved_revision: number | null;
  fact_revision: number | null; fact_status: 'unchecked' | 'complete' | 'unavailable'; fact_error: string | null; findings: Finding[];
  fact_context?: {padding_seconds: number; episodes: {episode: string; status: 'available' | 'partial' | 'unavailable'; language: string | null; note: string | null; windows: {start: string; end: string}[]; cues: {id: string; start: string; end: string; text: string; source_url: string}[]}[]} | null;
  recording: {fingerprint: string; duration: number; transcript: string; differences: {kind: string; expected: string; spoken: string}[]; reviewed_revision: number | null} | null;
  recording_edit?: RecordingEdit | null;
  auto_editor?: FacecamAutoEditorSettings;
  preview_enabled?: boolean;
  prepared: {revision: number; fingerprint: string; preview_music_key?: string | null; preview_version?: string; duration_seconds?: number;
    auto_editor?: FacecamAutoEditorSettings; auto_editor_signature?: string;
    cut_report?: {removed_seconds: number; cut_count: number; protected_word_count: number; clipped_word_count: number};
  } | null; cover: {anilist_id?: number; manual?: boolean} | null;
  job: {status: string; step?: string; message?: string; error?: string} | null;
}
export async function facecamRequest<T = FacecamState>(projectId: string, path = '', method = 'GET', data?: unknown): Promise<T> {
  const response = await fetch(`/api/projects/${projectId}/facecam${path}`, {method, headers: data ? {'Content-Type': 'application/json'} : undefined, body: data ? JSON.stringify(data) : undefined});
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(typeof error.detail === 'string' ? error.detail : 'Opération Facecam impossible.'); }
  return response.json();
}
export const createFacecam = (id: string) => facecamRequest<Project>(id, '', 'POST');
export function uploadFacecam(projectId: string, file: File, path: string, progress: (value: number) => void): Promise<FacecamState> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest(); request.open('POST', `/api/projects/${projectId}/facecam/${path}`);
    request.upload.onprogress = e => { if (e.lengthComputable) progress(Math.round(e.loaded / e.total * 100)); };
    request.onload = () => { try { const result = JSON.parse(request.responseText); if (request.status < 300) resolve(result); else reject(new Error(result.detail || 'Import impossible.')); } catch { reject(new Error('Réponse serveur invalide.')); } };
    request.onerror = () => reject(new Error('Connexion interrompue pendant l’import.'));
    const form = new FormData(); form.append('file', file); request.send(form);
  });
}
