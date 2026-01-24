import { create } from 'zustand';
import type { Project } from '@/types';
import { api } from '@/api/client';

interface ProjectState {
  project: Project | null;
  loading: boolean;
  error: string | null;

  // Actions
  loadProject: (id: string) => Promise<void>;
  createProject: (tiktokUrl?: string, sourcePath?: string, animeName?: string) => Promise<Project>;
  updateProject: (id: string, data: { anime_name?: string }) => Promise<Project>;
  clearProject: () => void;
}

export const useProjectStore = create<ProjectState>((set) => ({
  project: null,
  loading: false,
  error: null,

  loadProject: async (id: string) => {
    set({ loading: true, error: null });
    try {
      const project = await api.getProject(id);
      set({ project, loading: false });
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
    }
  },

  createProject: async (tiktokUrl?: string, sourcePath?: string, animeName?: string) => {
    set({ loading: true, error: null });
    try {
      const project = await api.createProject(tiktokUrl, sourcePath, animeName);
      set({ project, loading: false });
      return project;
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
      throw err;
    }
  },

  updateProject: async (id: string, data: { anime_name?: string }) => {
    set({ loading: true, error: null });
    try {
      const project = await api.updateProject(id, data);
      set({ project, loading: false });
      return project;
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
      throw err;
    }
  },

  clearProject: () => {
    set({ project: null, error: null });
  },
}));
