import { create } from 'zustand';
import type { Scene } from '@/types';
import { api } from '@/api/client';

interface SceneState {
  scenes: Scene[];
  loading: boolean;
  error: string | null;

  // Actions
  loadScenes: (projectId: string) => Promise<void>;
  setScenes: (scenes: Scene[]) => void;
  updateScene: (index: number, updates: Partial<Scene>) => void;
  splitScene: (projectId: string, sceneIndex: number, timestamp: number) => Promise<void>;
  mergeScenes: (projectId: string, index1: number, index2: number) => Promise<void>;
  saveScenes: (projectId: string) => Promise<void>;

  // Helpers
  getSceneAtTime: (time: number) => Scene | null;
  getCurrentSceneIndex: (time: number) => number;
}

export const useSceneStore = create<SceneState>((set, get) => ({
  scenes: [],
  loading: false,
  error: null,

  loadScenes: async (projectId: string) => {
    set({ loading: true, error: null });
    try {
      const { scenes } = await api.getScenes(projectId);
      set({ scenes, loading: false });
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
    }
  },

  setScenes: (scenes: Scene[]) => set({ scenes }),

  updateScene: (index: number, updates: Partial<Scene>) => {
    const { scenes } = get();
    const newScenes = scenes.map((scene, i) =>
      i === index ? { ...scene, ...updates } : scene
    );
    set({ scenes: newScenes });
  },

  splitScene: async (projectId: string, sceneIndex: number, timestamp: number) => {
    set({ loading: true, error: null });
    try {
      const { scenes } = await api.splitScene(projectId, sceneIndex, timestamp);
      set({ scenes, loading: false });
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
    }
  },

  mergeScenes: async (projectId: string, index1: number, index2: number) => {
    set({ loading: true, error: null });
    try {
      const { scenes } = await api.mergeScenes(projectId, [index1, index2]);
      set({ scenes, loading: false });
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
    }
  },

  saveScenes: async (projectId: string) => {
    const { scenes } = get();
    set({ loading: true, error: null });
    try {
      const result = await api.updateScenes(projectId, scenes);
      set({ scenes: result.scenes, loading: false });
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
    }
  },

  getSceneAtTime: (time: number) => {
    const { scenes } = get();
    return scenes.find((s) => time >= s.start_time && time < s.end_time) || null;
  },

  getCurrentSceneIndex: (time: number) => {
    const { scenes } = get();
    const index = scenes.findIndex((s) => time >= s.start_time && time < s.end_time);
    return index >= 0 ? index : 0;
  },
}));
