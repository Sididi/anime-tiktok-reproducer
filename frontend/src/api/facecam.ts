import type { Project } from '@/types';

export interface FacecamBlock { id: string; kind: 'hook' | 'body' | 'raw' | 'opinion' | 'ending'; text: string; scene_indices: number[] }
export interface Finding { quote: string; start: number; end: number; kind: 'contradiction' | 'uncertain'; explanation: string; correction: string; sources: {url: string; title: string; kind?: string; text?: string; language?: string}[] }
export interface FacecamState {
  revision: number; hooks: {id: string; text: string; original: boolean}[]; opinions: {id: string; text: string; original: boolean}[];
  selected_hook: string; selected_opinion: string; blocks: FacecamBlock[]; approved_revision: number | null;
  fact_revision: number | null; fact_status: 'unchecked' | 'complete' | 'unavailable'; fact_error: string | null; findings: Finding[];
  fact_context?: {padding_seconds: number; episodes: {episode: string; status: 'available' | 'partial' | 'unavailable'; language: string | null; note: string | null; windows: {start: string; end: string}[]; cues: {id: string; start: string; end: string; text: string; source_url: string}[]}[]} | null;
  recording: {fingerprint: string; duration: number; transcript: string; differences: {kind: string; expected: string; spoken: string}[]; reviewed_revision: number | null} | null;
  prepared: {revision: number; fingerprint: string; preview_music_key?: string | null; preview_version?: string} | null; cover: {anilist_id?: number; manual?: boolean} | null;
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
    request.onload = () => { try { const result = JSON.parse(request.responseText); request.status < 300 ? resolve(result) : reject(new Error(result.detail || 'Import impossible.')); } catch { reject(new Error('Réponse serveur invalide.')); } };
    request.onerror = () => reject(new Error('Connexion interrompue pendant l’import.'));
    const form = new FormData(); form.append('file', file); request.send(form);
  });
}
