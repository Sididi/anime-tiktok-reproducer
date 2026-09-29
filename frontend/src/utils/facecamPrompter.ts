import type { FacecamBlock } from '@/api/facecam';

export interface PrompterSettings { speed: number; fontSize: number; mirror: boolean }
export interface PrompterDocument { projectId: string; title: string; revision: number; blocks: FacecamBlock[] }
export interface PrompterStatus { playing: boolean; progress: number; blockId: string | null; finished: boolean }
export type PrompterCommand = { action: 'toggle' | 'restart' } | { action: 'seek'; blockId: string };
export type PrompterMessage =
  | { type: 'ready' | 'ping' | 'closed' }
  | { type: 'document'; document: PrompterDocument }
  | { type: 'settings'; settings: PrompterSettings }
  | { type: 'command'; command: PrompterCommand }
  | { type: 'status'; status: PrompterStatus; settings: PrompterSettings };

export const defaultPrompterSettings: PrompterSettings = { speed: 32, fontSize: 46, mirror: false };
export const initialPrompterStatus: PrompterStatus = { playing: false, progress: 0, blockId: null, finished: false };
const settingsKey = 'facecam-prompter-settings-v1';

export function prompterChannel(projectId: string) { return `facecam-prompter-v1:${projectId}`; }

export function normalizePrompterSettings(value: Partial<PrompterSettings>): PrompterSettings {
  const bounded = (n: unknown, fallback: number, min: number, max: number) =>
    typeof n === 'number' && Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : fallback;
  return {
    speed: bounded(value.speed, defaultPrompterSettings.speed, 5, 120),
    fontSize: bounded(value.fontSize, defaultPrompterSettings.fontSize, 28, 80),
    mirror: value.mirror === true,
  };
}

export function readPrompterSettings(): PrompterSettings {
  try { return normalizePrompterSettings(JSON.parse(localStorage.getItem(settingsKey) || '{}') || {}); }
  catch { return { ...defaultPrompterSettings }; }
}

export function savePrompterSettings(settings: PrompterSettings) {
  try { localStorage.setItem(settingsKey, JSON.stringify(settings)); } catch { /* Reading also works without storage. */ }
}

export function prompterBlockLabel(block: FacecamBlock, index: number) {
  const labels = { hook: 'Accroche', body: 'Récit', raw: 'Extrait original · ne pas lire', opinion: 'Point de vue', ending: 'Phrase de fin' };
  return `${index + 1}. ${labels[block.kind]}`;
}
